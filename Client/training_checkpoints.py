import hashlib
import json
import queue
import re
import shutil
import tarfile
import threading
import time
from pathlib import Path

import requests
import zstandard


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def send_artifact(connection, run_id, path, name, kind, reporter):
    digest = digest_file(path)
    size = path.stat().st_size
    for attempt in range(5):
        reporter.check()
        try:
            with path.open('rb') as source:
                response = connection.request('POST', '%d/artifacts/' % run_id, params={'name': name, 'kind': kind, 'sha256': digest}, data=source, timeout=(15, 300)).json()
            if response['sha256'] != digest or response['size'] != size:
                raise RuntimeError('Artifact acknowledgement did not match the uploaded file.')
            return response
        except (requests.RequestException, RuntimeError):
            if attempt == 4:
                raise
            reporter.stop.wait(min(30, 2 ** attempt))


class CheckpointUploader:
    def __init__(self, connection, job, directory, reporter):
        self.connection = connection
        self.job = job
        self.directory = directory
        self.reporter = reporter
        self.pending = queue.Queue(maxsize=8)
        self.seen = set()
        self.uploaded_networks = set()
        self.saved = 0
        self.records = []
        self.error = None
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def observe(self, line):
        descriptor = None
        if line.startswith('MATTBENCH_CHECKPOINT '):
            try:
                descriptor = json.loads(line.removeprefix('MATTBENCH_CHECKPOINT '))
            except ValueError:
                raise RuntimeError('Invalid MATTBENCH_CHECKPOINT record.') from None
        else:
            match = re.fullmatch(r'Saved \[(.+)-(\d+)\]\s*', line)
            if match:
                descriptor = {'path': '%s-%s' % match.groups(), 'superbatch': int(match[2])}
        if descriptor is None:
            return
        if not isinstance(descriptor, dict) or type(descriptor.get('superbatch')) is not int or not 1 <= descriptor['superbatch'] <= 10000000 or not isinstance(descriptor.get('path'), str):
            raise RuntimeError('Checkpoint records require path and superbatch.')
        step = descriptor['superbatch']
        if self.closed.is_set():
            raise RuntimeError('The schedule announced a checkpoint after checkpoint collection ended.')
        if step in self.seen:
            return
        self.seen.add(step)
        while True:
            self.reporter.check()
            try:
                self.pending.put(descriptor, timeout=1)
                return
            except queue.Full:
                continue

    def upload(self, descriptor):
        outputs = (self.directory / 'outputs').resolve()
        checkpoint = (outputs / descriptor['path']).resolve()
        if checkpoint == outputs or not checkpoint.is_relative_to(outputs) or not checkpoint.is_dir():
            raise RuntimeError('Checkpoint directory must be inside the training output directory.')
        files = sorted(checkpoint.rglob('*'))
        if any(file.is_symlink() or not file.resolve().is_relative_to(checkpoint) for file in files):
            raise RuntimeError('Checkpoint files must not contain symlinks or external paths.')
        files = [file for file in files if file.is_file()]
        if not files or len(files) > 10000 or not (checkpoint / 'optimiser_state' / 'weights.bin').is_file():
            raise RuntimeError('A complete Bullet checkpoint must include optimiser_state/weights.bin and its optimiser state.')
        network = checkpoint / descriptor.get('network', 'quantised.bin')
        if not network.resolve().is_relative_to(checkpoint) or not network.is_file():
            raise RuntimeError('The checkpoint network is missing.')
        limits = self.job['snapshot']['settings']
        if not limits['network_min_bytes'] <= network.stat().st_size <= limits['network_max_bytes']:
            raise RuntimeError('The checkpoint network has an unexpected size.')
        before = [(file, file.stat().st_size, file.stat().st_mtime_ns) for file in files]
        step = descriptor['superbatch']
        archive = self.directory / ('checkpoint-%d.tar.zst' % step)
        with archive.open('wb') as output:
            with zstandard.ZstdCompressor(level=3).stream_writer(output) as compressed:
                with tarfile.open(fileobj=compressed, mode='w|') as tar:
                    for file in files:
                        self.reporter.check()
                        tar.add(file, arcname=file.relative_to(checkpoint).as_posix(), recursive=False)
        if any(not file.exists() or file.stat().st_size != size or file.stat().st_mtime_ns != modified for file, size, modified in before):
            raise RuntimeError('The schedule changed a checkpoint after announcing it as complete.')
        saved_archive = send_artifact(self.connection, self.job['id'], archive, archive.name, 'checkpoint', self.reporter)
        saved_network = send_artifact(self.connection, self.job['id'], network, 'sb-%d.bin' % step, 'network', self.reporter)
        payload = {'superbatch': step, 'archive': saved_archive['id'], 'network': saved_network['id'], 'metadata': descriptor.get('metadata', {})}
        for attempt in range(5):
            try:
                result = self.connection.request('POST', '%d/checkpoints/' % self.job['id'], json=payload).json()
                if not result.get('stored'):
                    raise RuntimeError('Checkpoint was not acknowledged.')
                break
            except (requests.RequestException, RuntimeError):
                if attempt == 4:
                    raise
                self.reporter.stop.wait(min(30, 2 ** attempt))
                self.reporter.check()
        self.saved += 1
        self.records.append({**payload, 'id': result['checkpoint'], 'archive_sha256': saved_archive['sha256'], 'network_sha256': saved_network['sha256']})
        self.uploaded_networks.add(network.resolve())
        self.reporter.update(checkpoints_saved=self.saved, last_checkpoint_superbatch=step)
        self.reporter.write('Checkpoint SB %d stored and verified by MattBench.\n' % step)
        archive.unlink()
        if limits.get('delete_uploaded_checkpoints', True) and result.get('replicated'):
            if any(not file.exists() or file.stat().st_size != size or file.stat().st_mtime_ns != modified for file, size, modified in before):
                raise RuntimeError('The schedule changed an uploaded checkpoint; its local directory was preserved.')
            if checkpoint != outputs and checkpoint.is_relative_to(outputs):
                shutil.rmtree(checkpoint)

    def loop(self):
        while not self.closed.is_set() or not self.pending.empty():
            try:
                item = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if item is None:
                    return
                if self.error or self.closed.is_set():
                    continue
                self.upload(item)
            except Exception as error:
                reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                self.error = 'Checkpoint upload failed: %s Local checkpoint files are preserved.' % reason
                self.reporter.abort_reason = self.error
                self.reporter.stop.set()
            finally:
                self.pending.task_done()

    def finish(self):
        while self.pending.unfinished_tasks:
            if self.error:
                raise RuntimeError(self.error)
            self.reporter.check()
            time.sleep(0.2)
        self.closed.set()
        self.thread.join(timeout=10)
        if self.error:
            raise RuntimeError(self.error)

    def close(self):
        self.closed.set()
        self.thread.join(timeout=320)


