import json
import uuid

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods

from OpenBench.config import fingerprint
from OpenBench.models import EngineConfig, Profile, WorkloadPreset


def preset_fields(kind):
    from OpenBench.config import verify_engine_test_preset, verify_engine_tune_preset, verify_engine_datagen_preset
    return {'TEST': verify_engine_test_preset, 'TUNE': verify_engine_tune_preset, 'DATAGEN': verify_engine_datagen_preset}[kind]({})


def preset_version(rows):
    return fingerprint([[str(row.pk), row.name, row.settings, row.position] for row in rows])


def preset_data(user, kind):
    engines = list(EngineConfig.objects.filter(enabled=True))
    rows = list(WorkloadPreset.objects.filter(engine__in=engines, workload_type=kind).filter(Q(owner=None) | Q(owner=user)).select_related('engine'))
    hidden = {row.engine_id for row in rows if row.owner_id is None and row.name != 'default'}
    editable = {engine.name: user.is_active and user.is_superuser for engine in engines}
    return {
        'versions': {engine.name: {scope: preset_version([row for row in rows if row.engine_id == engine.pk and
                     (row.owner_id is None if scope == 'engine' else row.owner_id == user.pk)])
                     for scope in ('engine', 'personal')} for engine in engines},
        'editable': editable, 'fields': preset_fields(kind),
        'presets': [{'id': str(row.pk), 'engine': row.engine.name, 'name': row.name, 'scope': 'personal' if row.owner_id else 'engine',
            'settings': row.settings, 'position': row.position, 'editable': row.owner_id == user.pk or editable[row.engine.name]}
            for row in rows if not (row.owner_id is None and row.name == 'default' and row.engine_id in hidden)],
    }


@transaction.atomic
def change_preset(user, kind, data):
    engine = get_object_or_404(EngineConfig.objects.select_for_update(), name=data.get('engine'), enabled=True)
    if data.get('scope') not in ('personal', 'engine'):
        raise ValidationError('Choose where to save the preset')
    owner = user if data['scope'] == 'personal' else None
    if not user.is_active or (owner is None and not user.is_superuser):
        raise PermissionDenied
    siblings = engine.presets.filter(owner=owner, workload_type=kind)
    if data.get('version') != preset_version(siblings):
        raise ValidationError('Presets changed; reload before saving')
    visible = siblings.exclude(name='default') if owner is None and siblings.exclude(name='default').exists() else siblings
    if data.get('action') == 'manage':
        entries = data.get('presets')
        if not isinstance(entries, list) or any(not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not isinstance(entry.get('name'), str) for entry in entries):
            raise ValidationError('Invalid preset list')
        rows = {str(row.pk): row for row in visible}
        ids = [entry['id'] for entry in entries]
        names = [entry['name'].strip() for entry in entries]
        if len(set(ids)) != len(ids) or not set(ids).issubset(rows):
            raise ValidationError('Preset list changed; reload before saving')
        if len(set(names)) != len(names) or any(not name or len(name) > 128 for name in names):
            raise ValidationError('Use a unique name of 1 to 128 characters for each preset')
        if owner is None and 'default' in names and len(names) > 1:
            raise ValidationError('Choose a name other than default for a shared preset')
        if not entries:
            siblings.delete()
        else:
            visible.exclude(pk__in=ids).delete()
            for identifier in ids:
                siblings.filter(pk=identifier).update(name=uuid.uuid4().hex)
            for position, (identifier, name) in enumerate(zip(ids, names)):
                row = rows[identifier]
                row.name, row.position = name, position
                row.full_clean()
                row.save()
    elif data.get('action') == 'save':
        if not isinstance(data.get('name'), str):
            raise ValidationError('Enter a preset name')
        if data.get('id'):
            row = get_object_or_404(siblings, pk=data['id'])
        else:
            row = WorkloadPreset(engine=engine, owner=owner, workload_type=kind,
                position=max((row.position for row in visible), default=-1) + 1)
        row.name, row.settings = data['name'].strip(), data.get('settings', {})
        if owner is None and row.name == 'default' and siblings.exclude(pk=row.pk).exists():
            raise ValidationError('Choose a name other than default for a shared preset')
        row.full_clean()
        row.save()
    else:
        raise ValidationError('Unknown preset action')


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def presets(request, kind):
    if kind not in ('TEST', 'TUNE', 'DATAGEN'):
        return JsonResponse({'error': 'Unknown workload type'}, status=400)
    if not request.user.is_active or not Profile.objects.filter(user=request.user, enabled=True).exists():
        raise PermissionDenied
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            if not isinstance(data, dict):
                raise ValidationError('Invalid preset request')
            change_preset(request.user, kind, data)
        except (ValidationError, ValueError, TypeError, IntegrityError) as error:
            message = '; '.join(error.messages) if isinstance(error, ValidationError) else 'Invalid preset or duplicate name'
            return JsonResponse({'error': message}, status=400)
    return JsonResponse(preset_data(request.user, kind))
