import bz2
import gzip
import json
import os
import random
import shutil
import subprocess
import tarfile
from contextlib import ExitStack
from pathlib import Path

try:
    from .training_data import publish_prepared
except ImportError:
    from training_data import publish_prepared


HEADER = b'WAKF\x01\x00\x00\x00'


def games(path, reporter):
    with path.open('rb') as source:
        if source.read(8) != HEADER:
            raise RuntimeError('Unsupported Wakformat header: ' + path.name)
        while True:
            reporter.check()
            header = source.read(32)
            if not header:
                return
            if len(header) != 32:
                raise RuntimeError('Truncated Wakformat game header.')
            result = bytearray(header)
            while True:
                record = source.read(5)
                if len(record) != 5:
                    raise RuntimeError('Truncated Wakformat move record.')
                result.extend(record)
                if record == b'\0' * 5:
                    break
                if len(result) > 4 * 1024 ** 2:
                    raise RuntimeError('Wakformat game exceeds 4 MiB.')
            yield bytes(result)


def build_tool(directory, environment, reporter, command):
    existing = reporter.job.get('worker_info', {}).get('wakformat_path')
    if existing:
        return Path(existing)
    specification = reporter.job['snapshot']['settings']['training_profile']['data_tool']
    root = directory / 'wakformat-tool'
    root.mkdir()
    command(['git', 'init', str(root)], directory, environment, reporter, trusted=True)
    command(['git', '-C', str(root), 'fetch', '--depth=1', specification['repo'], specification['commit']], directory, environment, reporter, trusted=True)
    command(['git', '-C', str(root), 'checkout', '--detach', 'FETCH_HEAD'], directory, environment, reporter, trusted=True)
    arguments = ['cargo', 'build', '--release', '--locked']
    info = reporter.job['worker_info']
    if info.get('execution_image'):
        args = ['docker', 'run', '--rm', '--init', '--user', '%d:%d' % (os.getuid(), os.getgid()), '--mount', 'type=bind,src=%s,dst=%s' % (reporter.directory, reporter.directory), '--workdir', str(root)]
        for key in ('HOME', 'CARGO_HOME', 'CARGO_TARGET_DIR'):
            args += ['--env', key + '=' + environment[key]]
        arguments = args + [info['execution_image'], *arguments]
    command(arguments, root, environment, reporter, trusted=True)
    target = Path(environment['CARGO_TARGET_DIR']) / 'release' / ('wakformat.exe' if os.name == 'nt' else 'wakformat')
    if not target.is_file():
        raise RuntimeError('Wakformat build produced no executable.')
    return target


def convert(paths, directory, tool, environment, reporter, command):
    target = directory / 'wakformat'
    target.mkdir()
    output = []
    expanded = 0
    limit = reporter.job['dataset']['size'] * reporter.job['snapshot']['settings'].get('dataset_expansion_factor', 8)
    def copy_limited(source, destination):
        nonlocal expanded
        while block := source.read(1024 * 1024):
            reporter.check()
            expanded += len(block)
            if expanded > limit:
                raise RuntimeError('DuckChess archive exceeds its expansion budget.')
            destination.write(block)
    def one(path, name):
        index = len(output)
        destination = target / ('%05d.wf' % index)
        if name.lower().endswith('.wf'):
            shutil.copyfile(path, destination)
        else:
            args = [str(tool), 'pgn2wf', str(path), '--output', str(destination), '--threads', str(reporter.job['worker_info']['threads'])]
            config = reporter.job['snapshot']['settings']
            if config.get('skip_broken_games', True):
                args.append('--skip-broken-games')
            if config.get('fill_missing_evals'):
                args += ['--fill-missing-evals', config['fill_missing_evals']]
            command(args, directory, environment, reporter)
        if destination.stat().st_size <= 8:
            raise RuntimeError('Dataset conversion produced no DuckChess games.')
        output.append(destination)
    for path in paths:
        lower = path.name.lower()
        if lower.endswith(('.wf', '.pgn', '.pgn.bz2')):
            one(path, path.name)
        elif lower.endswith(('.pgn.gz', '.pgn.zst')):
            plain = target / ('input-%d.pgn' % len(output))
            with ExitStack() as stack:
                stream = stack.enter_context(path.open('rb'))
                if lower.endswith('.gz'):
                    stream = stack.enter_context(gzip.GzipFile(fileobj=stream))
                else:
                    import zstandard
                    stream = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(stream))
                with plain.open('wb') as dest:
                    copy_limited(stream, dest)
            one(plain, plain.name)
            plain.unlink()
        else:
            with ExitStack() as stack:
                stream = stack.enter_context(path.open('rb'))
                if lower.endswith('.zst'):
                    import zstandard
                    stream = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(stream))
                archive = stack.enter_context(tarfile.open(fileobj=stream, mode='r|*'))
                total = 0
                for number, member in enumerate(archive):
                    reporter.check()
                    if number > 100000:
                        raise RuntimeError('DuckChess archive contains too many members.')
                    if not member.isfile() or not member.name.lower().endswith(('.wf', '.pgn', '.pgn.bz2')):
                        continue
                    total += member.size
                    if total > reporter.job['dataset']['size'] * reporter.job['snapshot']['settings'].get('dataset_expansion_factor', 8):
                        raise RuntimeError('DuckChess archive exceeds its expansion budget.')
                    temporary = target / ('input-%d-%s' % (number, Path(member.name).name))
                    with archive.extractfile(member) as source, temporary.open('wb') as dest:
                        copy_limited(source, dest)
                    one(temporary, member.name)
                    temporary.unlink()
    if not output:
        raise RuntimeError('No supported DuckChess dataset files were found.')
    return output


