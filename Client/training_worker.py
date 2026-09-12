import argparse
import bz2
import gzip
import hashlib
import json
import os
import platform
import queue
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import tarfile
import threading
import time
import tomllib
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

import psutil
import requests

try:
    from . import training_cache
    from .training_gpu import detect_gpu
    from .training_checkpoints import CheckpointUploader, download_checkpoint
    from .training_data import prepare_and_publish
    from .training_tools import build_dataset_tools, PAWNOCCHIO_REPO, PAWNOCCHIO_REF, COMBINER_REPO, COMBINER_REF
    from .training_runtime import cuda_build_environment, identity, isolated_command, native_command, remove_container, rocm_environment_variable, runtime_info, stop_previous, worker_lock, windows_build_environment, write_json
except ImportError:
    import training_cache
    from training_gpu import detect_gpu
    from training_checkpoints import CheckpointUploader, download_checkpoint
    from training_data import prepare_and_publish
    from training_tools import build_dataset_tools, PAWNOCCHIO_REPO, PAWNOCCHIO_REF, COMBINER_REPO, COMBINER_REF
    from training_runtime import cuda_build_environment, identity, isolated_command, native_command, remove_container, rocm_environment_variable, runtime_info, stop_previous, worker_lock, windows_build_environment, write_json


class Stopped(Exception):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def remove_work_directory(path, root):
    root = root.resolve()
    target = path.resolve()
    if target == root or not target.is_relative_to(root) or path.is_symlink():
        raise RuntimeError('Cleanup path escapes the worker directory: %s' % path)
    if not path.exists():
        return
    def retry(function, name, error):
        if not issubclass(error[0], PermissionError):
            raise error[1]
        os.chmod(name, os.stat(name).st_mode | stat.S_IWUSR)
        function(name)
    try:
        shutil.rmtree(path, onerror=retry)
    except OSError as error:
        raise RuntimeError('Could not clean up %s: %s' % (path, error)) from error


def cleanup_runs(root):
    for record in root.glob('.process-*.json'):
        match = re.fullmatch(r'\.process-([0-9]+)(?:-[a-f0-9]+)?\.json', record.name)
        if match:
            stop_previous(root / match[1])
    for directory in root.iterdir():
        if re.fullmatch(r'[0-9]+', directory.name):
            stop_previous(directory)
            remove_work_directory(directory, root)
    remove_work_directory(root / 'transfer-cache', root)


class Connection:
    def __init__(self, server, worker, token):
        self.server = server.rstrip('/')
        self.headers = {'Authorization': 'Bearer ' + token, 'X-Training-Worker': worker}

    def url(self, path):
        return self.server + '/api/training/' + path.lstrip('/')

    def request(self, method, path, **kwargs):
        response = requests.request(method, self.url(path), headers=self.headers, timeout=kwargs.pop('timeout', (15, 60)), **kwargs)
        if response.status_code >= 400:
            try:
                message = response.json().get('error', 'Server rejected the request.')
            except ValueError:
                message = 'Server returned HTTP %d.' % response.status_code
            raise RuntimeError(message)
        return response


class StageConnection(Connection):
    def __init__(self, connection, stage):
        self.server = connection.server
        self.headers = connection.headers
        self.stage = stage

    def url(self, path):
        url = super().url(path)
        return url + ('&' if '?' in url else '?') + 'stage=%d' % self.stage


