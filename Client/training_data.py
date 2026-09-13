import hashlib
import json
import mmap
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from tempfile import TemporaryDirectory
from pathlib import Path
import requests


class UploadStream:
    def __init__(self, source, size, progress):
        self.source = source
        self.size = size
        self.remaining = size
        self.progress = progress

    def __len__(self):
        return self.size

    def read(self, size=-1):
        amount = self.remaining if size < 0 else min(size, self.remaining)
        data = self.source.read(min(amount, 1024 ** 2))
        if self.remaining and not data:
            raise RuntimeError('Prepared dataset file changed during upload.')
        self.remaining -= len(data)
        self.progress(len(data))
        return data


def upload_prepared_files(connection, job, paths, descriptors, reporter):
    if not paths or len(paths) != len(descriptors):
        raise RuntimeError('Prepared upload files are missing.')
    workers = int(os.environ.get('MATTBENCH_HF_UPLOAD_WORKERS', '8'))
    if not 1 <= workers <= 32:
        raise RuntimeError('MATTBENCH_HF_UPLOAD_WORKERS must be between 1 and 32.')
    endpoint = '%d/dataset/prepare/upload/' % job['id']
    lock = threading.Lock()
    stopped = threading.Event()
    sent = [0] * len(paths)
    completed = set()
    total = sum(file['size'] for file in descriptors)

    def check():
        reporter.check()
        if stopped.is_set():
            raise RuntimeError('Dataset upload stopped after another shard failed.')

    def report(index, amount):
        check()
        with lock:
            sent[index] = min(descriptors[index]['size'], sent[index] + amount)
            uploaded = sum(sent)
            reporter.update(current_file='Uploading prepared dataset: %d/%d files confirmed' % (len(completed), len(paths)),
                            uploaded_bytes=uploaded, upload_total_bytes=total, uploaded_files=len(completed),
                            progress=100 * uploaded / max(1, total))

    def upload(index):
        descriptor = descriptors[index]
        for attempt in range(5):
            check()
            with lock:
                sent[index] = 0
            try:
                instruction = connection.request('POST', endpoint, json=descriptor, timeout=(15, 90)).json()
                if not instruction.get('stored'):
                    action = instruction['upload']
                    header = action.get('header', {})
                    chunk_size = int(header.get('chunk_size', descriptor['size']))
                    if chunk_size <= 0:
                        raise RuntimeError('Invalid upload chunk size.')
                    parts = []
                    with paths[index].open('rb') as source:
                        if os.fstat(source.fileno()).st_size != descriptor['size']:
                            raise RuntimeError('Prepared dataset file changed during upload.')
                        for part, offset in enumerate(range(0, descriptor['size'], chunk_size), 1):
                            check()
                            size = min(chunk_size, descriptor['size'] - offset)
                            stream = UploadStream(source, size, lambda amount: report(index, amount))
                            url = header[str(part)] if 'chunk_size' in header else action['href']
                            upload_headers = {} if 'chunk_size' in header else dict(header)
                            with requests.put(url, data=stream, headers=upload_headers, timeout=(15, 60)) as response:
                                response.raise_for_status()
                                if stream.remaining:
                                    raise RuntimeError('Upload ended before all file bytes were read.')
                                if 'chunk_size' in header:
                                    etag = response.headers.get('etag')
                                    if not etag:
                                        raise RuntimeError('Multipart upload returned no ETag.')
                                    parts.append({'partNumber': part, 'etag': etag})
                    if parts:
                        with requests.post(action['href'], json={'oid': descriptor['sha256'], 'parts': parts},
                                           headers={'Content-Type': 'application/vnd.git-lfs+json'}, timeout=(15, 60)) as response:
                            response.raise_for_status()
                    check()
                    verified = connection.request('POST', endpoint, json={**descriptor, 'verify': True}, timeout=(15, 90)).json()
                    if not verified.get('stored'):
                        raise RuntimeError('Uploaded dataset file was not acknowledged.')
                with lock:
                    completed.add(index)
                    sent[index] = descriptor['size']
                report(index, 0)
                reporter.write('Uploaded prepared shard %s (%d/%d).\n' % (descriptor['path'], len(completed), len(paths)))
                return
            except (requests.RequestException, RuntimeError, ValueError, KeyError) as error:
                check()
                if attempt == 4:
                    raise RuntimeError('Prepared shard upload failed after five attempts: %s (%s). Local files were retained.' % (descriptor['path'], type(error).__name__)) from None
                reporter.write('Retrying prepared shard %s after %s (attempt %d/5).\n' % (descriptor['path'], type(error).__name__, attempt + 2))
                reporter.stop.wait(min(30, 2 ** attempt))
    report(0, 0)
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(paths))), thread_name_prefix='dataset-upload') as executor:
        futures = [executor.submit(upload, index) for index in range(len(paths))]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            stopped.set()
            for future in futures:
                future.cancel()
            raise


def file_digest(path, reporter):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
            reporter.check()
            digest.update(chunk)
    return digest.hexdigest()


