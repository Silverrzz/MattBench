import hashlib
import mmap
import random
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from pathlib import Path
import requests


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
        from huggingface_hub.utils._xet import get_xet_session
        reporter.update(current_file='Publishing dataset files to Hugging Face')
        def progress(*args):
            reporter.check()
        with get_xet_session().new_upload_commit(token_refresh_url=connection.url('%d/dataset/prepare/token/' % job['id']), token_refresh_headers=connection.headers, custom_headers={}, progress_callback=progress) as commit:
            handles = [commit.start_upload_file(str(path), sha256=descriptor['sha256']) for path, descriptor in zip(selected, descriptors)]
        for handle, descriptor in zip(handles, descriptors):
            if handle.result().xet_info.sha256 != descriptor['sha256']:
                raise RuntimeError('Dataset upload checksum mismatch.')
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
