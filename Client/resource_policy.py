import contextlib
import ctypes
import os
import struct
from pathlib import Path

import psutil


def topology():
    try:
        logical = sorted(psutil.Process().cpu_affinity())
    except (AttributeError, OSError, psutil.Error):
        logical = list(range(psutil.cpu_count() or 1))
    physical = []
    if os.name == 'posix' and Path('/sys/devices/system/cpu').is_dir():
        seen = set()
        for cpu in logical:
            path = Path('/sys/devices/system/cpu/cpu%d/topology' % cpu)
            try:
                key = (path.joinpath('physical_package_id').read_text(), path.joinpath('core_id').read_text())
                if key not in seen:
                    seen.add(key)
                    physical.append(cpu)
            except OSError:
                physical = []
                break
    elif os.name == 'nt':
        try:
            length = ctypes.c_ulong()
            function = ctypes.windll.kernel32.GetLogicalProcessorInformationEx
            function(0, None, ctypes.byref(length))
            buffer = ctypes.create_string_buffer(length.value)
            if function(0, buffer, ctypes.byref(length)):
                position = 0
                while position < length.value:
                    relation, size = struct.unpack_from('<II', buffer.raw, position)
                    if relation == 0:
                        groups = struct.unpack_from('<H', buffer.raw, position + 30)[0]
                        for index in range(groups):
                            offset = position + 32 + 16 * index
                            mask, group = struct.unpack_from('<QH', buffer.raw, offset)
                            candidates = [cpu for cpu in logical if group == 0 and mask & (1 << cpu)]
                            if candidates:
                                physical.append(candidates[0])
                    if size < 8:
                        break
                    position += size
        except (OSError, AttributeError, struct.error):
            physical = []
    return {'logical': logical, 'physical': sorted(set(physical))}


@contextlib.contextmanager
def assignment(config, policy):
    original_threads = config.threads
    process = psutil.Process()
    affinity = None
    mode = policy.get('cpu', 'inherit')
    try:
        if mode != 'inherit':
            affinity = process.cpu_affinity()
            available = topology()
            cpus = available['physical'] if mode == 'physical' else available['logical']
            if mode == 'limit':
                cpus = cpus[:policy['threads']]
            if not cpus:
                raise RuntimeError('The workflow CPU policy cannot be applied on this worker.')
            process.cpu_affinity(cpus)
        yield
    finally:
        config.threads = original_threads
        if affinity is not None:
            process.cpu_affinity(affinity)
