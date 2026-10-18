import hashlib
import json
import mmap
import os
import random
import re
import signal
import sys
import threading
from pathlib import Path
from urllib.parse import urlencode

try:
    from . import training_worker as worker, training_data, variant_data
    from .dataset_tools import ensure_tools
    from .training_runtime import write_json
except ImportError:
    import training_worker as worker
    import training_data
    import variant_data
    from dataset_tools import ensure_tools
    from training_runtime import write_json


class Connection(worker.Connection):
    def __init__(self, connection):
        self.server, self.headers = connection.server, connection.headers

    def url(self, path):
        return super().url('tasks/' + path.lstrip('/'))


class UploadConnection:
    def __init__(self, connection):
        self.connection = connection

    def request(self, method, path, **kwargs):
        data = kwargs.get('json', {})
        match = re.search(r'/([0-9]{5})\.', data.get('path', ''))
        data['index'] = int(match[1]) if match else -1
        return self.connection.request(method, path.replace('dataset/prepare/upload/', 'upload/instruction/'), **kwargs)


def identity(file):
    return (file.get('provider'), file.get('repo'), file.get('commit'), file.get('artifact'), file.get('workload'), file['path'])


def materialize(value, paths):
    if isinstance(value, list):
        return [materialize(item, paths) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: materialize(item, paths) if isinstance(item, (dict, list)) else item for key, item in value.items()}
    if 'path' in value and 'size' in value and identity(value) in paths:
        result['local_path'] = str(paths[identity(value)])
    return result


def prepare_data(paths, job, directory, environment, reporter, root):
    config = job['snapshot']['settings']
    pawnocchio = ensure_tools(job, root, directory, environment, reporter, worker.command)
    if config['dataset_format'] == 'wf':
        tool = Path(job['worker_info']['wakformat_path'])
        selected = variant_data.convert(paths, directory, tool, environment, reporter, worker.command)
        if job['specification']['config'].get('shuffle', False):
            selected, count = variant_data.order(selected, directory / 'ordered', config, reporter, True)
            reporter.update(dataset_games=count)
        return selected
    selected = worker.convert_dataset(paths, directory, pawnocchio, environment, reporter, job['dataset']['files'])
    if job['specification']['config'].get('shuffle', False):
        selected, count = training_data.order_games(selected, directory / 'ordered', config, reporter, True, Path(job['worker_info']['combiner_path']), environment, worker.command)
        reporter.update(dataset_games=count)
    return selected


def split_data(paths, directory, specification, reporter, format):
    fraction, seed = specification.get('validation_fraction', .1), specification.get('seed', 42)
    rng = random.Random(seed)
    folders = [directory / 'training', directory / 'validation']
    for folder in folders:
        folder.mkdir()
    outputs, streams, sizes, counts = [[], []], [None, None], [0, 0], [0, 0]
    limit = specification.get('dataset_shard_mb', 512) * 1024 ** 2
    def emit(game):
        reporter.check()
        group = int(rng.random() < fraction)
        if streams[group] is None or sizes[group] >= limit:
            if streams[group]:
                streams[group].close()
            path = folders[group] / ('%05d.%s' % (len(outputs[group]), format))
            outputs[group].append(path)
            streams[group] = path.open('wb')
            sizes[group] = 0
            if format == 'wf':
                streams[group].write(variant_data.HEADER)
                sizes[group] = len(variant_data.HEADER)
        streams[group].write(game)
        sizes[group] += len(game)
        counts[group] += 1
    try:
        for path in paths:
            if format == 'wf':
                for game in variant_data.games(path, reporter):
                    emit(game)
            else:
                with path.open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
                    for offset, length in training_data.game_ranges(mapping):
                        emit(mapping[offset:offset + length])
    finally:
        for stream in streams:
            if stream:
                stream.close()
    if any(not count for count in counts):
        raise RuntimeError('The split produced an empty partition. Use more games or a different validation fraction.')
    reporter.update(training_games=counts[0], validation_games=counts[1])
    return outputs


def save_files(connection, job, paths, port, format, reporter):
    files = []
    for index, path in enumerate(paths):
        reporter.check()
        checksum = worker.sha256_file(path)
        suffix = ''.join(path.suffixes)[-40:] or '.bin'
        name = '%s-%05d%s' % (port, index, suffix)
        query = urlencode({'name': name, 'sha256': checksum})
        with path.open('rb') as stream:
            body = training_data.UploadStream(stream, path.stat().st_size, lambda amount: reporter.check())
            response = connection.request('POST', '%d/artifacts/?%s' % (job['id'], query), data=body, timeout=(15, 60)).json()
        files.append({'artifact': response['id'], 'format': format, 'shuffled': bool(job['specification']['config'].get('shuffle', False)), 'interleaved': bool(job['specification']['config'].get('shuffle', False))})
    return files


def upload_data(connection, job, paths, reporter):
    connection.request('POST', '%d/upload/prepare/' % job['id'], json={})
    files = []
    for index, (path, original) in enumerate(zip(paths, job['dataset']['files'])):
        suffix = ''.join(Path(original['path']).suffixes).lower()
        suffix = '.vf' if original['format'] == 'vf' else '.wf' if original['format'] == 'wf' else suffix if suffix in ('.pgn', '.pgn.tar', '.pgn.gz', '.pgn.bz2', '.pgn.zst', '.tar', '.tar.gz', '.tar.bz2', '.tar.zst') else '.tar'
        files.append({'path': 'workflows/%d/%05d%s' % (job['id'], index, suffix), 'size': path.stat().st_size, 'sha256': worker.sha256_file(path), 'format': original['format'], 'shuffled': original.get('shuffled', False), 'interleaved': original.get('interleaved', False)})
    training_data.upload_prepared_files(UploadConnection(connection), job, paths, files, reporter)
    connection.request('POST', '%d/upload/publish/' % job['id'], json={'files': files}, timeout=(15, 90))