class Reporter:
    def __init__(self, connection, job, directory):
        self.connection = connection
        self.job = job
        self.directory = directory
        self.state = job.get('state', 'DOWNLOADING')
        self.sequence = job.get('report_sequence', 0)
        self.inflight = None
        self.ack_state = self.state
        self.metrics = {}
        self.pending = ''
        self.error = ''
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.stop = threading.Event()
        self.done = threading.Event()
        self.last_success = time.monotonic()
        self.started = time.monotonic()
        self.abort_reason = ''
        self.checkpoints = None
        self.resumed = False
        self.expanded_bytes = 0
        self.log_path = directory / 'worker.log'
        self.log_file = self.log_path.open('a', encoding='utf-8')
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        self.control_thread = threading.Thread(target=self.control_loop, daemon=True)
        self.control_thread.start()

    def write(self, text):
        text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
        text = re.sub(r'\b(?:hf_|xet_)[A-Za-z0-9_-]{10,}', '[redacted]', text)
        with self.lock:
            self.log_file.write(text)
            self.log_file.flush()
            self.pending = (self.pending + text)[-131072:]
        print(text, end='', flush=True)
        if self.checkpoints and self.state in ('TRAINING', 'SAVING'):
            self.checkpoints.observe(text)
        if text.startswith('MATTBENCH_RESUMED '):
            expected = self.job['snapshot'].get('resume', {}).get('superbatch', 0) + 1
            if text.strip().removeprefix('MATTBENCH_RESUMED ') != str(expected):
                raise RuntimeError('The schedule acknowledged the wrong resume superbatch.')
            self.resumed = True
        if text.startswith('MATTBENCH_METRIC '):
            try:
                values = json.loads(text.removeprefix('MATTBENCH_METRIC '))
                allowed = ('loss', 'validation_loss', 'step', 'superbatch', 'learning_rate', 'wdl_blend', 'positions_per_second', 'progress')
                if isinstance(values, dict):
                    self.update(**{key: value for key, value in values.items() if key in allowed and type(value) in (int, float) and abs(value) < 1e20})
            except (ValueError, TypeError):
                pass

    def update(self, **values):
        with self.lock:
            self.metrics.update(values)

    def check(self):
        if self.stop.is_set():
            if self.abort_reason:
                raise RuntimeError(self.abort_reason)
            raise Stopped('Training stopped by the server or its owner.')
        if time.monotonic() - self.started > self.job['snapshot']['settings']['max_hours'] * 3600:
            raise RuntimeError('The configured maximum runtime was reached.')
        if shutil.disk_usage(self.directory).free < self.job['snapshot']['settings'].get('disk_reserve_gb', 10) * 1024 ** 3:
            raise RuntimeError('Worker disk space is exhausted.')

    def flush(self):
        with self.send_lock:
            with self.lock:
                if self.ack_state == 'COMPLETED' and self.inflight is None:
                    return
                if self.inflight is not None and self.state in ('FAILED', 'CANCELLED') and self.inflight['state'] != self.state:
                    self.inflight = None
                if self.inflight is None:
                    self.inflight = {'sequence': self.sequence + 1, 'state': self.state, 'metrics': dict(self.metrics), 'log': self.pending[:32768], 'error': self.error}
                payload = self.inflight
                chunk = payload['log']
            result = self.connection.request('POST', '%d/report/' % self.job['id'], json=payload).json()
            if result.get('stop') and not (result.get('state') == payload['state'] and payload['state'] in ('COMPLETED', 'FAILED', 'CANCELLED')):
                self.stop.set()
                if result.get('state') == 'FAILED':
                    self.abort_reason = 'The server marked this run failed.'
                raise Stopped('The server stopped this run.')
            if result.get('sequence', payload['sequence']) < payload['sequence']:
                raise RuntimeError('Report changed concurrently; retrying the same sequence.')
            with self.lock:
                self.sequence = payload['sequence']
                self.ack_state = payload['state']
                self.inflight = None
                if self.pending.startswith(chunk):
                    self.pending = self.pending[len(chunk):]
            self.last_success = time.monotonic()
            if result.get('stop'):
                self.stop.set()

    def stage(self, state):
        self.check()
        with self.lock:
            self.state = state
            self.metrics['progress'] = 100 if state == 'COMPLETED' else 0
        self.write('\n%s\n' % state.capitalize())
        while True:
            self.check()
            try:
                self.flush()
                if self.ack_state == state:
                    return
            except (requests.RequestException, RuntimeError):
                if time.monotonic() - self.last_success > 480:
                    raise RuntimeError('Server unavailable for eight minutes; stopping this run.') from None
                self.stop.wait(5)

    def control_loop(self):
        while not self.done.is_set() and not self.stop.is_set():
            try:
                result = self.connection.request('POST', '%d/control/' % self.job['id'], json={}, timeout=(2, 2)).json()
                if result.get('stop'):
                    if result.get('state') == self.state and self.state in ('COMPLETED', 'FAILED', 'CANCELLED'):
                        return
                    if result.get('state') == 'FAILED':
                        self.abort_reason = 'The server marked this run failed.'
                    self.stop.set()
                    if self.state in ('DOWNLOADING', 'CONVERTING'):
                        from huggingface_hub.utils._xet import abort_xet_session
                        abort_xet_session()
                    return
            except (requests.RequestException, RuntimeError, ValueError):
                pass
            if self.done.wait(1):
                return

    def loop(self):
        while not self.done.wait(5):
            try:
                self.update(elapsed_seconds=round(time.monotonic() - self.started), cpu_percent=psutil.cpu_percent(), ram_used_gb=round(psutil.virtual_memory().used / 1024 ** 3, 2), disk_free_gb=round(shutil.disk_usage(self.directory).free / 1024 ** 3, 2))
                if self.job['snapshot']['settings']['backend'] == 'cuda':
                    try:
                        output = subprocess.check_output(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used', '--format=csv,noheader,nounits', '-i', self.job['worker_info'].get('device', '0')], text=True, timeout=3, stderr=subprocess.DEVNULL)
                        gpu, memory = output.strip().split(',')
                        self.update(gpu_percent=float(gpu), gpu_used_mb=float(memory))
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                self.flush()
            except Exception:
                if time.monotonic() - self.last_success > 480:
                    self.abort_reason = 'Server unavailable for eight minutes; stopping this run.'
                    self.stop.set()
            if time.monotonic() - self.started > self.job['snapshot']['settings']['max_hours'] * 3600:
                self.abort_reason = 'The configured maximum runtime was reached.'
                self.stop.set()
            if self.stop.is_set() and self.state in ('DOWNLOADING', 'CONVERTING'):
                from huggingface_hub.utils._xet import abort_xet_session
                abort_xet_session()

    def close(self):
        self.done.set()
        self.control_thread.join()
        self.thread.join()
        with self.lock:
            self.log_file.close()


def child_environment(directory, job):
    keep = ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LD_LIBRARY_PATH', 'LIBRARY_PATH', 'CUDA_PATH', 'CUDA_HOME', 'CUDA_VISIBLE_DEVICES', 'HIP_PATH', 'ROCM_PATH', 'HIP_VISIBLE_DEVICES', 'RUSTUP_HOME', 'INCLUDE', 'LIB', 'LIBPATH', 'VCToolsInstallDir', 'VSINSTALLDIR', 'WindowsSdkDir', 'WindowsSDKVersion', 'UniversalCRTSdkDir', 'UCRTVersion')
    environment = {key: value for key, value in os.environ.items() if key in keep or rocm_environment_variable(key)}
    environment = windows_build_environment(environment)
    home = directory / 'home'
    home.mkdir(exist_ok=True)
    temporary = directory / 'tmp'
    temporary.mkdir(exist_ok=True)
    environment.setdefault('RUSTUP_HOME', str(Path.home() / '.rustup'))
    environment.update(HOME=str(home), USERPROFILE=str(home), CARGO_HOME=str(home / '.cargo'), CARGO_TARGET_DIR=str(directory / 'bullet' / 'target'), TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary), GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, CARGO_BUILD_JOBS=str(job['worker_info']['threads']), MATTBENCH_JOB=str(directory / 'job.json'), MATTBENCH_OUTPUT_DIR=str(directory / 'outputs'), MATTBENCH_DATA_DIR=str(directory / 'data'), MATTBENCH_NET_NAME=job['name'])
    return environment


def kill_tree(process):
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.Error:
                pass
        parent.kill()
    except psutil.Error:
        pass
    process.wait(timeout=10)


def command(args, cwd, environment, reporter, trusted=False, cpu_threads=None):
    reporter.check()
    arguments, child_env, container = (args, environment, None) if trusted else isolated_command(args, cwd, environment, reporter, cpu_threads)
    arguments = native_command(arguments, cwd, child_env)
    process_record = reporter.directory.parent / ('.process-' + reporter.directory.name + '-' + uuid.uuid4().hex + '.json')
    write_json(process_record, {'pid': None, 'created': None, 'container': container})
    process = subprocess.Popen(arguments, cwd=cwd, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=os.name != 'nt')
    write_json(process_record, {'pid': process.pid, 'created': psutil.Process(process.pid).create_time(), 'container': container})
    output = queue.Queue(maxsize=256)
    def read_output():
        try:
            while line := process.stdout.readline(16384):
                output.put(line.decode('utf-8', errors='replace'))
        finally:
            output.put(None)
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    command_started = time.monotonic()
    tail = ''
    try:
        while True:
            reporter.check()
            if reporter.state == 'TRAINING' and reporter.job['snapshot'].get('resume') and not reporter.resumed and time.monotonic() - command_started > 300:
                raise RuntimeError('The schedule did not acknowledge restoring its checkpoint within five minutes.')
            try:
                line = output.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            reporter.write(line)
            tail = (tail + line)[-48000:]
        while process.poll() is None:
            reporter.check()
            reporter.stop.wait(0.2)
        if process.returncode != 0:
            raise RuntimeError('%s %s failed with exit code %d. %s' % (Path(args[0]).name, ' '.join(map(str, args[1:])), process.returncode, tail[-2000:]))
        if reporter.state == 'TRAINING' and reporter.job['snapshot'].get('resume') and not reporter.resumed:
            raise RuntimeError('The schedule exited without acknowledging its resume checkpoint.')
        return tail
    finally:
        if process.poll() is None:
            kill_tree(process)
        if container:
            try:
                remove_container(container)
            except (RuntimeError, subprocess.TimeoutExpired):
                raise SystemExit('Execution container cleanup could not be confirmed. The worker will reconcile it on restart.') from None
        process_record.unlink(missing_ok=True)
        process.stdout.close()


