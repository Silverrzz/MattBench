import copy

from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
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
            'releases': ('Runner releases', RunnerRelease), 'site': ('Site settings', None), 'notifications': ('Notifications', None)}
RELATIONS = {Variant: ('runner_release', RunnerRelease),
             RunnerRelease: ('runner', Runner)}


def entry_version(instance):
    related = [str(getattr(instance, field + '_id')) for field in ('variant', 'runner_release', 'runner') if hasattr(instance, field + '_id')]
    if isinstance(instance, (EngineConfig, OpeningBook)) and not instance._state.adding:
        related += sorted(str(pk) for pk in instance.variants.values_list('pk', flat=True))
    if isinstance(instance, EngineConfig) and not instance._state.adding:
        related.append(['maintainers', *sorted(str(pk) for pk in instance.maintainers.values_list('pk', flat=True))])
    return fingerprint([instance.name, instance.enabled, instance.settings, related])


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def manage(request, section='engines', identifier=None):

    if section not in SECTIONS or section == 'notifications':
        raise Http404
    staff = request.user.is_staff or request.user.is_superuser
    if not request.user.is_active:
        raise PermissionDenied
    if not request.user.is_superuser and section != 'engines':
        raise PermissionDenied
    if not staff and (identifier == 'new' or not EngineConfig.objects.filter(maintainers=request.user).exists()):
        raise PermissionDenied
    if not staff and 'maintainers' in request.POST:
        raise PermissionDenied
    title, model = SECTIONS[section]
    context = {'title': title, 'page_title': 'Manage', 'section': section, 'admin': True,
               'active_section': 'runners' if section == 'releases' else section,
               'singular': {'engines': 'engine', 'books': 'book', 'variants': 'variant', 'runners': 'runner', 'releases': 'release'}.get(section),
               'can_create': staff, 'can_manage_maintainers': staff,
               'navigation': [(key, label) for key, (label, _) in SECTIONS.items() if key != 'releases' and (request.user.is_superuser or key == 'engines')]}
    if section == 'site':
        if request.method != 'GET' or identifier is not None:
            raise PermissionDenied
        context['site_settings'] = [(key, value) for key, value in read_site_config().items() if key != 'variants']
        return render(request, 'configuration.html', context)
    if identifier is None:
        if request.method != 'GET':
            raise PermissionDenied
        objects = model.objects.order_by('name')
        if not staff:
            objects = objects.filter(maintainers=request.user)
        if model in RELATIONS:
            objects = objects.select_related(RELATIONS[model][0])
        if model in (EngineConfig, OpeningBook):
            objects = objects.prefetch_related('variants')
        if model is Runner:
            objects = objects.prefetch_related('releases')
        context['objects'] = objects
        return render(request, 'configuration.html', context)

    instance = model() if identifier == 'new' else get_object_or_404(model, pk=identifier)
    if not staff and not instance.maintainers.filter(pk=request.user.pk).exists():
        raise PermissionDenied
    original = entry_version(instance)
    values = copy.deepcopy(instance.settings)
    values.update(name=instance.name, enabled=instance.enabled)
    relation = RELATIONS.get(model)
    if relation:
        field, related_model = relation
        values[field] = str(getattr(instance, field + '_id') or request.GET.get(field, ''))
        context.update(relation_name=field, relation_label=field.replace('_', ' ').title(),
                       related_objects=related_model.objects.order_by('name'))
    if section in ('engines', 'books'):
        context['variants'] = Variant.objects.order_by('name')
        context['selected_variants'] = [str(pk) for pk in instance.variants.values_list('pk', flat=True)] if identifier != 'new' else [request.GET.get('variant', '')]
    if section == 'engines':
        if staff:
            context['maintainer_accounts'] = get_user_model().objects.order_by('username')
            context['selected_maintainers'] = [str(pk) for pk in instance.maintainers.values_list('pk', flat=True)] if identifier != 'new' else []
        build = values.pop('build', {})
        values.update(path=build.get('path', ''), compilers='\n'.join(build.get('compilers', [])),
                      systems='\n'.join(build.get('systems', [])), cpuflags='\n'.join(build.get('cpuflags', [])))
        context['variants'] = Variant.objects.order_by('name')
        context['selected_variants'] = [str(pk) for pk in instance.variants.values_list('pk', flat=True)] if identifier != 'new' else []
    if request.method == 'POST':
        values.update(request.POST.dict())
        for field in ('enabled', 'private', 'syzygy'):
            values[field] = request.POST.get(field) == 'on'
        if section in ('engines', 'books'):
            context['selected_variants'] = request.POST.getlist('variants')
        if section == 'engines' and staff:
            context['selected_maintainers'] = request.POST.getlist('maintainers')
        try:
            with transaction.atomic():
                if identifier != 'new':
                    instance = get_object_or_404(model.objects.select_for_update(), pk=identifier)
                    if not staff and not instance.maintainers.filter(pk=request.user.pk).exists():
                        raise PermissionDenied
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
                if section in ('engines', 'books'):
                    selected = list(Variant.objects.filter(pk__in=context['selected_variants']))
                    if len(selected) != len(set(context['selected_variants'])):
                        raise ValidationError('Unknown variant')
                    if instance.enabled and (not selected or any(not variant.enabled for variant in selected)):
                        raise ValidationError('Enabled entries require enabled variants')
                    data['variants'] = sorted(variant.name for variant in selected)
                if section == 'engines':
                    data.update(private=values['private'], nps=int(request.POST.get('nps', '0')))
                    data['build'] = {'path': request.POST.get('path', '').strip(),
                                     **{field: list(dict.fromkeys(line.strip() for line in request.POST.get(field, '').splitlines() if line.strip()))
                                        for field in ('compilers', 'systems', 'cpuflags')}}
                    if data['build']['path'] == '""':
                        data['build']['path'] = ''
                elif section == 'books':
                    data['sha'] = request.POST.get('sha', '').strip()
                    data.pop('variant', None)
                    data.pop('format', None)
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
                    if section == 'variants' and (instance.engines.filter(enabled=True).exists() or OpeningBook.objects.filter(variants=instance, enabled=True).exists()):
                        raise ValidationError('Disable or reassign the engines and books using this variant first')
                instance.settings = data
                instance.full_clean()
                instance.save()
                if section in ('engines', 'books'):
                    instance.variants.set(selected)
                if section == 'engines' and staff:
                    identifiers = context['selected_maintainers']
                    if any(not value.isascii() or not value.isdigit() or len(value) > 20 for value in identifiers):
                        raise ValidationError('Choose existing OpenBench accounts as maintainers.')
                    maintainers = list(get_user_model().objects.filter(pk__in=identifiers))
                    if len(maintainers) != len(set(identifiers)):
                        raise ValidationError('Unknown maintainer account.')
                    instance.maintainers.set(maintainers)
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
