import gzip
import subprocess
from collections import deque
from pathlib import Path


LIMIT = 1024 * 1024
CAPABILITIES = {}


def prefix(config, timestamp, runner):
    return 'PGNs/uci-%s-%s-%s' % (config.workload['test']['id'], str(timestamp).replace('.', '-'), runner)


def flags(config, timestamp, runner, executable):
    import shlex
    if not config.workload['test'].get('capture_logs', True):
        return ''
    if executable not in CAPABILITIES:
        try:
            help_text = subprocess.check_output([executable, '-help'], stderr=subprocess.STDOUT, timeout=10).decode('utf-8', errors='replace')
        except (OSError, subprocess.SubprocessError):
            help_text = ''
        CAPABILITIES[executable] = help_text
    help_text = CAPABILITIES[executable]
    if '-log' not in help_text:
        return ''
    options = ' -log file=%s level=trace append=false' % shlex.quote(prefix(config, timestamp, runner) + '.log')
    for key in ('engine', 'realtime'):
        if key + '=' in help_text:
            options += ' ' + key + '=true'
    return options


def collect(config, timestamp, runner, limit=LIMIT):
    chunks = deque()
    size = 0
    truncated = False
    for path in sorted(Path('PGNs').glob(Path(prefix(config, timestamp, runner)).name + '*')):
        if not path.is_file():
            continue
        source = gzip.open if path.suffix == '.gz' else open
        try:
            with source(path, 'rb') as stream:
                for line in stream:
                    truncated = truncated or len(line) > limit
                    chunks.append(line[-limit:])
                    size += len(chunks[-1])
                    while size > limit and chunks:
                        size -= len(chunks.popleft())
                        truncated = True
        except (OSError, EOFError):
            pass
    if not chunks:
        return ''
    heading = '\n\nUCI and match-runner diagnostics for runner %s\n' % runner
    if truncated:
        heading += 'Earlier diagnostics omitted; the most recent %d KiB is retained.\n' % (limit // 1024)
    return heading + b''.join(chunks).decode('utf-8', errors='replace')


def cleanup(config, timestamp, runner):
    for path in Path('PGNs').glob(Path(prefix(config, timestamp, runner)).name + '*'):
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