def converted_shards(paths, destination, reporter, shard_bytes=512 * 1024 ** 2):
    destination.mkdir()
    shards = []
    output = None
    size = 0
    games = 0
    try:
        for path in paths:
            with path.open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
                for offset, length in game_ranges(mapping):
                    if games % 4096 == 0:
                        reporter.check()
                    games += 1
                    if output is None or size + length > shard_bytes:
                        if output:
                            output.close()
                        target = destination / ('%05d.vf' % len(shards))
                        output = target.open('wb')
                        shards.append(target)
                        size = 0
                        if len(shards) > 4096:
                            raise RuntimeError('Dataset exceeds 4096 shards.')
                    output.write(mapping[offset:offset + length])
                    size += length
            path.unlink()
    finally:
        if output:
            output.close()
    return shards


def game_ranges(data):
    offset = 0
    while offset < len(data):
        if offset + 36 > len(data):
            raise RuntimeError('Truncated Viriformat game header.')
        limit = min(len(data), offset + 4 * 1024 ** 2)
        end = data.find(b'\0\0\0\0', offset + 32, limit)
        while end >= 0 and (end - offset - 32) % 4:
            end = data.find(b'\0\0\0\0', end + 1, limit)
        if end < 0:
            raise RuntimeError('Viriformat game has no terminator within the supported size limit.')
        size = end + 4 - offset
        yield offset, size
        offset += size


def merge_games(runs, destination, seed, combiner, environment, reporter, command):
    inputs = [path for path, _ in runs]
    expected_size = sum(path.stat().st_size for path in inputs)
    command([str(combiner), '--interleave', '--seed', str(seed), str(destination), *map(str, inputs)], destination.parent, environment, reporter, cpu_threads=1)
    if not destination.is_file() or destination.stat().st_size != expected_size:
        raise RuntimeError('Pawnocchio interleaving did not preserve dataset size. Check the inputs for invalid games.')


def order_games(paths, destination, config, reporter, shuffle, combiner, environment, command):
    destination.mkdir()
    rng = random.Random(config.get('shuffle_seed', 42))
    memory_bytes = config.get('shuffle_memory_mb', 256) * 1024 ** 2
    fan_in = config.get('interleave_fan_in', 32)
    shard_bytes = config.get('dataset_shard_mb', 512) * 1024 ** 2
    source_bytes = sum(path.stat().st_size for path in paths)
    total = 0
    with TemporaryDirectory(prefix='ordering-', dir=destination) as temporary:
        work = Path(temporary)
        runs = []
        for path in paths:
            reporter.check()
            if not path.stat().st_size:
                raise RuntimeError('Cannot order an empty Viriformat file.')
            with path.open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
                batch = []
                batch_bytes = 0
                count = 0
                def flush():
                    rng.shuffle(batch)
                    target = work / ('run-%d.vf' % len(runs))
                    with target.open('xb', buffering=1024 * 1024) as output:
                        for index, (offset, size) in enumerate(batch):
                            if index % 4096 == 0:
                                reporter.check()
                            output.write(mapping[offset:offset + size])
                    runs.append((target, len(batch)))
                    batch.clear()
                for offset, size in game_ranges(mapping):
                    if total % 4096 == 0:
                        reporter.check()
                        reporter.update(dataset_games=total)
                    total += 1
                    count += 1
                    if shuffle:
                        if batch and batch_bytes + size + 128 > memory_bytes:
                            flush()
                            batch_bytes = 0
                        batch.append((offset, size))
                        batch_bytes += size + 128
                if shuffle:
                    if batch:
                        flush()
                else:
                    runs.append((path, count))
            if shuffle:
                path.unlink()
        if not total:
            raise RuntimeError('No Viriformat games to order.')
        reporter.update(dataset_games=total)
        generation = 0
        while True:
            groups = []
            group = []
            argument_size = 0
            for run in runs:
                size = len(str(run[0])) + 3
                if group and (len(group) >= fan_in or argument_size + size > 24000):
                    groups.append(group)
                    group = []
                    argument_size = 0
                group.append(run)
                argument_size += size
            groups.append(group)
            if len(groups) == 1:
                break
            merged = []
            def merge_group(item):
                start, group, seed = item
                if len(group) == 1:
                    return group[0]
                reporter.update(current_file='Interleaving merge pass %d, group %d' % (generation + 1, start + 1))
                target = work / ('merge-%d-%d.vf' % (generation, start))
                merge_games(group, target, seed, combiner, environment, reporter, command)
                for path, _ in group:
                    path.unlink()
                return target, sum(count for _, count in group)
            jobs = [(index, group, rng.getrandbits(64)) for index, group in enumerate(groups)]
            with ThreadPoolExecutor(max_workers=max(1, int(reporter.job['worker_info']['threads']))) as executor:
                try:
                    merged = list(executor.map(merge_group, jobs))
                except BaseException as error:
                    if not reporter.stop.is_set():
                        reporter.abort_reason = str(error)
                    reporter.stop.set()
                    raise
            runs = merged
            generation += 1
        reporter.update(current_file='Interleaving complete Viriformat games into output shards')
        target = work / 'interleaved.vf'
        merge_games(runs, target, rng.getrandbits(64), combiner, environment, reporter, command)
        for path, _ in runs:
            path.unlink()
        outputs = converted_shards([target], destination / 'shards', reporter, shard_bytes)
    if sum(path.stat().st_size for path in outputs) != source_bytes:
        raise RuntimeError('Game ordering must preserve dataset size.')
    return outputs, total