def download_dataset(connection, job, directory, reporter):
    from huggingface_hub.file_download import xet_get
    from huggingface_hub.utils import XetFileData
    from huggingface_hub.utils._xet import abort_xet_session
    from tqdm import tqdm
    downloads = directory / 'downloads'
    downloads.mkdir()
    total = job['dataset']['size']
    files = job['dataset']['files']
    paths = [None] * len(files)
    transferred = [0] * len(files)
    downloaded = 0
    progress_lock = threading.Lock()
    cancelled = threading.Event()
    workers = int(os.environ.get('MATTBENCH_HF_DOWNLOAD_WORKERS', '8'))
    if workers < 1:
        raise ValueError('MATTBENCH_HF_DOWNLOAD_WORKERS must be positive.')

    def check():
        if cancelled.is_set():
            raise Stopped('Dataset download cancelled.')
        reporter.check()

    def update_progress(index, amount):
        nonlocal downloaded
        check()
        with progress_lock:
            previous = transferred[index]
            transferred[index] = min(files[index]['size'], max(previous, amount))
            downloaded += transferred[index] - previous
            reporter.update(progress=min(100, 100 * downloaded / max(1, total)), downloaded_bytes=downloaded)

    def download_file(index, file):
        check()
        destination = downloads / ('%05d-' % index + Path(file['path']).name)
        reporter.update(current_file=file['path'])
        access = connection.request('GET', '%d/dataset/%d/' % (job['id'], index)).json()
        check()
        if access.get('xet_hash'):
            class DownloadProgress(tqdm):
                def __init__(self, *args, **kwargs):
                    kwargs['disable'] = True
                    super().__init__(*args, **kwargs)

                def update(self, amount=1):
                    self.n += amount
                    update_progress(index, self.n)
                    if reporter.stop.is_set():
                        raise Stopped('Download cancelled.')

                def update_transfer(self, amount=1):
                    check()

                def set_transfer_postfix_str(self, value, refresh=False):
                    pass
            xet_get(incomplete_path=destination, xet_file_data=XetFileData(file_hash=access['xet_hash'], refresh_route=connection.url('%d/xet-token/?file=%d' % (job['id'], index))), headers=connection.headers, expected_size=file['size'], tqdm_class=DownloadProgress)
        else:
            source = connection.request('GET', '%d/dataset/%d/file/' % (job['id'], index), stream=True) if access.get('proxy') else requests.get(access['url'], stream=True, timeout=(15, 60))
            with source:
                source.raise_for_status()
                written = 0
                with destination.open('wb') as output:
                    for chunk in source.iter_content(4 * 1024 * 1024):
                        check()
                        written += len(chunk)
                        if written > file['size']:
                            raise RuntimeError('Dataset download exceeded its declared size.')
                        output.write(chunk)
                        update_progress(index, written)
        check()
        if destination.stat().st_size != file['size']:
            raise RuntimeError('Dataset file size verification failed.')
        if file.get('sha256') and sha256_file(destination) != file['sha256']:
            raise RuntimeError('Dataset checksum verification failed.')
        if not file.get('sha256') and file.get('blob_id'):
            digest = hashlib.sha1(('blob %d\0' % file['size']).encode())
            with destination.open('rb') as source:
                for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
                    check()
                    digest.update(chunk)
            if digest.hexdigest() != file['blob_id']:
                raise RuntimeError('Dataset Git blob verification failed.')
        update_progress(index, file['size'])
        return destination

    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(files))), thread_name_prefix='hf-download') as executor:
        futures = {executor.submit(download_file, index, file): index for index, file in enumerate(files)}
        try:
            for future in as_completed(futures):
                paths[futures[future]] = future.result()
        except BaseException:
            cancelled.set()
            for future in futures:
                future.cancel()
            abort_xet_session()
            raise
    return paths


def decompressed(stack, source, name):
    if name.endswith('.bz2'):
        return stack.enter_context(bz2.BZ2File(source, 'rb'))
    if name.endswith(('.gz', '.tgz')):
        return stack.enter_context(gzip.GzipFile(fileobj=source))
    if name.endswith('.zst'):
        import zstandard
        return stack.enter_context(zstandard.ZstdDecompressor().stream_reader(source))
    return source