def download_checkpoint(connection, job, directory, reporter):
    resume = job['snapshot'].get('resume')
    if not resume:
        return None
    path = directory / 'resume.tar.zst'
    digest = hashlib.sha256()
    count = 0
    with connection.request('GET', '%d/resume/' % job['id'], stream=True) as response:
        with path.open('wb') as output:
            for chunk in response.iter_content(4 * 1024 * 1024):
                reporter.check()
                count += len(chunk)
                if count > resume['size']:
                    raise RuntimeError('Checkpoint download exceeded its declared size.')
                digest.update(chunk)
                output.write(chunk)
    if count != resume['size'] or digest.hexdigest() != resume['sha256']:
        raise RuntimeError('Resume checkpoint checksum verification failed.')
    destination = directory / 'resume'
    destination.mkdir()
    with path.open('rb') as source:
        with zstandard.ZstdDecompressor().stream_reader(source) as expanded:
            with tarfile.open(fileobj=expanded, mode='r|') as archive:
                members = 0
                for member in archive:
                    reporter.check()
                    members += 1
                    target = (destination / member.name).resolve()
                    if members > 10000 or not member.isfile() or not target.is_relative_to(destination.resolve()) or target == destination or '\\' in member.name:
                        raise RuntimeError('Unsafe checkpoint archive member.')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as data, target.open('xb') as output:
                        while chunk := data.read(4 * 1024 * 1024):
                            reporter.check()
                            output.write(chunk)
    path.unlink()
    return destination
