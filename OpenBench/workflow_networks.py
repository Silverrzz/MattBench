import hashlib

from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.files.storage import FileSystemStorage

from OpenBench.models import EngineConfig, Network
from OpenBench.training_storage import artifact_path


def register_network(task, files):
    if len(files) != 1:
        raise ValidationError('A network output requires one file.')
    engine = EngineConfig.objects.filter(pk=task.snapshot['node']['config'].get('engine')).first()
    if not engine:
        raise ValidationError('Choose the engine for network outputs.')
    file = files[0]
    sha = file['sha256'][:8].upper()
    storage = FileSystemStorage()
    row = Network.objects.filter(sha256=sha).first()
    if row:
        if row.engine != engine.name or not storage.exists(sha):
            raise ValidationError('Network hash collision.')
        with storage.open(sha, 'rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != file['sha256']:
                raise ValidationError('Network hash collision.')
    else:
        if storage.exists(sha):
            raise ValidationError('Network destination already exists.')
        with artifact_path('workflows/%d/%s-%s' % (task.pk, file['sha256'], file['path'])).open('rb') as source:
            destination = storage.save(sha, File(source))
        if destination != sha:
            storage.delete(destination)
            raise ValidationError('Network registration collided with another upload.')
        row = Network.objects.create(sha256=sha, name=('workflow-%d' % task.pk), engine=engine.name, author=task.owner.username)
    return {'type': 'network', 'network': row.sha256, 'network_name': row.name, 'engine': row.engine, 'files': files, 'provenance': {'workflow_task': task.pk}}
