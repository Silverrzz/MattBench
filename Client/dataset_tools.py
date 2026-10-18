import hashlib
import json
import os
import platform
import shutil
from pathlib import Path

try:
    from .training_tools import build_dataset_tools, PAWNOCCHIO_REPO, PAWNOCCHIO_REF, COMBINER_REPO, COMBINER_REF
    from .variant_data import build_tool
except ImportError:
    from training_tools import build_dataset_tools, PAWNOCCHIO_REPO, PAWNOCCHIO_REF, COMBINER_REPO, COMBINER_REF
    from variant_data import build_tool


def ensure_tools(job, root, directory, environment, reporter, command):
    profile = job['snapshot']['settings'].get('training_profile', {})
    specification = profile.get('data_tool') or {'kind': 'pawnocchio', 'repo': PAWNOCCHIO_REPO, 'commit': PAWNOCCHIO_REF, 'combiner': {'repo': COMBINER_REPO, 'commit': COMBINER_REF}}
    kind = specification.get('kind', 'wakformat' if job['snapshot']['settings'].get('dataset_format') == 'wf' else 'pawnocchio')
    info = job['worker_info']
    reporter.update(preparation_step='Preparing ' + ('Wakformat' if kind == 'wakformat' else 'Pawnocchio'))
    reporter.write('Preparing %s dataset tools for this workload.\n' % kind)
    if kind == 'pawnocchio':
        combiner = specification['combiner']
        paths, provenance = build_dataset_tools(root, specification['repo'], specification['commit'], combiner['repo'], combiner['commit'], info.get('execution_image', ''), int(info['threads']), info.get('memory_gb', 8), runner=lambda args, cwd, env: command(args, cwd, env, reporter, trusted=True))
    elif kind == 'wakformat':
        identity = {'source': specification, 'image': info.get('execution_image', ''), 'platform': platform.platform(), 'rust': info.get('runtime', {}).get('rust', '')}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
        cache = root / 'tools' / ('wakformat-' + key)
        binary = cache / ('wakformat.exe' if os.name == 'nt' else 'wakformat')
        metadata = cache / 'build.json'
        saved = json.loads(metadata.read_text()) if metadata.is_file() else {}
        if binary.is_file():
            with binary.open('rb') as source:
                checksum = hashlib.file_digest(source, 'sha256').hexdigest()
            if saved.get('specification') != identity or saved.get('sha256') != checksum:
                raise RuntimeError('Wakformat cache verification failed.')
        else:
            work = directory / 'dataset-tool-build'
            work.mkdir()
            tool_environment = {**environment, 'CARGO_TARGET_DIR': str(work / 'target')}
            built = build_tool(work, tool_environment, reporter, command)
            cache.mkdir(parents=True, exist_ok=True)
            shutil.copy2(built, binary)
            with binary.open('rb') as source:
                checksum = hashlib.file_digest(source, 'sha256').hexdigest()
            saved = {'specification': identity, 'sha256': checksum}
            metadata.write_text(json.dumps(saved, indent=2))
        paths, provenance = {'wakformat': binary}, {'wakformat': saved}
    else:
        raise RuntimeError('Unsupported dataset tool provider.')
    info.update({name + '_path': str(path) for name, path in paths.items()})
    info.setdefault('runtime', {})['dataset_tools'] = provenance
    job['dataset_tools'] = {'provider': kind, 'provenance': provenance}
    reporter.check()
    return paths.get('pawnocchio')