def convert_dataset(paths, directory, pawnocchio, environment, reporter, source_files):
    data = directory / 'data'
    data.mkdir()
    config = reporter.job['snapshot']['settings']
    workers = max(1, int(reporter.job['worker_info']['threads']))
    pending = {}
    outputs = {}
    completed_bytes = 0
    count = 0
    reporter.write('Preparing dataset with %d assigned CPU threads.\n' % workers)
    reporter.update(preparation_threads=workers)

    def convert_file(source_path, name, expected_format, number, staged):
        reporter.check()
        target = data / ('%05d.vf' % number)
        lower = name.lower()
        is_viri = lower.endswith(('.vf', '.viri', '.vf.bz2', '.vf.gz', '.vf.zst', '.viri.zst'))
        if not is_viri and not lower.endswith(('.pgn', '.pgn.bz2', '.pgn.gz', '.pgn.zst')):
            raise RuntimeError('Unsupported archive member: %s' % name)
        if expected_format == 'vf' and not is_viri:
            raise RuntimeError('Dataset declares Viriformat but contains PGN: %s' % name)
        temporary = target if is_viri else data / ('%05d.pgn' % number)
        reporter.update(current_file=name[-256:])
        with ExitStack() as stack:
            raw = stack.enter_context(source_path.open('rb'))
            stream = decompressed(stack, raw, lower)
            with temporary.open('xb') as output:
                while chunk := stream.read(4 * 1024 * 1024):
                    reporter.check()
                    with reporter.lock:
                        reporter.expanded_bytes += len(chunk)
                        expanded_bytes = reporter.expanded_bytes
                    if expanded_bytes > reporter.job['dataset']['size'] * config.get('dataset_expansion_factor', 8):
                        raise RuntimeError('Expanded dataset exceeds the schedule expansion budget. Increase dataset_expansion_factor before retrying.')
                    output.write(chunk)
        if staged:
            source_path.unlink()
        parsed_games = None
        broken_games = 0
        if not is_viri:
            arguments = [str(pawnocchio), 'pgntovf', '--input', str(temporary), '--output', str(target)]
            if config['skip_broken_games']:
                arguments.append('--skip-broken-games')
            if config['fill_missing_evals'] != '':
                arguments.extend(['--fill-missing-evals', config['fill_missing_evals']])
            conversion = command(arguments, directory, environment, reporter, cpu_threads=1)
            parsed = re.findall(r'parsed games: (\d+)', conversion)
            broken = re.findall(r'broken games: (\d+)', conversion)
            parsed_games = int(parsed[-1]) if parsed else None
            broken_games = int(broken[-1]) if broken else 0
            temporary.unlink()
        if not target.is_file():
            raise RuntimeError('Conversion did not produce an output file for %s.' % name)
        if target.stat().st_size == 0:
            # A successful conversion can skip every game in an individual file.
            # The dataset-wide check below still requires nonempty output.
            with reporter.lock:
                reporter.metrics['skipped_games'] = reporter.metrics.get('skipped_games', 0) + broken_games
            details = ' (%d broken games skipped)' % broken_games if broken_games else ''
            reporter.write('Skipping %s: conversion produced an empty file%s.\n' % (name, details))
            target.unlink()
            source_path.unlink(missing_ok=True)
            return None
        if config['skip_broken_games']:
            cleaned = target.with_suffix('.clean.vf')
            result = command([str(pawnocchio), 'sanitise', '--input', str(target), '--output', str(cleaned)], directory, environment, reporter, cpu_threads=1)
            matches = re.findall(r'parsed (\d+) games and skipped (\d+) bytes', result)
            if not matches or not cleaned.is_file():
                raise RuntimeError('Sanitisation did not report its result for %s.' % name)
            games, skipped = map(int, matches[-1])
            with reporter.lock:
                reporter.metrics['sanitised_games'] = reporter.metrics.get('sanitised_games', 0) + games
                reporter.metrics['skipped_bytes'] = reporter.metrics.get('skipped_bytes', 0) + skipped
                reporter.metrics['skipped_games'] = reporter.metrics.get('skipped_games', 0) + broken_games + (max(0, parsed_games - games) if parsed_games is not None else 0)
            if skipped:
                discarded = '%d games, ' % max(0, parsed_games - games) if parsed_games is not None else ''
                reporter.write('%s: excluded %s%d invalid bytes; retained %d valid games.\n' % (name, discarded, skipped, games))
            cleaned.replace(target)
            if not games:
                target.unlink()
                source_path.unlink(missing_ok=True)
                return None
        else:
            try:
                command([str(pawnocchio), 'sanitise', '--input', str(target), '--check-only'], directory, environment, reporter, cpu_threads=1)
            except RuntimeError as error:
                raise RuntimeError('Dataset validation failed for %s. Enable Skip invalid games to sanitise the dataset. %s' % (name, error)) from error
        source_path.unlink(missing_ok=True)
        return target

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='dataset-prep') as executor:
        def drain(all_pending=False):
            nonlocal completed_bytes
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            if all_pending:
                done = set(pending)
            for future in done:
                number = pending.pop(future)
                target = future.result()
                if target:
                    outputs[number] = target
                    completed_bytes += target.stat().st_size
                reporter.update(converted_files=len(outputs), converted_bytes=completed_bytes)

        def submit(source_path, name, expected_format, staged=False):
            nonlocal count
            if count >= 100000:
                raise RuntimeError('Dataset archive contains too many members.')
            pending[executor.submit(convert_file, source_path, name, expected_format, count, staged)] = count
            count += 1
            if len(pending) >= workers * 2:
                drain()

        try:
            for index, path in enumerate(paths):
                expected_format = source_files[index].get('format')
                name = path.name.lower()
                if name.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tar.zst')):
                    with ExitStack() as stack:
                        raw = stack.enter_context(path.open('rb'))
                        stream = decompressed(stack, raw, name)
                        archive = stack.enter_context(tarfile.open(fileobj=stream, mode='r|'))
                        for member in archive:
                            reporter.check()
                            if member.isdir():
                                continue
                            if not member.isfile():
                                raise RuntimeError('Dataset archives may contain regular files only.')
                            staged = data / ('source-%05d' % count)
                            with archive.extractfile(member) as source, staged.open('xb') as output:
                                while chunk := source.read(4 * 1024 * 1024):
                                    reporter.check()
                                    output.write(chunk)
                            submit(staged, member.name, expected_format, True)
                    path.unlink()
                else:
                    submit(path, name, expected_format)
                reporter.update(progress=100 * (index + 1) / len(paths))
            while pending:
                drain()
        except BaseException as error:
            if not isinstance(error, Stopped):
                reporter.abort_reason = str(error)
            reporter.stop.set()
            for future in pending:
                future.cancel()
            raise
    if not outputs:
        raise RuntimeError('No valid training games were found in the dataset.')
    return [outputs[number] for number in sorted(outputs)]


def save_outputs(connection, job, directory, reporter):
    settings = job['snapshot']['settings']
    outputs = directory / 'outputs'
    uploaded = reporter.checkpoints.uploaded_networks if reporter.checkpoints else set()
    networks = sorted(path for path in outputs.glob(settings['network_glob']) if path.is_file() and path.resolve() not in uploaded)
    if (not networks and not uploaded and not (job.get('workload') or {}).get('finalize_only')) or len(networks) > 250:
        raise RuntimeError('Expected between 1 and 250 network outputs matching %s.' % settings['network_glob'])
    artifacts = []
    for index, path in enumerate(networks):
        if not path.resolve().is_relative_to(outputs.resolve()) or path.is_symlink():
            raise RuntimeError('Network outputs must stay inside the output directory.')
        size = path.stat().st_size
        if not settings['network_min_bytes'] <= size <= settings['network_max_bytes']:
            raise RuntimeError('Network output size is outside the schedule limits: %s' % path.name)
        name = '%03d-%s' % (index, re.sub(r'[^A-Za-z0-9_.-]', '_', path.name)[-80:])
        artifacts.append({'path': path, 'name': name, 'kind': 'network', 'sha256': sha256_file(path), 'size': size})
    manifest = {**job, 'checkpoints': reporter.checkpoints.records if reporter.checkpoints else [], 'artifacts': [{key: str(value) if key == 'path' else value for key, value in item.items()} for item in artifacts]}
    manifest_path = directory / 'manifest.json'
    write_json(manifest_path, manifest)
    reporter.write('Training finished; saving %d networks and the run manifest.\n' % len(networks))
    final_log = directory / 'final-log.txt'
    shutil.copyfile(reporter.log_path, final_log)
    artifacts.extend([{'path': manifest_path, 'name': 'manifest.json', 'kind': 'manifest'}, {'path': final_log, 'name': 'worker.log', 'kind': 'log'}])
    for index, item in enumerate(artifacts):
        reporter.check()
        path = item['path']
        digest = item.get('sha256') or sha256_file(path)
        for attempt in range(5):
            try:
                with path.open('rb') as source:
                    result = connection.request('POST', '%d/artifacts/' % job['id'], params={'name': item['name'], 'kind': item['kind'], 'sha256': digest}, data=source, timeout=(15, 300)).json()
                if result['sha256'] != digest or result['size'] != path.stat().st_size:
                    raise RuntimeError('Server artifact acknowledgement does not match.')
                break
            except (requests.RequestException, RuntimeError):
                if attempt == 4:
                    raise RuntimeError('Artifact upload failed.') from None
                reporter.stop.wait(min(30, 2 ** attempt))
                reporter.check()
        reporter.update(progress=100 * (index + 1) / len(artifacts))


