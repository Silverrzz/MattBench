import re
import uuid
from urllib.parse import quote

import requests
from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect

from OpenBench.bullet import BULLET_REPOSITORY, DEFAULT_BULLET_COMMIT, pinned_bullet_commit
from OpenBench.models import TrainingConfiguration


class BulletRevisionForm(forms.Form):
    version = forms.CharField(widget=forms.HiddenInput)
    bullet_ref = forms.CharField(label='Bullet branch, tag or commit', max_length=200)

    def clean_bullet_ref(self):
        ref = self.cleaned_data['bullet_ref']
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', ref) or ref.startswith('-'):
            raise ValidationError('Enter a valid Bullet branch, tag or commit.')
        try:
            response = requests.get('https://api.github.com/repos/jw1912/bullet/commits/' + quote(ref, safe=''), timeout=20)
            if response.status_code == 404:
                raise ValidationError('This Bullet revision does not exist.')
            response.raise_for_status()
            commit = response.json()['sha']
            if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
                raise ValueError
        except (requests.RequestException, ValueError, KeyError, TypeError):
            raise ValidationError('Could not verify the Bullet revision. Try again.') from None
        return commit


def manage_training(request, context):
    from OpenBench.views import render
    current = pinned_bullet_commit()
    data = request.POST.copy() if request.method == 'POST' else None
    if data is not None and data.get('action') == 'latest':
        data['bullet_ref'] = 'main'
    form = BulletRevisionForm(data, initial={'version': current, 'bullet_ref': current})
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            config, _ = TrainingConfiguration.objects.get_or_create(pk=1, defaults={'bullet_commit': DEFAULT_BULLET_COMMIT})
            config = TrainingConfiguration.objects.select_for_update().get(pk=config.pk)
            if form.cleaned_data['version'] != config.bullet_commit:
                form.add_error(None, 'The pinned revision changed in another tab. Reload before saving.')
            else:
                config.bullet_candidate = form.cleaned_data['bullet_ref']
                config.bullet_validation = 'QUEUED'
                config.bullet_validation_base = config.bullet_commit
                config.bullet_validation_message = 'Waiting to check generated trainer compatibility. The current revision remains active.'
                config.bullet_validation_token = uuid.uuid4()
                config.save(update_fields=['bullet_candidate', 'bullet_validation', 'bullet_validation_base', 'bullet_validation_message', 'bullet_validation_token'])
                request.session['status_message'] = 'Compatibility check queued. The revision will be pinned only after it passes.'
                return redirect('/manage/training/')
    context.update(bullet_form=form, bullet_commit=current, bullet_repository=BULLET_REPOSITORY, bullet_configuration=TrainingConfiguration.objects.filter(pk=1).first())
    return render(request, 'configuration.html', context)
