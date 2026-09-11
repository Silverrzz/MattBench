import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import uuid
from importlib.metadata import requires, version as package_version
from pathlib import Path

import psutil
from packaging.requirements import Requirement


def native_command(args, cwd, environment):
    arguments = list(map(str, args))
    executable = arguments[0]
    if '/' in executable or '\\' in executable:
        candidate = Path(executable)
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        if os.name == 'nt' and not candidate.is_file() and candidate.suffix.lower() != '.exe':
            candidate = candidate.with_name(candidate.name + '.exe')
        if not candidate.is_file():
            raise RuntimeError('Command executable was not found: %s (working directory: %s).' % (candidate, cwd))
        arguments[0] = str(candidate.resolve())
    else:
        resolved = shutil.which(executable, path=environment.get('PATH', ''))
        if not resolved:
            raise RuntimeError('Command executable %s was not found on the worker PATH.' % executable)
        arguments[0] = resolved
    return arguments


def windows_build_environment(environment):
    if os.name != 'nt':
        return environment
    locator = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft Visual Studio' / 'Installer' / 'vswhere.exe'
    if not locator.is_file():
        raise RuntimeError('Windows training requires Visual Studio C++ Build Tools and the Windows SDK.')
    installation = subprocess.check_output([str(locator), '-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'], text=True, timeout=30).strip()
    script = Path(installation) / 'VC' / 'Auxiliary' / 'Build' / 'vcvarsall.bat'
    if not installation or not script.is_file():
        raise RuntimeError('Install the Visual Studio Desktop development with C++ workload before starting training.')
    shell = os.environ.get('COMSPEC', r'C:\Windows\System32\cmd.exe')
    result = subprocess.run('"%s" /d /s /c ""%s" x64 >nul && set"' % (shell, script), env=environment, capture_output=True, text=True, errors='replace', timeout=60)
    if result.returncode:
        raise RuntimeError('Visual Studio could not initialise its x64 compiler environment. Repair the C++ Build Tools and Windows SDK installation.')
    selected = ('PATH', 'INCLUDE', 'LIB', 'LIBPATH', 'VCTOOLSINSTALLDIR', 'VSINSTALLDIR', 'WINDOWSSDKDIR', 'WINDOWSSDKVERSION', 'UNIVERSALCRTSDKDIR', 'UCRTVERSION')
    configured = dict(environment)
    for line in result.stdout.splitlines():
        name, separator, value = line.partition('=')
        if separator and name.upper() in selected:
            configured[name.upper()] = value
    linker = Path(configured.get('VCTOOLSINSTALLDIR', '')) / 'bin' / 'Hostx64' / 'x64' / 'link.exe'
    compiler = linker.with_name('cl.exe')
    if not linker.is_file() or not compiler.is_file() or not configured.get('LIB') or not configured.get('INCLUDE'):
        raise RuntimeError('The Microsoft x64 compiler, linker or Windows SDK paths are missing.')
    configured['PATH'] = str(linker.parent) + os.pathsep + configured.get('PATH', '')
    configured['CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER'] = str(linker)
    configured['CUDAHOSTCXX'] = str(compiler)
    return configured


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
    runtime = {'worker_sha256': digest.hexdigest(), 'python': platform.python_version(), 'platform': sys.platform, 'packages': packages, 'execution_image': image, 'rust': 'image-pinned' if image else version(['rustc', '--version']), 'cargo': 'image-pinned' if image else version(['cargo', '--version']), 'pawnocchio_sha256': pawnocchio_hash}
    if os.name == 'nt' and not image:
        configured = windows_build_environment(dict(os.environ))
        runtime['msvc'] = Path(configured['VCTOOLSINSTALLDIR']).name
        runtime['windows_sdk'] = configured.get('WINDOWSSDKVERSION', '').rstrip('\\/')
    return runtime


def stop_previous(directory):
    for path in directory.parent.glob('.process-' + directory.name + '-*.json'):
        stop_process_record(path)
    path = directory.parent / ('.process-' + directory.name + '.json')
    stop_process_record(path)


def stop_process_record(path):
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
            _, alive = psutil.wait_procs(children + [process], timeout=10)
            if alive:
                raise RuntimeError('Previous training processes have not stopped; cleanup will retry before accepting work.')
    except psutil.NoSuchProcess:
        pass
    path.unlink()


def remove_container(name):
    result = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, timeout=30)
    if result.returncode and 'No such container' not in result.stderr:
        raise RuntimeError('Cannot confirm the previous execution container stopped. Restore Docker connectivity before restarting this worker.')


def rocm_environment_variable(key):
    return key in ('HIP_VISIBLE_DEVICES', 'ROCR_VISIBLE_DEVICES') or re.fullmatch(r'HSA_OVERRIDE_GFX_VERSION(?:_\d+)?', key) is not None


def isolated_command(args, cwd, environment, reporter, cpu_threads=None):
    info = reporter.job['worker_info']
    image = info.get('execution_image')
    if not image:
        return args, environment, None
    directory = reporter.directory
    name = 'mattbench-%s-%s' % (reporter.job['id'], uuid.uuid4().hex[:12])
    command = ['docker', 'run', '--rm', '--name', name, '--init', '--read-only', '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '1024', '--user', '%d:%d' % (os.getuid(), os.getgid()), '--cpus', str(cpu_threads or info['threads']), '--memory', '%sg' % info['memory_gb'], '--tmpfs', '/tmp:rw,nosuid,nodev,size=1g', '--mount', 'type=bind,src=%s,dst=%s' % (directory, directory), '--workdir', str(cwd)]
    for key in ('pawnocchio_path', 'combiner_path'):
        executable = info[key]
        command.extend(['--mount', 'type=bind,src=%s,dst=%s,readonly' % (executable, executable)])
    if reporter.state in ('TRAINING', 'SAVING'):
        if info['backend'] == 'cuda':
            command.extend(['--gpus', 'device=' + info['device']])
        else:
            command.extend(['--device', '/dev/kfd', '--device', '/dev/dri', '--group-add', 'video', '--group-add', 'render'])
    for key, value in environment.items():
        if key.startswith('MATTBENCH_') or rocm_environment_variable(key) or key in ('HOME', 'TMPDIR', 'CARGO_HOME', 'CARGO_TARGET_DIR', 'CARGO_BUILD_JOBS') or key in reporter.job['snapshot'].get('environment', {}):
            command.extend(['--env', key + '=' + value])
    command.extend([image, *args])
    return command, dict(os.environ), name