def order(paths, directory, config, reporter, shuffle):
    directory.mkdir()
    rng = random.Random(config.get('shuffle_seed', 42))
    budget = config.get('shuffle_memory_mb', 256) * 1024 ** 2
    runs, buffer, size, count = [], [], 0, 0
    def flush():
        nonlocal buffer, size
        if not buffer:
            return
        if shuffle:
            rng.shuffle(buffer)
        path = directory / ('chunk-%05d.wf' % len(runs))
        with path.open('wb') as out:
            out.write(HEADER)
            out.writelines(buffer)
        runs.append((path, len(buffer)))
        buffer, size = [], 0
    for path in paths:
        for game in games(path, reporter):
            buffer.append(game)
            size += len(game)
            count += 1
            if size >= budget:
                flush()
    flush()
    if not count:
        raise RuntimeError('No Wakformat games remain.')
    generation = 0
    fan_in = config.get('interleave_fan_in', 32)
    while len(runs) > 1:
        merged = []
        for group in range(0, len(runs), fan_in):
            selected = runs[group:group + fan_in]
            iterators = [iter(games(path, reporter)) for path, _ in selected]
            remaining = [number for _, number in selected]
            path = directory / ('merge-%d-%d.wf' % (generation, group))
            with path.open('wb') as out:
                out.write(HEADER)
                total = sum(remaining)
                for _ in range(total):
                    ticket = rng.randrange(sum(remaining)) if shuffle or config.get('interleave', True) else 0
                    index = 0
                    while ticket >= remaining[index]:
                        ticket -= remaining[index]
                        index += 1
                    out.write(next(iterators[index]))
                    remaining[index] -= 1
            for iterator in iterators:
                iterator.close()
            merged.append((path, total))
            for old, _ in selected:
                if old.resolve().is_relative_to(directory.resolve()):
                    old.unlink()
        runs, generation = merged, generation + 1
    outputs = []
    limit = config.get('dataset_shard_mb', 512) * 1024 ** 2
    stream, size = None, 0
    try:
        for game in games(runs[0][0], reporter):
            if stream is None or size >= limit:
                if stream:
                    stream.close()
                path = directory / ('%05d.wf' % len(outputs))
                outputs.append(path)
                stream = path.open('wb')
                stream.write(HEADER)
                size = 8
            stream.write(game)
            size += len(game)
    finally:
        if stream:
            stream.close()
    runs[0][0].unlink()
    return outputs, count


def prepare(connection, job, plan, paths, directory, environment, reporter, command):
    tool = build_tool(directory, environment, reporter, command)
    config = job['snapshot']['settings']
    steps = plan['steps']
    selected = convert(paths, directory, tool, environment, reporter, command) if any(step in steps for step in ('convert', 'extract')) else paths
    statistics = dict(plan.get('statistics', {}))
    if any(step in steps for step in ('shuffle', 'interleave', 'convert', 'extract')):
        reporter.update(progress_known=0, preparation_step='Preparing Wakformat game order')
        selected, count = order(selected, directory / 'ordered-wf', config, reporter, 'shuffle' in steps)
        statistics.update(games=count, source_bytes=job['dataset']['size'], shuffle_seed=config.get('shuffle_seed', 42), format='wf', data_tool=config['training_profile']['data_tool'])
    reports = []
    for index, path in enumerate(selected):
        reporter.check()
        work = directory / ('analysis-wf-%d' % index)
        work.mkdir()
        reports.append(command([str(tool), 'analyse', str(path), '--approximate'], work, environment, reporter))
    statistics.update(analysis='\n'.join(reports)[-48000:], converted_bytes=sum(path.stat().st_size for path in selected), converted_files=len(selected))
    job['dataset_statistics'] = statistics
    if plan['mode'] != 'prepare':
        return selected
    return publish_prepared(connection, job, plan, selected, directory, statistics, reporter)
