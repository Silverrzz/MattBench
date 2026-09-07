import copy

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_http_methods

from OpenBench.config import fingerprint, read_site_config
from OpenBench.models import EngineConfig, OpeningBook, Runner, RunnerRelease, Variant
from OpenBench.views import render


SECTIONS = {'engines': ('Engines', EngineConfig), 'books': ('Books', OpeningBook),
            'variants': ('Variants', Variant), 'runners': ('Runners', Runner),
            'releases': ('Runner releases', RunnerRelease), 'site': ('Site settings', None)}
RELATIONS = {OpeningBook: ('variant', Variant), Variant: ('runner_release', RunnerRelease),
             RunnerRelease: ('runner', Runner)}


def entry_version(instance):
    related = [str(getattr(instance, field + '_id')) for field in ('variant', 'runner_release', 'runner') if hasattr(instance, field + '_id')]
    if isinstance(instance, EngineConfig) and not instance._state.adding:
        related += sorted(str(pk) for pk in instance.variants.values_list('pk', flat=True))
    return fingerprint([instance.name, instance.enabled, instance.settings, related])


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def manage(request, section='engines', identifier=None):

    if section not in SECTIONS:
        raise Http404
    if not request.user.is_active or not request.user.is_superuser:
        raise PermissionDenied
    title, model = SECTIONS[section]
    context = {'title': title, 'page_title': 'Manage', 'section': section, 'admin': True,
               'active_section': 'runners' if section == 'releases' else section,
               'singular': {'engines': 'engine', 'books': 'book', 'variants': 'variant', 'runners': 'runner', 'releases': 'release'}.get(section),
               'navigation': [(key, label) for key, (label, _) in SECTIONS.items() if key != 'releases']}
    if section == 'site':
        if request.method != 'GET' or identifier is not None:
            raise PermissionDenied
        context['site_settings'] = [(key, value) for key, value in read_site_config().items() if key != 'variants']
        return render(request, 'configuration.html', context)
    if identifier is None:
        if request.method != 'GET':
            raise PermissionDenied
        objects = model.objects.order_by('name')
        if model in RELATIONS:
            objects = objects.select_related(RELATIONS[model][0])
        if model is Runner:
            objects = objects.prefetch_related('releases')
        context['objects'] = objects
        return render(request, 'configuration.html', context)

    instance = model() if identifier == 'new' else get_object_or_404(model, pk=identifier)
    original = entry_version(instance)
    values = copy.deepcopy(instance.settings)
    values.update(name=instance.name, enabled=instance.enabled)
    relation = RELATIONS.get(model)
    if relation:
        field, related_model = relation
        values[field] = str(getattr(instance, field + '_id') or request.GET.get(field, ''))
        context.update(relation_name=field, relation_label=field.replace('_', ' ').title(),
                       related_objects=related_model.objects.order_by('name'))
    if section == 'engines':
        build = values.pop('build', {})
        values.update(path=build.get('path', ''), compilers='\n'.join(build.get('compilers', [])),
                      systems='\n'.join(build.get('systems', [])), cpuflags='\n'.join(build.get('cpuflags', [])))
        context['variants'] = Variant.objects.order_by('name')
        context['selected_variants'] = [str(pk) for pk in instance.variants.values_list('pk', flat=True)] if identifier != 'new' else []
    if request.method == 'POST':
        values.update(request.POST.dict())
        for field in ('enabled', 'private', 'syzygy'):
            values[field] = request.POST.get(field) == 'on'
        if section == 'engines':
            context['selected_variants'] = request.POST.getlist('variants')
        try:
            with transaction.atomic():
                if identifier != 'new':
                    instance = get_object_or_404(model.objects.select_for_update(), pk=identifier)
                    if request.POST.get('version') != entry_version(instance):
                        raise ValidationError('This entry changed; reload before saving')
                    if request.POST.get('name') != instance.name:
                        raise ValidationError('Names cannot be changed')
                else:
                    instance.name = request.POST.get('name', '').strip()
                instance.enabled = values['enabled']
                data = copy.deepcopy(instance.settings)
                if section in ('engines', 'books', 'runners'):
                    data['source'] = request.POST.get('source', '').strip()
                if relation and not (section == 'releases' and identifier != 'new'):
                    field, related_model = relation
                    related = get_object_or_404(related_model, pk=request.POST.get(field))
                    setattr(instance, field, related)
                if section == 'engines':
                    data.update(private=values['private'], nps=int(request.POST.get('nps', '0')))
                    data['build'] = {'path': request.POST.get('path', '').strip(),
                                     **{field: list(dict.fromkeys(line.strip() for line in request.POST.get(field, '').splitlines() if line.strip()))
                                        for field in ('compilers', 'systems', 'cpuflags')}}
                    if data['build']['path'] == '""':
                        data['build']['path'] = ''
                    selected = list(Variant.objects.filter(pk__in=context['selected_variants']))
                    if len(selected) != len(set(context['selected_variants'])):
                        raise ValidationError('Unknown variant')
                    if instance.enabled and (not selected or any(not variant.enabled for variant in selected)):
                        raise ValidationError('Enabled engines require enabled variants')
                    data['variants'] = sorted(variant.name for variant in selected)
                elif section == 'books':
                    data['sha'] = request.POST.get('sha', '').strip()
                    data['variant'] = instance.variant.name
                    data.pop('format', None)
                    if instance.enabled and not instance.variant.enabled:
                        raise ValidationError('Choose an enabled variant')
                elif section == 'variants':
                    data = {'fastchess_variant': request.POST.get('fastchess_variant', '').strip(), 'syzygy': values['syzygy']}
                elif section == 'releases' and identifier == 'new':
                    data = {'ref': request.POST.get('ref', '').strip(), 'min_version': request.POST.get('min_version', '').strip(), 'protocol': 'fastchess-ob'}
                    if request.POST.get('commit', '').strip():
                        data['commit'] = request.POST['commit'].strip()
                if not instance.enabled and identifier != 'new':
                    if section == 'runners' and instance.releases.filter(enabled=True).exists():
                        raise ValidationError('Disable the runner releases first')
                    if section == 'releases' and Variant.objects.filter(runner_release=instance, enabled=True).exists():
                        raise ValidationError('Disable the variants using this release first')
                    if section == 'variants' and (instance.engines.filter(enabled=True).exists() or OpeningBook.objects.filter(variant=instance, enabled=True).exists()):
                        raise ValidationError('Disable or reassign the engines and books using this variant first')
                instance.settings = data
                instance.full_clean()
                instance.save()
                if section == 'engines':
                    instance.variants.set(selected)
            request.session['status_message'] = '%s saved.' % instance
            return redirect('/manage/%s/' % ('runners' if section == 'releases' else section))
        except (ValidationError, IntegrityError, ValueError) as error:
            context['errors'] = error.messages if isinstance(error, ValidationError) else ['Invalid values or duplicate name.']
    if relation:
        context['selected_relation'] = values[relation[0]]
    context.update(editing=True, is_new=identifier == 'new', values=values,
                   version=request.POST.get('version', original), instance=instance,
                   immutable_release=section == 'releases' and identifier != 'new')
    return render(request, 'configuration.html', context)
