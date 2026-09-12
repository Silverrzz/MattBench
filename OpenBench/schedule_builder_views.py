import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from OpenBench.models import EngineConfig, TrainingSchedule
from OpenBench.schedule_builder import DEFAULT_SPEC, SOURCE, builder_state, generate_schedule
from OpenBench.training_views import enabled, error_text, schedules_for


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def schedule_builder(request, schedule_id=None):
    from OpenBench.views import render
    enabled(request.user)
    selected = get_object_or_404(schedules_for(request.user), pk=schedule_id) if schedule_id else None
    spec, unchanged = builder_state(selected) if selected else (DEFAULT_SPEC, True)
    editable = not selected or selected.owner_id == request.user.pk or selected.owner_id is None and request.user.is_superuser
    if request.method == 'POST':
        try:
            if len(request.body) > 2 * 1024 * 1024:
                raise ValidationError('Builder configuration is too large.')
            data = json.loads(request.body)
            if not isinstance(data, dict) or data.get('action') not in ('preview', 'save'):
                raise ValidationError('Invalid builder request.')
            normalized, files, config = generate_schedule(data.get('spec'))
            if data['action'] == 'preview':
                return JsonResponse({'source': files[SOURCE]})
            if selected:
                if not editable:
                    raise PermissionDenied
                if not unchanged:
                    return JsonResponse({'error': 'The source was edited outside the builder. Reopen the builder to save a copy.'}, status=409)
            scope = data.get('scope')
            if scope not in ('personal', 'engine', 'global'):
                raise ValidationError('Choose a schedule scope.')
            if scope != 'personal' and not request.user.is_superuser:
                raise PermissionDenied
            engine = None
            if scope == 'engine':
                engine = EngineConfig.objects.filter(pk=data.get('engine'), enabled=True).first()
                if not engine:
                    raise ValidationError('Choose an engine.')
            name = data.get('name', '')
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
                raise ValidationError('Enter a schedule name of at most 128 characters.')
            values = {
                'name': name.strip(), 'engine': engine, 'owner': request.user if scope == 'personal' else None,
                'files': files, 'settings': config, 'updated': timezone.now(),
            }
            if selected:
                if type(data.get('version')) is not int:
                    raise ValidationError('Reload this schedule before saving.')
                changed = TrainingSchedule.objects.filter(pk=selected.pk, version=data['version']).update(**values, version=F('version') + 1)
                if not changed:
                    return JsonResponse({'error': 'This schedule changed in another tab. Reload or save a copy.'}, status=409)
                selected.refresh_from_db()
            else:
                selected = TrainingSchedule.objects.create(**values)
            return JsonResponse({
                'id': str(selected.pk), 'version': selected.version,
                'url': '/training/schedules/%s/builder/' % selected.pk,
                'source_url': '/training/schedules/%s/' % selected.pk,
                'train_url': '/training/new/?schedule=%s%s' % (selected.pk, '&engine=%s' % engine.pk if engine else ''),
                'source': files[SOURCE], 'spec': normalized,
            })
        except (ValidationError, ValueError, TypeError, IntegrityError) as error:
            return JsonResponse({'error': 'A schedule with this name already exists.' if isinstance(error, IntegrityError) else error_text(error)}, status=400)
    copy = bool(selected and (not unchanged or not editable or request.GET.get('copy')))
    notice = ''
    if selected and spec is None:
        notice = 'This schedule has no builder settings. Start a new schedule here, or use the source editor to edit the original.'
    elif selected and not unchanged:
        notice = 'The Rust source or build settings have been edited. Saving here creates a copy from the last builder settings.'
    elif selected and not editable:
        notice = 'Saving creates a personal copy of this shared schedule.'
    spec, files, config = generate_schedule(spec or DEFAULT_SPEC)
    payload = {
        'id': str(selected.pk) if selected and not copy else '', 'version': selected.version if selected and not copy else 0,
        'spec': spec, 'source': files[SOURCE], 'notice': notice,
        'wdl_model_defaults': {key: DEFAULT_SPEC[key] for key in ('wdl_model_params_a', 'wdl_model_params_b', 'material_min', 'material_max', 'mom_target', 'wdl_heuristic_scale')},
        'name': selected.name + (' copy' if copy else '') if selected else '',
        'engine': str(selected.engine_id) if selected and selected.engine_id else request.GET.get('engine', ''),
        'scope': selected.scope if selected and not copy else 'personal',
        'post_url': '/training/schedules/%s/builder/' % selected.pk if selected and not copy else '/training/schedules/builder/',
    }
    return render(request, 'schedule_builder.html', {
        'page_title': 'Schedule builder', 'training_tab': 'schedules', 'builder_data': payload,
        'engines': EngineConfig.objects.filter(enabled=True).order_by('name'),
    })
