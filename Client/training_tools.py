import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import time
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import requests


PAWNOCCHIO_REPO = 'https://github.com/JonathanHallstrom/pawnocchio'
PAWNOCCHIO_REF = '85df99c903ee6b90dd593a71009354699a81bcc5'
COMBINER_REPO = 'https://github.com/JonathanHallstrom/viriformat_combiner'
COMBINER_REF = '045a3e399ca971e8153847e5f3f7e55e3e5a8103'
ZIG_HASHES = {
    ('0.16.0', 'x86_64-windows'): '68659eb5f1e4eb1437a722f1dd889c5a322c9954607f5edcf337bc3684a75a7e',
    ('0.16.0', 'x86_64-linux'): '70e49664a74374b48b51e6f3fdfbf437f6395d42509050588bd49abe52ba3d00',
    ('0.16.0', 'aarch64-linux'): 'ea4b09bfb22ec6f6c6ceac57ab63efb6b46e17ab08d21f69f3a48b38e1534f17',
    ('0.15.2', 'x86_64-windows'): '3a0ed1e8799a2f8ce2a6e6290a9ff22e6906f8227865911fb7ddedc3cc14cb0c',
    ('0.15.2', 'x86_64-linux'): '02aa270f183da276e5b5920b1dac44a63f1a49e55050ebde3aecc9eb82f93239',
    ('0.15.2', 'aarch64-linux'): '958ed7d1e00d0ea76590d27666efbf7a932281b3d7ba0c6b01b0ff26498f667f',
}


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def zig_toolchain(root, version, target):
    system_zig = shutil.which('zig')
    if system_zig:
        executable = Path(system_zig).resolve()
        try:
            installed_version = subprocess.check_output([str(executable), 'version'], text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
        except (OSError, subprocess.SubprocessError):
            installed_version = None
        if installed_version == version:
            print('Using system Zig %s: %s' % (version, executable), flush=True)
            return executable
    checksum = ZIG_HASHES.get((version, target))
    if checksum is None:
        raise RuntimeError('No pinned Zig toolchain for ' + target)
    name = 'zig-%s-%s' % (target, version)
    destination = root / name
    executable = destination / ('zig.exe' if target.endswith('windows') else 'zig')
    if executable.is_file():
        return executable
    print('Downloading Zig %s for %s' % (version, target), flush=True)
    suffix = '.zip' if target.endswith('windows') else '.tar.xz'
    with TemporaryDirectory(prefix='zig-download-', dir=root) as temporary:
        work = Path(temporary)
        archive = work / ('download' + suffix)
        started = last_report = time.monotonic()
        downloaded = 0
        try:
            with requests.get('https://ziglang.org/download/%s/%s%s' % (version, name, suffix), stream=True, timeout=(15, 30)) as response:
                response.raise_for_status()
                total = int(response.headers.get('Content-Length') or 0)
                if total:
                    print('Zig archive: %.1f MiB. Progress is reported every 5 seconds.' % (total / 1024 ** 2), flush=True)
                with archive.open('xb') as output:
                    for chunk in response.iter_content(64 * 1024):
                        output.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last_report >= 5:
                            size = '%.1f / %.1f MiB (%.0f%%)' % (downloaded / 1024 ** 2, total / 1024 ** 2, downloaded * 100 / total) if total else '%.1f MiB' % (downloaded / 1024 ** 2)
                            print('Downloading Zig: %s, %.2f MiB/s' % (size, downloaded / 1024 ** 2 / max(now - started, 0.001)), flush=True)
                            last_report = now
        except requests.RequestException as error:
            raise RuntimeError('Zig %s download failed after %.1f MiB: %s' % (version, downloaded / 1024 ** 2, error)) from error
        print('Zig download complete (%.1f MiB). Verifying checksum...' % (downloaded / 1024 ** 2), flush=True)
        if digest(archive) != checksum:
            raise RuntimeError('Zig download checksum mismatch.')
        unpacked = work / 'unpacked'
        unpacked.mkdir()
        print('Extracting Zig %s...' % version, flush=True)
        if suffix == '.zip':
            with zipfile.ZipFile(archive) as source:
                for member in source.infolist():
                    if not (unpacked / member.filename).resolve().is_relative_to(unpacked):
                        raise RuntimeError('Invalid Zig archive path.')
                source.extractall(unpacked)
        else:
            with tarfile.open(archive, 'r:xz') as source:
                for member in source:
                    path = (unpacked / member.name).resolve()
                    if not path.is_relative_to(unpacked) or not (member.isfile() or member.isdir()):
                        raise RuntimeError('Invalid Zig archive entry.')
                    if member.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with source.extractfile(member) as input_file, path.open('xb') as output:
                            shutil.copyfileobj(input_file, output)
                        path.chmod(member.mode & 0o777)
        if not (unpacked / name / executable.name).is_file():
            raise RuntimeError('Zig archive has no compiler.')
        (unpacked / name).rename(destination)
    print('Zig %s ready.' % version, flush=True)
    return executable


def build_dataset_tools(directory, pawnocchio_repo, pawnocchio_ref, combiner_repo, combiner_ref, image, threads, memory_gb):
    root = directory / 'tools'
    root.mkdir(exist_ok=True)
    machine = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'aarch64', 'aarch64': 'aarch64'}.get(platform.machine().lower())
    target = '%s-%s' % (machine, 'linux' if image else 'windows' if os.name == 'nt' else 'linux')
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('OPENBENCH_', 'MATTBENCH_', 'HF_', 'HUGGING_FACE_'))}
    environment.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    outputs = {}
    provenance = {}
    for name, repository, revision, version, flags, binary in (
        ('pawnocchio', pawnocchio_repo, pawnocchio_ref, '0.16.0', ['-Deval=hce', '-Dtools_only=true', '-Dname=pawnocchio'], 'pawnocchio'),
        ('combiner', combiner_repo, combiner_ref, '0.15.2', [], 'viriformat_combiner'),
    ):
        if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not re.fullmatch(r'[a-f0-9]{40}', revision):
            raise RuntimeError('Dataset tool sources require an HTTPS GitHub repository and a full commit SHA.')
        specification = {'repo': repository, 'commit': revision, 'zig': version, 'target': target, 'image': image, 'flags': flags, 'optimize': 'ReleaseFast'}
        cache_key = hashlib.sha256(json.dumps(specification, sort_keys=True).encode()).hexdigest()[:24]
        destination = root / (name + '-' + cache_key)
        executable = destination / 'install' / 'bin' / (binary + ('.exe' if target.endswith('windows') else ''))
        metadata = destination / 'build.json'
        if executable.is_file() and metadata.is_file():
            saved = json.loads(metadata.read_text())
            if saved.get('specification') == specification and saved.get('sha256') == digest(executable):
                outputs[name] = executable
                provenance[name] = saved
                continue
        if destination.exists():
            raise RuntimeError('Dataset tool cache failed verification: ' + str(destination))
        zig = zig_toolchain(root, version, target)
        with TemporaryDirectory(prefix=name + '-build-', dir=root) as temporary:
            work = Path(temporary)
            source = work / 'source'
            print('Fetching %s at %s' % (repository, revision), flush=True)
            for args in (
                ['git', 'init', str(source)],
                ['git', '-C', str(source), 'remote', 'add', 'origin', repository],
                ['git', '-C', str(source), 'fetch', '--depth', '1', 'origin', revision],
                ['git', '-C', str(source), 'checkout', '--detach', 'FETCH_HEAD'],
            ):
                subprocess.run(args, check=True, env=environment, timeout=300)
            actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True, env=environment, timeout=30).strip()
            if actual != revision:
                raise RuntimeError('Dataset tool checkout does not match its pinned commit.')
            args = [str(zig), 'build', '-Doptimize=ReleaseFast', '-j%d' % threads, '--prefix', str(work / 'install'), *flags]
            if image:
                args = ['docker', 'run', '--rm', '--init', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '1024', '--user', '%d:%d' % (os.getuid(), os.getgid()), '--cpus', str(threads), '--memory', '%sg' % memory_gb, '--tmpfs', '/tmp:rw,nosuid,nodev,size=1g', '--mount', 'type=bind,src=%s,dst=%s' % (work, work), '--mount', 'type=bind,src=%s,dst=%s,readonly' % (zig.parent, zig.parent), '--workdir', str(source), '--env', 'ZIG_GLOBAL_CACHE_DIR=' + str(work / 'zig-cache'), image, *args]
            else:
                environment['ZIG_GLOBAL_CACHE_DIR'] = str(work / 'zig-cache')
            print('Building %s with Zig %s' % (name, version), flush=True)
            subprocess.run(args, cwd=source, env=environment, check=True, timeout=3600)
            built = work / 'install' / 'bin' / executable.name
            if not built.is_file():
                raise RuntimeError('Dataset tool build did not produce ' + binary)
            saved = {'specification': specification, 'sha256': digest(built)}
            (work / 'build.json').write_text(json.dumps(saved, indent=2), encoding='utf-8')
            work.rename(destination)
        outputs[name] = executable
        provenance[name] = saved
    return outputs, provenance
