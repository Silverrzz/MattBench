from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_http_methods

from OpenBench.configuration import authorize, save_configuration
from OpenBench.configuration_forms import ConfigurationForm
from OpenBench.configuration_schema import DEFAULT_SITE
from OpenBench.models import ConfigurationRevision, EngineConfig, OpeningBook, Runner, RunnerRelease, SiteSettings, Variant
from OpenBench.views import render


SECTIONS = {'engines': ('Engines', EngineConfig), 'books': ('Books', OpeningBook), 'variants': ('Variants', Variant),
    'runners': ('Runners', Runner), 'releases': ('Runner releases', RunnerRelease), 'site': ('Site settings', SiteSettings),
    'history': ('Configuration history', ConfigurationRevision)}


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
def manage(request, section='engines', identifier=None):
    if section not in SECTIONS:
        raise Http404
    if not request.user.is_active or (section != 'engines' and not request.user.is_superuser):
        raise PermissionDenied
    if not request.user.is_superuser and not EngineConfig.objects.filter(maintainers__user=request.user).exists():
        raise PermissionDenied
    title, model = SECTIONS[section]
    generation = SiteSettings.objects.filter(pk=1).values_list('generation', flat=True).first() or 0
    navigation = [(key, label) for key, (label, _) in SECTIONS.items() if key != 'releases' and (request.user.is_superuser or key == 'engines')]
    context = {'title': title, 'section': section, 'navigation': navigation, 'generation': generation}
    context['active_section'] = {'releases': 'runners'}.get(section, section)
    context['singular'] = {'engines': 'engine', 'books': 'opening book', 'variants': 'variant', 'runners': 'runner', 'releases': 'release'}.get(section, title.lower())
    context['query'] = request.GET.get('q', '').strip()
    if section == 'history':
        if request.method != 'GET':
            raise PermissionDenied
        context['history'] = Paginator(ConfigurationRevision.objects.select_related('actor').order_by('-generation'), 30).get_page(request.GET.get('page'))
    elif identifier is None and section != 'site':
        if request.method != 'GET':
            raise PermissionDenied
        objects = model.objects.order_by('name')
        if section == 'engines':
            objects = objects.prefetch_related('maintainers__user', 'variants')
        if not request.user.is_superuser:
            objects = objects.filter(maintainers__user=request.user)
        if section == 'books': objects = objects.select_related('variant')
        if section == 'variants': objects = objects.select_related('runner_release__runner')
        if section == 'runners': objects = objects.prefetch_related('releases')
        if section == 'releases': objects = objects.select_related('runner')
        if context['query']: objects = objects.filter(name__icontains=context['query'])
        context['objects'] = objects
        context['count'] = objects.count()
        context['can_add'] = request.user.is_superuser
    else:
        if section == 'site':
            instance = SiteSettings.objects.filter(pk=1).first() or SiteSettings(settings=dict(DEFAULT_SITE))
        elif identifier == 'new':
            instance = model()
        else:
            instance = get_object_or_404(model, pk=identifier)
        if identifier == 'new' and request.method == 'GET':
            for field, related in {'runner': Runner, 'runner_release': RunnerRelease, 'variant': Variant}.items():
                if hasattr(instance, field + '_id') and request.GET.get(field):
                    try:
                        setattr(instance, field, get_object_or_404(related, pk=request.GET[field]))
                    except ValidationError:
                        raise Http404
        authorize(instance, request.user)
        form = ConfigurationForm(instance, generation, request.user, request.POST if request.method == 'POST' else None)
        if request.method == 'POST' and form.is_valid():
            try:
                save_configuration(form.populate(), request.user, form.cleaned_data['generation'],
                    variants=form.cleaned_data.get('variants'), maintainers=form.cleaned_data.get('maintainers'))
                request.session['status_message'] = '%s saved.' % title
                return redirect('/manage/%s/' % section)
            except (ValidationError, IntegrityError) as error:
                form.add_error(None, '; '.join(error.messages) if isinstance(error, ValidationError) else 'This name or relationship already exists.')
        context['is_new'] = instance._state.adding
        context.update(form=form, instance=instance, immutable_release=isinstance(instance, RunnerRelease) and not instance._state.adding)
    return render(request, 'configuration.html', context)