def execute(connection, job, root):
    connection = Connection(connection)
    directory = root / ('workflow-%d' % job['id'])
    worker.stop_previous(directory)
    if directory.exists():
        worker.preserve_work_directory(directory, root)
    directory.mkdir()
    reporter = None
    previous = {}
    interrupted = threading.Event()
    try:
        config = job['specification']['config']
        profile = config.get('training_profile', {})
        settings = job['snapshot']['settings']
        settings.update(backend=job['worker_info'].get('backend', 'cpu'), dataset_format=profile.get('format', 'vf'), skip_broken_games=config.get('skip_broken_games', True), fill_missing_evals=config.get('fill_missing_evals', ''), dataset_expansion_factor=8, shuffle_memory_mb=256, interleave_fan_in=32, dataset_shard_mb=config.get('dataset_shard_mb', 512), shuffle_seed=config.get('seed', 42))
        job['workflow_task'] = True
        write_json(directory / 'job.json', job)
        reporter = worker.Reporter(connection, job, directory)
        def stop(signum, frame):
            interrupted.set()
            reporter.stop.set()
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
        if job['report_sequence'] or job.get('cancel_requested'):
            raise RuntimeError('The worker restarted during this task. Retry the node to create a fresh task.')
        environment = worker.child_environment(directory, job)
        output_directory = directory / 'outputs'
        output_directory.mkdir()
        paths = worker.download_dataset(connection, job, directory, reporter) if job['dataset']['files'] else []
        local = {identity(file): path for file, path in zip(job['dataset']['files'], paths)}
        input_path = directory / 'inputs.json'
        write_json(input_path, materialize(job['inputs'], local))
        environment.update(MATTBENCH_INPUTS=str(input_path), MATTBENCH_THREADS=str(job['worker_info']['threads']))
        kind = job['specification']['kind']
        if kind == 'upload':
            upload_data(connection, job, paths, reporter)
        else:
            assets = {}
            if kind in ('convert', 'split'):
                paths = prepare_data(paths, job, directory, environment, reporter, root)
                partitions = split_data(paths, output_directory, config, reporter, settings['dataset_format']) if kind == 'split' else [paths]
                for port, selected in zip(('training', 'validation') if kind == 'split' else ('data',), partitions):
                    assets[port] = {'type': 'dataset', 'files': save_files(connection, job, selected, port, settings['dataset_format'], reporter)}
                assets['metrics'] = {'type': 'metrics', 'values': {'files': sum(len(part) for part in partitions), **{key: value for key, value in reporter.metrics.items() if type(value) in (int, float)}}}
            else:
                work = directory / 'recipe'
                work.mkdir()
                (work / 'outputs').mkdir()
                environment['MATTBENCH_OUTPUT_DIR'] = str(work / 'outputs')
                repository = config.get('repository')
                if repository:
                    worker.command(['git', 'init', str(work)], directory, environment, reporter, trusted=True)
                    worker.command(['git', '-C', str(work), 'fetch', '--depth=1', repository['url'], repository['commit']], directory, environment, reporter, trusted=True)
                    worker.command(['git', '-C', str(work), 'checkout', '--detach', 'FETCH_HEAD'], directory, environment, reporter, trusted=True)
                replacements = {'{inputs}': str(input_path), '{outputs}': str(work / 'outputs'), '{threads}': str(job['worker_info']['threads']), '{python}': 'python3' if job['worker_info'].get('execution_image') else sys.executable}
                for arguments in config['commands']:
                    expanded = list(arguments)
                    for index, argument in enumerate(expanded):
                        for key, value in replacements.items():
                            argument = argument.replace(key, value)
                        expanded[index] = argument
                    worker.command(expanded, work, environment, reporter)
                for port, asset_type in config.get('outputs', {'artifact': 'artifact'}).items():
                    selected = sorted(work.glob(config['files'][port]))
                    if not selected or len(selected) > 4096 or any(not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(work.resolve()) for path in selected):
                        raise RuntimeError('Output %s did not produce regular files inside the task repository.' % port)
                    if asset_type in ('parameters', 'metrics'):
                        if len(selected) != 1 or selected[0].stat().st_size > 1024 ** 2:
                            raise RuntimeError('A parameter or metric output requires one small JSON object.')
                        assets[port] = {'type': asset_type, 'values': json.loads(selected[0].read_text())}
                    else:
                        assets[port] = {'type': asset_type, 'files': save_files(connection, job, selected, port, config.get('dataset_format', settings['dataset_format']), reporter)}
            connection.request('POST', '%d/outputs/' % job['id'], json=assets, timeout=(15, 90))
        reporter.state = 'COMPLETED'
        reporter.flush()
    except Exception as error:
        if reporter:
            reporter.error = str(error)[:2048]
            reporter.state = 'CANCELLED' if reporter.stop.is_set() else 'FAILED'
            reporter.write(reporter.error + '\n')
            try:
                reporter.flush()
            except Exception:
                print('Could not report task failure. The coordinator will expire the claim.')
        else:
            raise
    finally:
        if reporter:
            reporter.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        worker.stop_previous(directory)
        if reporter and reporter.ack_state == 'COMPLETED':
            worker.remove_work_directory(directory, root)
        else:
            worker.preserve_work_directory(directory, root)
    return interrupted.is_set()
