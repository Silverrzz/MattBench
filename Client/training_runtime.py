import hashlib
import json
import os
import platform
import secrets
import subprocess
import sys
import uuid
from importlib.metadata import requires, version as package_version
from pathlib import Path

import psutil
from packaging.requirements import Requirement


def dependency_versions():
    pending = ['requests', 'psutil', 'huggingface_hub', 'hf-xet', 'zstandard']
    packages = {}
    while pending:
        name = pending.pop().lower().replace('_', '-')
        if name in packages:
            continue
        packages[name] = package_version(name)
        for dependency in requires(name) or []:
            requirement = Requirement(dependency)
            if requirement.marker is None or requirement.marker.evaluate({'extra': ''}):
                pending.append(requirement.name)
    return dict(sorted(packages.items()))


def write_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(value, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def worker_lock(directory):
    handle = (directory / 'worker.lock').open('a+b')
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if handle.read(1) == b'':
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('Another training worker is using this directory.') from None
    return handle


def identity(directory, server):
    path = directory / 'identity.json'
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['server'] != server.rstrip('/'):
            raise RuntimeError('This worker directory belongs to another server.')
        if os.name != 'nt' and path.stat().st_mode & 0o077:
            raise RuntimeError('Worker identity must only be readable by its owner (chmod 600).')
        return path, data
    data = {'server': server.rstrip('/'), 'worker': str(uuid.uuid4()), 'token': secrets.token_urlsafe(48), 'claim_id': str(uuid.uuid4()), 'registered': False}
    write_json(path, data)
    return path, data


def runtime_info(pawnocchio, image):
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ('training_worker.py', 'training_data.py', 'training_tools.py', 'training_checkpoints.py', 'training_runtime.py', 'training-requirements.txt', 'training-lock.txt'):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    def version(command):
        return subprocess.check_output(command, text=True, timeout=30, stderr=subprocess.STDOUT).strip()
    with pawnocchio.open('rb') as source:
        pawnocchio_hash = hashlib.file_digest(source, 'sha256').hexdigest()
    packages = dependency_versions()
    return {'worker_sha256': digest.hexdigest(), 'python': platform.python_version(), 'platform': sys.platform, 'packages': packages, 'execution_image': image, 'rust': 'image-pinned' if image else version(['rustc', '--version']), 'cargo': 'image-pinned' if image else version(['cargo', '--version']), 'pawnocchio_sha256': pawnocchio_hash}


def stop_previous(directory):
    path = directory.parent / ('.process-' + directory.name + '.json')
    if not path.exists():
        return
    record = json.loads(path.read_text(encoding='utf-8'))
    if record.get('container'):
        remove_container(record['container'])
    if record['pid'] is None:
        path.unlink()
        return
    try:
        process = psutil.Process(record['pid'])
        if process.create_time() == record['created']:
            children = process.children(recursive=True)
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
            psutil.wait_procs(children + [process], timeout=10)
    except psutil.NoSuchProcess:
        pass
    path.unlink()


def remove_container(name):
    result = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, timeout=30)
    if result.returncode and 'No such container' not in result.stderr:
        raise RuntimeError('Cannot confirm the previous execution container stopped. Restore Docker connectivity before restarting this worker.')


def isolated_command(args, cwd, environment, reporter):
    info = reporter.job['worker_info']
    image = info.get('execution_image')
    if not image:
        return args, environment, None
    directory = reporter.directory
    name = 'mattbench-%s-%s' % (reporter.job['id'], uuid.uuid4().hex[:12])
    command = ['docker', 'run', '--rm', '--name', name, '--init', '--read-only', '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '1024', '--user', '%d:%d' % (os.getuid(), os.getgid()), '--cpus', str(reporter.job['snapshot']['settings']['threads']), '--memory', '%sg' % info['memory_gb'], '--tmpfs', '/tmp:rw,nosuid,nodev,size=1g', '--mount', 'type=bind,src=%s,dst=%s' % (directory, directory), '--workdir', str(cwd)]
    for key in ('pawnocchio_path', 'combiner_path'):
        executable = info[key]
        command.extend(['--mount', 'type=bind,src=%s,dst=%s,readonly' % (executable, executable)])
    if reporter.state in ('TRAINING', 'SAVING'):
        if info['backend'] == 'cuda':
            command.extend(['--gpus', 'device=' + info['device']])
        else:
            command.extend(['--device', '/dev/kfd', '--device', '/dev/dri', '--group-add', 'video', '--group-add', 'render'])
    for key, value in environment.items():
        if key.startswith('MATTBENCH_') or key in ('HOME', 'TMPDIR', 'CARGO_HOME', 'CARGO_TARGET_DIR', 'CARGO_BUILD_JOBS'):
            command.extend(['--env', key + '=' + value])
    command.extend([image, *args])
    return command, dict(os.environ), name