def prepare_and_publish(connection, job, plan, paths, directory, pawnocchio, environment, reporter, command):
    config = job['snapshot']['settings']
    steps = plan['steps']
    statistics = dict(plan.get('statistics', {}))
    statistics['source_bytes'] = job['dataset']['size']
    selected = paths
    if any(step in steps for step in ('convert', 'extract')) and not any(step in steps for step in ('shuffle', 'interleave')):
        selected = converted_shards(paths, directory / 'converted', reporter, config.get('dataset_shard_mb', 512) * 1024 ** 2)
    if 'shuffle' in steps or 'interleave' in steps:
        reporter.update(current_file='Preparing Viriformat game order')
        selected, games = order_games(selected, directory / 'ordered', config, reporter, 'shuffle' in steps, job['worker_info']['combiner_path'], environment, command)
        statistics['interleave_tool'] = job['worker_info']['runtime']['dataset_tools']['combiner']
        statistics.update(games=games, shuffle_seed=config.get('shuffle_seed', 42), interleave_algorithm='pawnocchio-viriformat-combiner-v1', interleave_fan_in=config.get('interleave_fan_in', 32), dataset_shard_mb=config.get('dataset_shard_mb', 512))
        if 'shuffle' in steps:
            statistics.update(shuffle_algorithm='viriformat32-chunk-shuffle-pawnocchio-interleave-v3', shuffle_memory_mb=config.get('shuffle_memory_mb', 256))
    statistics.update(converted_bytes=sum(path.stat().st_size for path in selected), converted_files=len(selected))
    if 'analyse' in steps:
        statistics.pop('analysis_kind', None)
        analysis = directory / 'analysis'
        analysis.mkdir()
        def analyse_file(item):
            index, path = item
            target = analysis / str(index)
            target.mkdir()
            result = command([str(pawnocchio), 'analyse', '--inputs', str(path), '--approximate'], target, environment, reporter, cpu_threads=1)
            result = '\n'.join(line for line in result.splitlines() if not line.lstrip().lower().startswith('progress:'))
            return '%s:\n%s' % (path.name, result)
        with ThreadPoolExecutor(max_workers=max(1, int(job['worker_info']['threads']))) as executor:
            try:
                statistics['analysis'] = '\n'.join(executor.map(analyse_file, enumerate(selected)))[:48000]
            except BaseException as error:
                if not reporter.stop.is_set():
                    reporter.abort_reason = str(error)
                reporter.stop.set()
                raise
    statistics.update({key: reporter.metrics.get(key, 0) for key in ('sanitised_games', 'skipped_games', 'skipped_bytes')})
    job['dataset_statistics'] = statistics
    if plan['mode'] != 'prepare':
        return selected
    changed = any(step in steps for step in ('convert', 'extract', 'shuffle', 'interleave'))
    shuffled = 'shuffle' in steps or all(file.get('shuffled', False) for file in job['dataset']['files'])
    interleaved = 'shuffle' in steps or 'interleave' in steps or all(file.get('interleaved', False) for file in job['dataset']['files'])
    if changed:
        descriptors = [{'path': plan['prefix'] + ('%05d.vf' % index), 'size': path.stat().st_size, 'sha256': file_digest(path, reporter), 'format': 'vf', 'shuffled': shuffled, 'interleaved': interleaved} for index, path in enumerate(selected)]
        (directory / 'prepared-upload.json').write_text(json.dumps({'files': descriptors, 'statistics': statistics,
            'paths': [path.relative_to(directory).as_posix() for path in selected]}, indent=2))
        upload_prepared_files(connection, job, selected, descriptors, reporter)
    else:
        descriptors = [{**{key: file[key] for key in ('path', 'size', 'sha256', 'format', 'shuffled')}, 'interleaved': file.get('interleaved', False)} for file in job['dataset']['files']]
    payload = {'files': descriptors, 'statistics': statistics}
    for attempt in range(5):
        reporter.check()
        try:
            result = connection.request('POST', '%d/dataset/prepare/publish/' % job['id'], json=payload, timeout=(15, 180)).json()
            if not result.get('stored'):
                raise RuntimeError('Dataset update was not acknowledged.')
            break
        except (requests.RequestException, RuntimeError):
            if attempt == 4:
                raise
            reporter.stop.wait(min(30, 2 ** attempt))
    job['dataset_publication'] = {'repo': plan['repo'], 'revision': result['revision']}
    reporter.write('Dataset files and dataset.json updated in the source repository.\n')
    return selected
