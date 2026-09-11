import math
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path


def gpu_tool(name):
    executable = shutil.which(name)
    if executable:
        return executable
    roots = [Path(os.environ[key]) for key in ('HIP_PATH', 'ROCM_PATH', 'CUDA_PATH') if os.environ.get(key)]
    if os.name != 'nt':
        roots.append(Path('/opt/rocm'))
    for root in roots:
        executable = shutil.which(name, path=str(root / 'bin'))
        if executable:
            return executable
    return None


def memory_gb(value):
    match = re.fullmatch(r'\s*([\d.]+)(?:\s*\(0x[0-9a-f]+\))?\s*(bytes|[KMGT]i?B|B)?\s*', value, re.I)
    if not match:
        raise ValueError('Unrecognized GPU memory size')
    unit = (match[2] or 'B').upper().replace('IB', 'B')
    size = float(match[1]) * {'B': 1, 'BYTES': 1, 'KB': 1024, 'MB': 1024 ** 2, 'GB': 1024 ** 3, 'TB': 1024 ** 4}[unit] / 1024 ** 3
    if not math.isfinite(size) or size <= 0:
        raise ValueError('Invalid GPU memory size')
    return size


def parse_amd_gpu(output, tool):
    if tool.lower() == 'hipinfo':
        for block in re.split(r'(?im)^\s*name\s*:', output)[1:]:
            name = block.splitlines()[0].strip()
            memory = re.search(r'(?im)^\s*totalGlobalMem\s*:\s*([^\r\n]+)', block)
            if name and memory:
                return name, memory_gb(memory[1])
    else:
        for block in re.split(r'(?im)^\s*Agent\s+\d+\s*$', output):
            if not re.search(r'(?im)^\s*Device Type:\s*GPU\s*$', block):
                continue
            name = re.search(r'(?im)^\s*Marketing Name:[ \t]*([^\r\n]*)', block)
            if not name or not name[1].strip():
                name = re.search(r'(?im)^\s*Name:[ \t]*([^\r\n]+)', block)
            pools = re.findall(r'(?im)^\s*Segment:[ \t]*GLOBAL[^\r\n]*\r?\n\s*Size:[ \t]*([^\r\n]+)', block)
            if name and pools:
                return name[1].strip(), max(memory_gb(pool) for pool in pools)
    raise ValueError('No GPU with a reported memory size found')


def detect_gpu(backend=None, device=None):
    errors = []
    for candidate in ([backend] if backend else ['cuda', 'rocm']):
        visibility = 'CUDA_VISIBLE_DEVICES' if candidate == 'cuda' else 'HIP_VISIBLE_DEVICES'
        selected = device if device is not None else os.environ.get(visibility, '0').split(',')[0].strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', selected):
            errors.append('%s: no valid visible GPU selected' % candidate)
            continue
        names = ['nvidia-smi'] if candidate == 'cuda' else (['hipInfo', 'rocminfo'] if os.name == 'nt' else ['rocminfo', 'hipInfo'])
        for name in names:
            executable = gpu_tool(name)
            if not executable:
                errors.append('%s not found' % name)
                continue
            environment = os.environ.copy()
            command = [executable]
            if candidate == 'cuda':
                command.extend(['--query-gpu=name,memory.total,uuid,driver_version', '--format=csv,noheader,nounits', '-i', selected])
            elif name == 'rocminfo':
                visible = environment.get('ROCR_VISIBLE_DEVICES')
                if visible is not None and selected.isdigit() and (not visible.strip() or int(selected) >= len(visible.split(','))):
                    errors.append('rocminfo: selected GPU is outside ROCR_VISIBLE_DEVICES')
                    continue
                physical = visible.split(',')[int(selected)].strip() if visible and selected.isdigit() and int(selected) < len(visible.split(',')) else selected
                environment['ROCR_VISIBLE_DEVICES'] = physical
            else:
                environment[visibility] = selected
            try:
                output = subprocess.check_output(command, text=True, errors='replace', timeout=10, stderr=subprocess.PIPE, env=environment)
                driver = 'kernel-' + platform.release()
                if candidate == 'cuda':
                    gpu, memory, selected, driver = [part.strip() for part in output.strip().split(',')]
                    vram = memory_gb(memory + ' MB')
                else:
                    gpu, vram = parse_amd_gpu(output, name)
                return {'backend': candidate, 'device': selected, 'gpu': gpu, 'vram_gb': vram, 'driver': driver}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                errors.append('%s failed (%s)' % (name, type(error).__name__))
    raise RuntimeError('GPU detection failed: %s. Check the GPU driver and utility installation, or specify the training backend, GPU name and VRAM explicitly.' % '; '.join(errors))
