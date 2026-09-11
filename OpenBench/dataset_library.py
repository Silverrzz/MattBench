import logging
import re

from django import forms
from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from OpenBench.models import TrainingDataset
from OpenBench.training import DATA_SUFFIXES, hf_token, relative_path, repo_id


logger = logging.getLogger(__name__)


class DatasetForm(forms.ModelForm):
    class Meta:
        model = TrainingDataset
        fields = ['name', 'repo', 'revision', 'patterns', 'notes']
        labels = {'repo': 'Hugging Face repository', 'patterns': 'Included files'}
        widgets = {'patterns': forms.Textarea(attrs={'rows': 3}), 'notes': forms.Textarea(attrs={'rows': 3})}
        help_texts = {'patterns': 'One file path or wildcard per line. Use * for all supported training files.'}

    def clean_repo(self):
        return repo_id(self.cleaned_data['repo'])

    def clean_patterns(self):
        value = self.cleaned_data['patterns']
        if len(value) > 4096 or not value.strip():
            raise ValidationError('Enter file paths or wildcards, up to 4096 characters.')
        return '\n'.join(line.strip() for line in value.splitlines() if line.strip())


def refresh_metadata(dataset):
    from huggingface_hub import HfApi
    from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError, RevisionNotFoundError
    token = hf_token(dataset.owner_id)
    try:
        info = HfApi(token=token).dataset_info(dataset.repo, revision=dataset.revision, files_metadata=True, timeout=20)
    except GatedRepoError:
        raise ValidationError('This dataset is gated. Request access on Hugging Face and check your token permissions.') from None
    except RevisionNotFoundError:
        raise ValidationError('This dataset revision does not exist. Check the branch, tag or commit.') from None
    except RepositoryNotFoundError:
        raise ValidationError('Dataset not found or inaccessible. Check the repository name and your Hugging Face access.') from None
    except Exception:
        logger.exception('Dataset metadata lookup failed for %s', dataset.repo)
        raise ValidationError('Could not verify this repository with Hugging Face. Please retry; no changes were saved.') from None
    if not isinstance(info.sha, str) or not re.fullmatch(r'[0-9a-f]{40}', info.sha):
        raise ValidationError('Could not verify the dataset revision. Please retry.')
    from OpenBench.dataset_manifest import resolve_dataset, remaining_steps
    metadata = resolve_dataset(dataset.repo, info, dataset.patterns.splitlines(), token)
    files = metadata['files']
    dataset.metadata = {**metadata, 'commit': info.sha, 'file_count': len(files), 'size': sum(file['size'] for file in files), 'remaining_steps': remaining_steps(metadata, {})}
    dataset.checked = timezone.now()


def public_repository(repo):
    from huggingface_hub import HfApi
    key = 'dataset-public:' + repo
    public = cache.get(key)
    if public is None:
        try:
            info = HfApi(token=False).dataset_info(repo, timeout=5)
            public = info.private is False
        except Exception:
            public = False
        cache.set(key, public, 60)
    return public


@require_http_methods(['GET', 'POST'])
def library(request, dataset_id=None, create=False):
    from OpenBench.training_views import enabled
    from OpenBench.views import render, redirect
    if create or request.method == 'POST':
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), login_url='/login/')
        enabled(request.user)
    rows = TrainingDataset.objects.select_related('owner')
    selected = get_object_or_404(rows, pk=dataset_id) if dataset_id else None
    editable = create or bool(selected and request.user.is_authenticated and selected.owner_id == request.user.pk)
    if selected and not editable and not public_repository(selected.repo):
        raise Http404
    if request.method == 'POST' and not editable:
        raise PermissionDenied
    if not selected and not create:
        rows = [row for row in rows if request.user.is_authenticated and row.owner_id == request.user.pk or public_repository(row.repo)]
    initial = {'name': request.GET.get('name', ''), 'repo': request.GET.get('repo', ''), 'revision': request.GET.get('revision', 'main'), 'patterns': request.GET.get('files', '*')}
    form = DatasetForm(request.POST if request.method == 'POST' else None, instance=selected, initial=initial if create else None) if selected or create else None
    if form and not editable:
        for field in form.fields.values():
            field.disabled = True
    feedback = ''
    refresh_error = ''
    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if selected and action == 'delete':
            selected.delete()
            return redirect(request, '/training/datasets/')
        if selected and action in ('archive', 'restore'):
            selected.archived = action == 'archive'
            selected.save(update_fields=['archived', 'updated'])
            return redirect(request, '/training/datasets/')
        if selected and action == 'refresh':
            form = DatasetForm(instance=selected)
            try:
                refresh_metadata(selected)
                selected.save(update_fields=['metadata', 'checked', 'updated'])
                feedback = 'Contents and file statistics refreshed.'
            except ValidationError as error:
                refresh_error = ' '.join(error.messages)
            except Exception:
                logger.exception('Dataset refresh failed for %s', selected.pk)
                refresh_error = 'Could not refresh this repository. Check its revision and your Hugging Face access, then retry.'
        elif action == 'save' and form and form.is_valid():
            dataset = form.save(commit=False)
            dataset.owner = request.user
            try:
                refresh_metadata(dataset)
                with transaction.atomic():
                    dataset.save()
                return redirect(request, '/training/datasets/%s/' % dataset.pk)
            except IntegrityError:
                form.add_error('name', 'You already registered a dataset with this name.')
            except ValidationError as error:
                form.add_error(None, error)
            except Exception:
                logger.exception('Dataset registration failed for %s', dataset.repo)
                form.add_error(None, 'Could not verify and save this dataset. Please retry; no changes were saved.')
    return render(request, 'dataset_library.html', {
        'page_title': selected.name if selected else 'Register dataset' if create else 'Datasets',
        'training_tab': 'datasets', 'datasets': rows, 'selected': selected, 'form': form, 'editable': editable, 'feedback': feedback, 'refresh_error': refresh_error,
    }, always_allow=True)
