from django.core.cache import cache

from OpenBench.models import DatasetUpload, TrainingDataset
from OpenBench.training import hf_token


def repository_choices(user, connection):
    known = {row.repo: {'id': row.repo, 'private': row.private}
             for row in DatasetUpload.objects.filter(owner=user).order_by('created')}
    for repo in TrainingDataset.objects.filter(owner=user).values_list('repo', flat=True).distinct():
        known.setdefault(repo, {'id': repo, 'private': None})
    if not connection:
        return sorted(known.values(), key=lambda row: row['id'].lower()), ''
    key = 'hf-destinations:%s:%s' % (user.pk, connection.account)
    cached = cache.get(key)
    message = cache.get(key + ':error', '')
    if cached is None:
        try:
            from huggingface_hub import HfApi
            api = HfApi(token=hf_token(user.pk))
            identity = api.whoami()
            authors = [connection.account, *[org['name'] for org in identity.get('orgs', []) if org.get('roleInOrg') in ('admin', 'write', 'contributor')]]
            cached = []
            for author in dict.fromkeys(authors):
                for dataset in api.list_datasets(author=author, limit=200):
                    cached.append({'id': dataset.id, 'private': getattr(dataset, 'private', None)})
            cache.set(key, cached, 60)
        except Exception:
            cached = []
            message = 'Could not refresh Hugging Face repositories. Previously used datasets are still listed, and you can enter a repository above.'
            cache.set(key, cached, 30)
            cache.set(key + ':error', message, 30)
    for row in cached:
        known[row['id']] = row
    return sorted(known.values(), key=lambda row: row['id'].lower()), message
