"""Content-validated local caches; active entries move into the attempt directory."""
import hashlib
import json
import shutil
import time
from pathlib import Path


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 ** 2), b''):
            result.update(chunk)
    return result.hexdigest()


def take(root, cache_key, destination):
    entry = root / cache_key
    try:
        manifest = json.loads((entry / 'cache.json').read_text())
        for file in manifest['files']:
            path = entry / file['path']
            if not path.resolve().is_relative_to(entry.resolve()) or path.is_symlink() or path.stat().st_size != file['size'] or digest(path) != file['sha256']:
                raise ValueError('Cache verification failed')
        if destination.exists():
            if any(destination.iterdir()):
                return None
            destination.rmdir()
        entry.rename(destination)
        return manifest
    except (ValueError, KeyError, OSError, TypeError):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        return None


def put(root, cache_key, directory, files, metadata=None):
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in files:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            return False
        entries.append({'path': path.relative_to(directory).as_posix(), 'size': path.stat().st_size, 'sha256': digest(path)})
    if not entries:
        return False
    (directory / 'cache.json').write_text(json.dumps({'files': entries, 'metadata': metadata or {}, 'used': time.time()}))
    destination = root / cache_key
    if destination.exists():
        shutil.rmtree(destination)
    directory.rename(destination)
    return True


def evict(root, required_free):
    if not root.exists():
        return
    entries = sorted((path for path in root.glob('*/*') if path.is_dir() and not path.is_symlink()), key=lambda path: path.stat().st_mtime)
    for path in entries:
        if shutil.disk_usage(root).free >= required_free:
            break
        shutil.rmtree(path)


def reclaimable_bytes(root):
    return sum(path.stat().st_size for path in root.glob('*/*/**/*') if path.is_file() and not path.is_symlink()) if root.exists() else 0