def execute(connection, job, root, pawnocchio):
    if job.get('workload'):
        connection = Connection(connection.server, connection.headers['X-Training-Worker'], connection.headers['Authorization'].removeprefix('Bearer '))
        connection.headers['X-Training-Claim'] = job['workload']['claim_token']
    directory = root / str(job['id'])
    cache_root = root / 'cache'
    training_cache.evict(cache_root, job.get('required_disk_bytes', (job['snapshot']['settings'].get('disk_reserve_gb', 10) * 1024 ** 3) + job['dataset'].get('size', 0)))
    directory.mkdir(exist_ok=False)
    reporter = None
    previous_handlers = {}
    interrupted = threading.Event()
    dataset_caches = []
    build_cache_key = None
    build_ready = False
    def interrupt(*args):
        interrupted.set()
        if reporter:
            reporter.stop.set()
    try:
        reporter = Reporter(connection, job, directory)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupt)
        environment = child_environment(directory, job)
        if job['snapshot']['settings']['backend'] == 'cuda' and not job['worker_info'].get('execution_image'):
            environment.update({key: value for key, value in job['snapshot'].get('environment', {}).items() if key in ('CUDA_PATH', 'CUDA_HOME')})
            environment = cuda_build_environment(environment)
            reporter.write('Using CUDA toolkit: %s\n' % environment['CUDA_PATH'])
        if environment.get('CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER'):
            reporter.write('Using Microsoft linker: %s\n' % environment['CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER'])
        source_dataset = job['dataset']
        resume_superbatch = job['snapshot'].get('resume', {}).get('superbatch', 0)
        if not (job.get('workload') or {}).get('finalize_only') and source_dataset.get('stages') and all(stage.get('end', float('inf')) <= resume_superbatch for stage in source_dataset['stages']):
            raise RuntimeError('The checkpoint is already at or beyond the end of the schedule. Choose an earlier checkpoint or extend the schedule.')
        resume_directory = download_checkpoint(connection, job, directory, reporter)
        if (job.get('workload') or {}).get('finalize_only'):
            (directory / 'outputs').mkdir()
            for state in ('CONVERTING', 'COMPILING', 'TRAINING', 'SAVING'):
                reporter.stage(state)
            reporter.write('Final checkpoint already committed; completing remaining artifacts.\n')
            save_outputs(connection, job, directory, reporter)
            reporter.stage('COMPLETED')
            return False
        stage_data = []
        stage_statistics = []
        prepared_stages = {}
        data = []
        for index, stage in enumerate(source_dataset.get('stages') or [source_dataset]):
            if stage.get('end', float('inf')) <= resume_superbatch or stage.get('start', 1) > (job.get('workload') or {}).get('end', float('inf')):
                stage_data.append([])
                stage_statistics.append({})
                continue
            indices = stage.get('file_indices', list(range(len(source_dataset['files']))))
            key = tuple(indices)
            if key in prepared_stages:
                prepared, statistics = prepared_stages[key]
                stage_data.append(prepared)
                stage_statistics.append(statistics)
                continue
            stage_directory = directory / ('stage-%d' % index)
            stage_directory.mkdir()
            stage_connection = StageConnection(connection, index) if source_dataset.get('stages') else connection
            stage_job = {**job, 'dataset': {**stage, 'files': [source_dataset['files'][i] for i in indices]}}
            plan = stage_connection.request('POST', '%d/dataset/prepare/' % job['id'], json={}).json()
            reporter.write('Dataset stage %d remaining steps: %s.\n' % (index + 1, ', '.join(plan['steps']) or 'none'))
            dataset_cache_key = training_cache.key({'source': plan['source_key'], 'settings': job['snapshot']['settings'], 'tools': [PAWNOCCHIO_REF, COMBINER_REF]})
            cached = training_cache.take(cache_root / 'datasets', dataset_cache_key, stage_directory) if job.get('workload') else None
            if cached:
                if reporter.state == 'DOWNLOADING':
                    reporter.stage('CONVERTING')
                prepared = [stage_directory / item['path'] for item in cached['files']]
                statistics = cached['metadata'].get('statistics', {})
                prepared_stages[key] = (prepared, statistics)
                stage_data.append(prepared)
                stage_statistics.append(statistics)
                data.extend(prepared)
                dataset_caches.append((dataset_cache_key, stage_directory, prepared, statistics))
                reporter.write('Reusing verified dataset preparation for stage %d.\n' % (index + 1))
                continue
            paths = download_dataset(stage_connection, stage_job, stage_directory, reporter)
            if reporter.state == 'DOWNLOADING':
                reporter.stage('CONVERTING')
            if 'convert' in plan['steps'] or 'extract' in plan['steps']:
                paths = convert_dataset(paths, stage_directory, pawnocchio, environment, reporter, stage_job['dataset']['files'])
            prepared = prepare_and_publish(stage_connection, stage_job, plan, paths, stage_directory, pawnocchio, environment, reporter, command)
            statistics = stage_job.get('dataset_statistics', {})
            prepared_stages[key] = (prepared, statistics)
            stage_data.append(prepared)
            stage_statistics.append(statistics)
            data.extend(prepared)
            if job.get('workload'):
                dataset_caches.append((dataset_cache_key, stage_directory, prepared, statistics))
        job['dataset_statistics'] = {'stages': stage_statistics, 'games': sum(item.get('games', 0) for _, item in prepared_stages.values())}
        reporter.stage('COMPILING')
        repository = directory / 'bullet'
        snapshot = job['snapshot']
        build_cache_key = training_cache.key({'commit': snapshot['bullet_commit'], 'source': snapshot['files'], 'settings': snapshot['settings'], 'environment': snapshot.get('environment', {}), 'runtime': job['worker_info']['runtime'], 'backend': job['worker_info']['backend']})
        cached_build = training_cache.take(cache_root / 'builds', build_cache_key, repository) if job.get('workload') else None
        if not cached_build:
            command(['git', '-c', 'init.defaultBranch=main', 'init', str(repository)], directory, environment, reporter, trusted=True)
            command(['git', '-C', str(repository), 'remote', 'add', 'origin', snapshot['settings']['bullet_repo']], directory, environment, reporter, trusted=True)
            command(['git', '-C', str(repository), 'fetch', '--depth', '1', 'origin', snapshot['bullet_commit']], directory, environment, reporter, trusted=True)
            command(['git', '-C', str(repository), 'checkout', '--detach', 'FETCH_HEAD'], directory, environment, reporter, trusted=True)
        for name, source in snapshot['files'].items():
            destination = repository / name
            if not destination.resolve().is_relative_to(repository.resolve()) or '.git' in Path(name).parts:
                raise RuntimeError('Schedule file escapes the repository.')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source, encoding='utf-8')
        schedule_settings = snapshot['settings']
        if schedule_settings.get('example_name'):
            manifest = repository / schedule_settings['example_manifest']
            if not manifest.resolve().is_relative_to(repository.resolve()):
                raise RuntimeError('Cargo manifest escapes the repository.')
            document = tomllib.loads(manifest.read_text(encoding='utf-8'))
            example = next((item for item in document.get('example', []) if item.get('name') == schedule_settings['example_name']), None)
            example_source = repository / schedule_settings['example_source']
            if example and (manifest.parent / example.get('path', '')).resolve() != example_source.resolve():
                raise RuntimeError('The Cargo example name already points to another source file. Change the example name in the schedule settings.')
            if not example:
                relative_source = os.path.relpath(example_source, manifest.parent).replace(os.sep, '/')
                with manifest.open('a', encoding='utf-8') as output:
                    output.write('\n[[example]]\nname = %s\npath = %s\n' % (json.dumps(schedule_settings['example_name']), json.dumps(relative_source)))
        (directory / 'outputs').mkdir()
        config = {**job['parameters'], 'net_name': job['name'], 'engine': job['engine'], 'data_files': [str(path) for path in data], 'output_directory': str(directory / 'outputs'), 'threads': snapshot['settings']['threads']}
        if resume_directory:
            config.update(resume_checkpoint_dir=str(resume_directory), start_superbatch=snapshot['resume']['superbatch'] + 1, resume_metadata=snapshot['resume']['metadata'])
            environment['MATTBENCH_RESUME_DIR'] = str(resume_directory)
            environment['MATTBENCH_START_SUPERBATCH'] = str(config['start_superbatch'])
        if job.get('workload'):
            config.update(start_superbatch=job['workload']['start'], end_superbatch=job['workload']['end'])
            environment['MATTBENCH_START_SUPERBATCH'] = str(config['start_superbatch'])
            environment['MATTBENCH_END_SUPERBATCH'] = str(config['end_superbatch'])
        (directory / 'job.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
        (directory / 'data-files.txt').write_text('\n'.join(str(path) for path in data), encoding='utf-8')
        environment['MATTBENCH_DATA_FILES'] = str(directory / 'data-files.txt')
        for index, prepared in enumerate(stage_data):
            stage_list = directory / ('stage-%d-files.txt' % index)
            stage_list.write_text('\n'.join(str(path) for path in prepared), encoding='utf-8')
            environment['MATTBENCH_STAGE_%d_FILES' % index] = str(stage_list)
        lockfile = repository / 'Cargo.lock'
        if not lockfile.is_file():
            raise RuntimeError('The pinned training source must include Cargo.lock.')
        job['build_provenance'] = {'cargo_lock_sha256': sha256_file(lockfile), 'runtime': job['worker_info']['runtime'], 'resume_semantics': snapshot['resume_semantics']}
        build = list(snapshot['settings']['build'])
        if job['worker_info'].get('execution_image'):
            command(['sh', '-c', 'mkdir -p "$1" && cp -a /opt/cargo/. "$1"/', 'cache', environment['CARGO_HOME']], repository, environment, reporter)
        if build[0] == 'cargo' and '--locked' not in build and '--frozen' not in build:
            build.append('--frozen' if job['worker_info'].get('execution_image') else '--locked')
        environment.update(snapshot.get('environment', {}))
        if snapshot['settings']['backend'] == 'rocm':
            overrides = {key: value for key, value in environment.items() if key.startswith('HSA_OVERRIDE_GFX_VERSION') and rocm_environment_variable(key)}
            if platform.system() == 'Linux' and not overrides and re.search(r'\b(?:gfx1032|RX\s*6600(?:\s*XT)?)\b', job['worker_info']['gpu'], re.I):
                environment['HSA_OVERRIDE_GFX_VERSION'] = '10.3.0'
                overrides['HSA_OVERRIDE_GFX_VERSION'] = '10.3.0'
            for key, value in sorted(overrides.items()):
                reporter.write('ROCm architecture override for %s: %s=%s\n' % (job['worker_info']['gpu'], key, value))
        if cached_build:
            reporter.write('Reusing verified trainer build.\n')
        else:
            command(build, repository, environment, reporter)
        if sha256_file(lockfile) != job['build_provenance']['cargo_lock_sha256']:
            raise RuntimeError('The build changed Cargo.lock. Pin the resolved dependency lockfile in the schedule before retrying.')
        build_ready = True
        reporter.stage('TRAINING')
        reporter.checkpoints = CheckpointUploader(connection, job, directory, reporter)
        command(snapshot['settings']['run'], repository, environment, reporter)
        reporter.stage('SAVING')
        reporter.checkpoints.finish()
        if not job.get('workload'):
            for index in range(len(stage_data)):
                remove_work_directory(directory / ('stage-%d' % index), directory)
        save_outputs(connection, job, directory, reporter)
        reporter.stage('COMPLETED')
    except Exception as error:
        if reporter is None:
            raise
        recoverable = interrupted.is_set() or isinstance(error, requests.RequestException) or str(error).startswith('Server unavailable') or (reporter.abort_reason or '').startswith(('Server unavailable', 'The server marked this run failed'))
        if interrupted.is_set():
            error = RuntimeError('Worker interrupted. Training will recover automatically when the worker reconnects.')
        elif reporter.abort_reason:
            error = RuntimeError(reporter.abort_reason)
        elif reporter.stop.is_set():
            error = Stopped('Training stopped by the server or its owner.')
        message = str(error) if isinstance(error, (RuntimeError, Stopped)) else '%s during %s.' % (type(error).__name__, reporter.state.lower())
        reporter.write('\n' + message + '\n')
        with reporter.lock:
            reporter.error = message[:2048]
            reporter.state = 'CANCELLED' if isinstance(error, Stopped) else 'FAILED'
            if recoverable:
                reporter.metrics['recovery_pending'] = 1
                job['recovery_pending'] = True
        try:
            for attempt in range(3):
                reporter.flush()
                if reporter.ack_state == reporter.state:
                    break
        except Exception:
            print('Could not report the failure. MattBench will mark the run failed after its heartbeat timeout.')
    finally:
        try:
            if reporter:
                try:
                    if reporter.checkpoints:
                        reporter.stop.set()
                        reporter.checkpoints.close()
                finally:
                    reporter.close()
        finally:
            from huggingface_hub.utils._xet import abort_xet_session
            abort_xet_session()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            stop_previous(directory)
            if job.get('workload'):
                for cache_key, stage_directory, prepared, statistics in dataset_caches:
                    training_cache.put(cache_root / 'datasets', cache_key, stage_directory, prepared, {'statistics': statistics})
                if build_ready and (directory / 'bullet').exists():
                    repository = directory / 'bullet'
                    executable = repository / job['snapshot']['settings']['run'][0]
                    training_cache.put(cache_root / 'builds', build_cache_key, repository, [executable, repository / 'Cargo.lock'])
            remove_work_directory(directory, root)
            remove_work_directory(root / 'transfer-cache', root)
    return interrupted.is_set()


class WorkerSession:
    def __init__(self, args, connection, registration, identity_path, directory_lock):
        self.args = args
        self.connection = connection
        self.registration = registration
        self.identity_path = identity_path
        self.directory_lock = directory_lock
        self.pending_test = None
        self.settings = {}

    def close(self):
        self.directory_lock.close()

    def poll(self, completed_workload=0, blacklist=None):
        cleanup_runs(self.args.directory)
        result = self.connection.request('POST', 'claim/', json={'disk_gb': (shutil.disk_usage(self.args.directory).free + training_cache.reclaimable_bytes(self.args.directory / 'cache')) / 1024 ** 3, 'claim_id': self.registration['claim_id'], 'machine_idle': self.args.unified, 'completed_workload': completed_workload, 'blacklist': blacklist or []}).json()
        self.settings = result.get('settings', {})
        if result.get('claim_finished'):
            self.registration['claim_id'] = str(uuid.uuid4())
            write_json(self.identity_path, self.registration)
            return True
        self.pending_test = result.get('assignment', {}).get('workload') if result.get('assignment', {}).get('type') == 'test' else None
        if self.pending_test:
            self.registration['claim_id'] = str(uuid.uuid4())
            write_json(self.identity_path, self.registration)
        if not result['run']:
            return False
        job = result['run']
        interrupted = False
        executed = False
        if job['state'] == 'DOWNLOADING' and (job.get('workload', {}).get('report_sequence', 0) if job.get('workload') else job['report_sequence']) == 0 and not job['cancel_requested']:
            executed = True
            interrupted = execute(self.connection, job, self.args.directory, self.args.pawnocchio)
        elif job['state'] not in ('COMPLETED', 'CANCELLED'):
            recovery_connection = Connection(self.connection.server, self.connection.headers['X-Training-Worker'], self.connection.headers['Authorization'].removeprefix('Bearer '))
            if job.get('workload'):
                recovery_connection.headers['X-Training-Claim'] = job['workload']['claim_token']
            recovery_connection.request('POST', '%d/recover/' % job['id'], json={})
        if not interrupted and not (executed and job.get('recovery_pending')):
            self.registration['claim_id'] = str(uuid.uuid4())
            write_json(self.identity_path, self.registration)
        if interrupted:
            raise SystemExit()
        return True


def register_worker(server, payload, registration, identity_path):
    try:
        for attempt in range(2):
            response = requests.post(server.rstrip('/') + '/api/training/register/', data=payload, timeout=30)
            response.request.body = None
            if response.status_code < 400:
                response.raise_for_status()
                return
            try:
                details = response.json()
            except ValueError:
                details = {}
            message = details.get('error') if isinstance(details, dict) else None
            if attempt == 0 and response.status_code == 403 and message == 'Worker identity is revoked or belongs to another account.':
                replacement = {'server': server.rstrip('/'), 'worker': str(uuid.uuid4()), 'token': secrets.token_urlsafe(48), 'claim_id': str(uuid.uuid4()), 'registered': False}
                write_json(identity_path.with_name(identity_path.name + '.rejected'), registration)
                write_json(identity_path, replacement)
                registration.clear()
                registration.update(replacement)
                payload.update(worker=registration['worker'], token=registration['token'])
                print('Saved training identity was rejected. Registering a new identity for this authenticated session.')
                continue
            raise RuntimeError('Training registration rejected by %s (HTTP %d): %s' % (server, response.status_code, message or 'Server returned no error details.'))
    finally:
        payload.clear()


def main(argv=None, worker_config=None, gpu_info=None):
    parser = argparse.ArgumentParser(description='MattBench single-worker NNUE training. Runs schedules belonging to your account; use a dedicated worker account on the GPU host.')
    server = os.environ.pop('OPENBENCH_SERVER', None)
    parser.add_argument('--server', default=server, required=not server)
    parser.add_argument('--username', default=os.environ.pop('OPENBENCH_USERNAME', None))
    parser.add_argument('--name', default=socket.gethostname())
    parser.add_argument('--directory', type=Path, default=Path('training-work'))
    parser.add_argument('--pawnocchio-repo', default=os.environ.pop('MATTBENCH_PAWNOCCHIO_REPO', PAWNOCCHIO_REPO))
    parser.add_argument('--pawnocchio-ref', default=os.environ.pop('MATTBENCH_PAWNOCCHIO_REF', PAWNOCCHIO_REF), help='Pinned Pawnocchio commit to fetch and build.')
    parser.add_argument('--combiner-repo', default=os.environ.pop('MATTBENCH_COMBINER_REPO', COMBINER_REPO))
    parser.add_argument('--combiner-ref', default=os.environ.pop('MATTBENCH_COMBINER_REF', COMBINER_REF), help='Pinned Pawnocchio Viriformat combiner commit to fetch and build.')
    parser.add_argument('--backend', choices=('cuda', 'rocm'), default=os.environ.get('MATTBENCH_TRAINING_BACKEND'))
    parser.add_argument('--device', default=os.environ.get('MATTBENCH_TRAINING_DEVICE'), help='GPU index or UUID; defaults to the first visible GPU')
    parser.add_argument('--gpu-name', default=os.environ.get('MATTBENCH_TRAINING_GPU_NAME'))
    parser.add_argument('--vram-gb', type=float, default=os.environ.get('MATTBENCH_TRAINING_VRAM_GB'))
    parser.add_argument('--threads', type=int, default=os.cpu_count() or 1)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--mode', choices=('automatic', 'testing-only', 'training-only', 'paused'), default='automatic', help='Initial mode for a new worker')
    parser.add_argument('--register-only', action='store_true', help='Persist worker credentials, then exit without claiming a run.')
    parser.add_argument('--high-performance-transfers', '--high-performance-downloads', dest='high_performance_downloads', action='store_true', help='Enable Xet high-performance uploads and downloads; intended for high bandwidth and at least 64 GB RAM.')
    parser.add_argument('--execution-image', default='', help='Optional Linux execution image pinned as repository@sha256:digest. Omit to run natively. Include Rust, GPU libraries and cached Cargo dependencies.')
    parser.add_argument('--memory-gb', type=int, default=max(1, int(psutil.virtual_memory().total / 1024 ** 3 * 0.8)))
    args = parser.parse_args(argv)
    if args.backend not in (None, 'cuda', 'rocm'):
        parser.error('MATTBENCH_TRAINING_BACKEND must be cuda or rocm.')
    args.unified = worker_config is not None
    parsed = urlsplit(args.server)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        parser.error('Use HTTPS for the server URL (HTTP is allowed on localhost).')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('Use a server URL without credentials, query or fragment.')
    args.directory = args.directory.resolve()
    args.directory.mkdir(parents=True, exist_ok=True)
    directory_lock = worker_lock(args.directory)
    identity_path, registration = identity(args.directory, args.server)
    cleanup_runs(args.directory)
    os.environ['HF_HOME'] = str(args.directory / 'transfer-cache')
    os.environ['HF_XET_CACHE'] = str(args.directory / 'transfer-cache' / 'xet')
    os.environ['HF_XET_LOG_DEST'] = os.devnull
    os.environ['HF_XET_CHUNK_CACHE_SIZE_BYTES'] = '0'
    if args.execution_image:
        if os.name == 'nt' or not re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', args.execution_image) or not shutil.which('docker'):
            parser.error('Isolated execution requires Linux, Docker and an image pinned by SHA256 digest.')
        if os.getuid() == 0:
            parser.error('Run the worker as a dedicated non-root account.')
        subprocess.run(['docker', 'image', 'inspect', args.execution_image], check=True, stdout=subprocess.DEVNULL, timeout=30)
    if args.memory_gb < 1:
        parser.error('Memory limit must be positive.')
    for tool in (('git',) if args.execution_image else ('git', 'cargo', 'rustc')):
        if not shutil.which(tool):
            parser.error('%s is required on this worker.' % tool)
    gpu_name, vram = args.gpu_name, args.vram_gb
    driver_version = 'kernel-' + platform.release()
    if gpu_info is None:
        try:
            gpu_info = detect_gpu(args.backend, args.device)
        except RuntimeError as error:
            if not args.backend or not gpu_name or not vram:
                parser.error(str(error))
            print('%s Using explicitly supplied GPU details.' % error)
    if gpu_info:
        args.backend = gpu_info['backend']
        args.device = gpu_info['device']
        gpu_name = gpu_name if gpu_name is not None else gpu_info['gpu']
        vram = vram if vram is not None else gpu_info['vram_gb']
        driver_version = gpu_info['driver']
        print('Detected %s GPU: %s (%.2f GB VRAM).' % (args.backend.upper(), gpu_info['gpu'], gpu_info['vram_gb']))
    visibility = 'CUDA_VISIBLE_DEVICES' if args.backend == 'cuda' else 'HIP_VISIBLE_DEVICES'
    args.device = args.device or os.environ.get(visibility, '0').split(',')[0].strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.device):
        parser.error('Choose a single GPU index or UUID.')
    os.environ[visibility] = args.device
    if not gpu_name or not vram or vram <= 0 or args.threads <= 0:
        parser.error('GPU name, positive VRAM and positive threads are required.')
    if args.high_performance_downloads:
        os.environ['HF_XET_HIGH_PERFORMANCE'] = '1'
    for name in ('HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN'):
        os.environ.pop(name, None)
    try:
        dataset_tools, tool_provenance = build_dataset_tools(args.directory, args.pawnocchio_repo, args.pawnocchio_ref, args.combiner_repo, args.combiner_ref, args.execution_image, args.threads, args.memory_gb)
    except (OSError, RuntimeError, ValueError, requests.RequestException, subprocess.SubprocessError) as error:
        parser.error('Dataset tool setup failed: %s' % error)
    args.pawnocchio = dataset_tools['pawnocchio']
    import getpass
    username = args.username or registration.get('username') or input('MattBench username: ')
    info = {'protocol': 4, 'capabilities': ['training-workloads', 'typed-assignments'], 'gpu': gpu_name, 'device': args.device, 'backend': args.backend, 'vram_gb': vram, 'threads': args.threads, 'disk_gb': shutil.disk_usage(args.directory).free / 1024 ** 3, 'pawnocchio_sha256': sha256_file(args.pawnocchio), 'pawnocchio_path': str(args.pawnocchio), 'execution_image': args.execution_image, 'memory_gb': args.memory_gb, 'runtime': runtime_info(args.pawnocchio, args.execution_image)}
    info['combiner_path'] = str(dataset_tools['combiner'])
    info['runtime']['dataset_tools'] = tool_provenance
    info['runtime']['gpu_driver'] = driver_version.strip()
    if not registration['registered'] or worker_config is not None:
        payload = {'username': username, 'name': args.name, 'info': json.dumps(info), 'worker': registration['worker'], 'token': registration['token'], 'mode': args.mode}
        if worker_config is not None:
            payload.update(machine_id=worker_config.machine_id, machine_secret=worker_config.secret_token)
        else:
            payload['password'] = os.environ.pop('OPENBENCH_PASSWORD', None) or getpass.getpass('MattBench password: ')
        register_worker(args.server, payload, registration, identity_path)
        registration.update(registered=True, username=username, info=info)
        write_json(identity_path, registration)
    elif registration['info'] != info:
        previous = {key: value for key, value in registration['info'].items() if key != 'disk_gb'}
        current = {key: value for key, value in info.items() if key != 'disk_gb'}
        if previous != current:
            parser.error('Worker capabilities changed. Use a new worker directory to register this runtime.')
    connection = Connection(args.server, registration['worker'], registration['token'])
    if worker_config is not None:
        return WorkerSession(args, connection, registration, identity_path, directory_lock)
    if args.register_only:
        print('Worker credentials saved in %s.' % identity_path)
        return
    print('Connected %s (%s, %.1f GB). Waiting for training assigned to %s.' % (args.name, gpu_name, vram, username))
    while True:
        try:
            cleanup_runs(args.directory)
            result = connection.request('POST', 'claim/', json={'disk_gb': shutil.disk_usage(args.directory).free / 1024 ** 3, 'claim_id': registration['claim_id']}).json()
            if result['run']:
                job = result['run']
                interrupted = False
                executed = False
                if job['state'] == 'DOWNLOADING' and (job.get('workload', {}).get('report_sequence', 0) if job.get('workload') else job['report_sequence']) == 0 and not job['cancel_requested']:
                    executed = True
                    interrupted = execute(connection, job, args.directory, args.pawnocchio)
                elif job['state'] not in ('COMPLETED', 'CANCELLED'):
                    connection.request('POST', '%d/recover/' % job['id'], json={})
                if not interrupted and not (executed and job.get('recovery_pending')):
                    registration['claim_id'] = str(uuid.uuid4())
                    write_json(identity_path, registration)
                if args.once or interrupted:
                    return
        except (requests.RequestException, RuntimeError) as error:
            print('Worker connection failed (%s). Retrying in 15 seconds.' % type(error).__name__)
        time.sleep(15)


if __name__ == '__main__':
    main()
