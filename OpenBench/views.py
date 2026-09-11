# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import csv, io, os, json, secrets

import django.http
import django.shortcuts
import django.contrib.auth

import OpenBench.config
import OpenBench.model_utils
import OpenBench.spsa_utils
import OpenBench.utils

from OpenBench.workloads.create_workload import create_workload
from OpenBench.workloads.get_workload import get_workload
from OpenBench.workloads.modify_workload import modify_workload
from OpenBench.workloads.verify_workload import verify_workload
from OpenBench.workloads.view_workload import view_workload, fetch_results, fetch_result_summaries

from OpenBench.config import OPENBENCH_CONFIG, eligibility_fingerprint, OPENBENCH_STATIC_VERSION
from OpenSite.settings import PROJECT_PATH

from OpenBench.models import *
from django.contrib.auth.models import User
from OpenSite.settings import MEDIA_ROOT

from django.db import transaction
from django.db.models import F, Q
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.files.storage import FileSystemStorage
from django.core.files.base import ContentFile
from django.utils import timezone

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                              GENERAL UTILITIES                              #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

ERROR_MESSAGES = {
    'disabled'            : 'Account has not been enabled. Contact an Administrator',
    'fakeuser'            : 'This is not a real OpenBench User. Create an OpenBench account',
    'requires_login'      : 'All pages require a user login to access',
    'manual_registration' : 'Registration can only be done via an Administrator',
}

class UnableToAuthenticate(Exception):
    pass

def render(request, template, content={}, always_allow=False, error=None, warning=None, status=None):

    data = content.copy()
    page_titles = {
        'index.html': 'Tests',
        'profile.html': 'Profile',
        'search.html': 'Search tests',
        'users.html': 'Contributors',
        'machines.html': 'Machines',
        'machine.html': 'Machine details',
        'networks.html': 'Networks',
        'network.html': 'Network details',
        'uploadnet.html': 'Upload network',
        'events.html': 'Events',
        'event.html': 'Event details',
        'errors.html': 'Errors',
        'login.html': 'Log in',
        'register.html': 'Create an account',
        'create_workload.html': 'New workload',
        'workload.html': 'Workload',
        'configuration.html': 'Engines',
    }
    title = page_titles.get(template, 'MattBench')
    if template == 'create_workload.html':
        title = {'TEST': 'New test', 'TUNE': 'New tune', 'DATAGEN': 'New datagen'}.get(data.get('workload'), title)
    if template == 'configuration.html':
        title = data.get('title', title)
    data.setdefault('page_title', title)
    data.update({ 'config' : dict(OPENBENCH_CONFIG) })
    static_directory = os.path.join(os.path.dirname(__file__), 'static')
    with os.scandir(static_directory) as static_files:
        static_revision = max((asset.stat().st_mtime_ns for asset in static_files if asset.is_file()), default=0)
    data.update({ 'static_version' : '%s-%s' % (OPENBENCH_STATIC_VERSION, static_revision) })

    if OPENBENCH_CONFIG['require_login_to_view']:
        if not request.user.is_authenticated and not always_allow:
            return redirect(request, '/login/',  error=ERROR_MESSAGES['requires_login'])

    if request.user.is_authenticated:

        profile = Profile.objects.filter(user=request.user)
        data.update({'profile' : profile.first()})

        if profile.first() and not profile.first().enabled:
            request.session['error_message'] = ERROR_MESSAGES['disabled']

        elif request.user.is_authenticated and not profile.first():
            request.session['error_message'] = ERROR_MESSAGES['fakeuser']

    if error:
        request.session['error_message'] = error

    if warning:
        request.session['warning_message'] = error

    if status:
        request.session['status_message'] = status

    response = django.shortcuts.render(request, 'OpenBench/{0}'.format(template), data)

    if hasattr(request, 'live_regions'):
        request.live_payload = {'regions': request.live_regions}
        if template == 'workload.html':
            workload = data['workload']
            request.live_payload['fields'] = {
                field: str(getattr(workload, field))
                for field in ('info', 'priority', 'throughput', 'workload_size')
            }
            request.live_payload['summary'] = fetch_result_summaries(workload)
            if workload.test_mode == 'SPRT':
                from OpenBench.llr_history import workload_llr_history
                request.live_payload['history'] = workload_llr_history(workload)
            if 'results' in request.live_subscriptions:
                request.live_payload['results'] = fetch_results(workload)
            if 'digest' in request.live_subscriptions and data['type'] == 'TUNE':
                request.live_payload['digest'] = OpenBench.spsa_utils.spsa_param_digest(workload)
        if template == 'networks.html':
            request.live_payload['networks'] = data['networks']
        if template == 'training_detail.html':
            run = data['run']
            request.live_payload['training'] = {'state': run.state, 'updated': run.updated.isoformat(), 'metrics': run.metrics, 'history': run.history}

    for key in ['status_message', 'warning_message', 'error_message']:
        if key in request.session: del request.session[key]

    return response

def redirect(request, destination, error=None, warning=None, status=None):

    if error:
        request.session['error_message'] = error

    if warning:
        request.session['warning_message'] = warning

    if status:
        request.session['status_message'] = status

    return django.http.HttpResponseRedirect(destination)

def authenticate(request, requireEnabled=False):

    try:
        user = django.contrib.auth.authenticate(
            username = request.POST['username'],
            password = request.POST['password'])

        if requireEnabled:
            profile = OpenBench.models.Profile.objects.get(user=user)
            if not profile.enabled: raise UnableToAuthenticate()

    except Exception:
        raise UnableToAuthenticate()

    if user is None:
        raise UnableToAuthenticate()

    return user

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                            ADMINISTRATIVE VIEWS                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def register(request):

    if request.method == 'GET':
        if not OPENBENCH_CONFIG['require_manual_registration']:
            return render(request, 'register.html', always_allow=True)
        return redirect(request, '/login/', error=ERROR_MESSAGES['manual_registration'])

    if request.POST['password1'] != request.POST['password2']:
        return redirect(request, '/register/', error='Passwords do not match')

    if not request.POST['username'].isalnum():
        return redirect(request, '/register/', error='Alpha-numeric usernames Only')

    if User.objects.filter(username=request.POST['username']):
        return redirect(request, '/register/', error='That username is already taken')

    email    = request.POST['email']
    username = request.POST['username']
    password = request.POST['password1']

    user = User.objects.create_user(username, email, password)
    django.contrib.auth.login(request, user)
    Profile.objects.create(user=user)

    return redirect(request, '/index/')

def login(request):

    if request.method == 'GET':
        return render(request, 'login.html', always_allow=True)

    try:
        django.contrib.auth.login(request, authenticate(request))
        return redirect(request, '/index/')

    except UnableToAuthenticate:
        return redirect(request, '/login/', error='Unable to authenticate user')

def logout(request):

    django.contrib.auth.logout(request)
    return redirect(request, '/index/', status='Logged out')

def profile(request):

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    if not Profile.objects.filter(user=request.user).first():
        return redirect(request, '/index/')

    if request.method == 'GET':
        return render(request, 'profile.html')

    changes_message = ''
    if request.user.email != request.POST['email']:
        changes_message += 'Updated email address to %s' % (request.POST['email'])
        request.user.email = request.POST['email']
        request.user.save()

    if request.POST['password1'] != request.POST['password2']:
        return redirect(request, '/profile/', status=changes_message, error='Passwords do not match')

    if request.POST['password1']:
        request.user.set_password(request.POST['password1'])
        request.user.save()
        django.contrib.auth.login(request, request.user)
        changes_message += '\nUpdated password'

    return redirect(request, '/profile/', status=changes_message.removeprefix('\n'))

def profile_config(request):

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    if not (profile := Profile.objects.filter(user=request.user).first()):
        return redirect(request, 'index')

    if request.method == 'GET':
        return render(request, 'profile.html')

    changes = ''

    if (engine := request.POST.get('default-status', profile.engine)) != profile.engine:
        changes += 'Set %s as the default, replacing %s\n' % (engine, profile.engine)
        profile.engine = engine

    for engine in json.loads(request.POST.get('deleted-repos', '[]')):
        profile.repos.pop(engine, False)
        changes += 'Deleted Engine: %s\n' % (engine)

    for (engine, current_repo) in profile.repos.items():
        repo_name = request.POST.get('engine-repo-%s' % (engine), '').removesuffix('/')
        repo = 'https://github.com/%s' % (repo_name)

        if repo != current_repo and repo_name:
            changes += 'Updated Engine: %s to use %s\n' % (engine, repo)
            profile.repos[engine] = repo

    if changes:
        profile.save()

    engine_name = request.POST.get('new-engine-name', 'None')
    engine_repo = request.POST.get('new-engine-repo', '').removesuffix('/')

    if engine_name != 'None' and engine_repo:

        if not engine_repo.startswith('https://github.com/'):
            return redirect(request, '/profile/', error='Repositories must be on Github')

        if not profile.engine:
            profile.engine = engine_name

        changes += 'Added Engine: %s at %s' % (engine_name, engine_repo)
        profile.repos[engine_name] = engine_repo
        profile.save()

    return redirect(request, '/profile/', status=changes)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                               TEST LIST VIEWS                               #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def index(request, page=1):

    page = max(1, int(page))
    if request.GET.get('kind') == 'training':
        return redirect(request, '/training/' if page == 1 else '/training/page/%d/' % page)

    pending   = OpenBench.utils.get_pending_tests()
    active    = OpenBench.utils.get_active_tests()
    completed = OpenBench.utils.get_completed_tests()

    kind = request.GET.get('kind', '')
    modes = {'tests': ('SPRT', 'GAMES'), 'tunes': ('SPSA',), 'datagen': ('DATAGEN',)}
    if kind not in modes:
        kind = ''
    if kind in modes:
        pending = pending.filter(test_mode__in=modes[kind])
        active = active.filter(test_mode__in=modes[kind])
        completed = completed.filter(test_mode__in=modes[kind])
    start, end, paging = OpenBench.utils.getPaging(completed, page, 'index')

    data = {
        'pending'   : pending,
        'active'    : OpenBench.utils.group_active_tests_by_priority(active),
        'completed' : completed[start:end],
        'paging'    : paging,
        'status'    : OpenBench.utils.getMachineStatus(),
        'test_kind': kind,
        'is_test_index': True,
    }

    return render(request, 'index.html', data)

def user(request, username, page=1):

    pending   = OpenBench.utils.get_pending_tests().filter(author=username)
    active    = OpenBench.utils.get_active_tests().filter(author=username)
    completed = OpenBench.utils.get_completed_tests().filter(author=username)

    start, end, paging = OpenBench.utils.getPaging(completed, int(page), 'user/%s' % (username))

    data = {
        'page_title': '%s\u2019s tests' % username,
        'pending'   : pending,
        'active'    : OpenBench.utils.group_active_tests_by_priority(active),
        'completed' : completed[start:end],
        'paging'    : paging,
        'status'    : OpenBench.utils.getMachineStatus(username),
    }

    return render(request, 'index.html', data)

def greens(request, page=1):

    completed = OpenBench.utils.get_completed_tests().filter(passed=True)
    start, end, paging = OpenBench.utils.getPaging(completed, int(page), 'greens')

    data = { 'completed' : completed[start:end], 'paging' : paging, 'page_title' : 'Passed tests' }
    return render(request, 'index.html', data)

def search(request):

    # Search uses GET so the parameters live in the URL and can be shared.
    # With no parameters at all, simply present the empty search form.

    if not (params := request.GET):
        return render(request, 'search.html', {})

    tests  = Test.objects.all()

    # Optional field-based filters, defaulting to no restriction

    if params.get('dev-engine'):
        tests = tests.filter(dev_engine=params['dev-engine'])

    if params.get('base-engine'):
        tests = tests.filter(base_engine=params['base-engine'])

    if params.get('workload-type'):
        tests = tests.filter(test_mode=params['workload-type'])

    if params.get('opening-book'):
        tests = tests.filter(book_name=params['opening-book'])

    if params.get('info-contains'):
        tests = tests.filter(info__icontains=params['info-contains'])

    if params.get('dev-network'):
        tests = tests.filter(dev_netname__icontains=params['dev-network'])

    if params.get('base-network'):
        tests = tests.filter(base_netname__icontains=params['base-network'])

    # Authors are space-separated; match any of them case-insensitively

    if authors := params.get('authors', '').split():
        query = Q()
        for author in authors:
            query |= Q(author__iexact=author)
        tests = tests.filter(query)

    # Test statuses. These default to shown, except for deleted, so the URL
    # only carries the deviations: hide-<status>, or show-deleted to opt in.

    if 'hide-greens' in params:
        tests = tests.annotate(x=F('elolower') + F('eloupper')).exclude(x__gte=0, passed=True)

    if 'hide-yellows' in params:
        tests = tests.exclude(failed=True, wins__gte=F('losses'))

    if 'hide-reds' in params:
        tests = tests.exclude(failed=True, wins__lt=F('losses'))

    if 'hide-blues' in params:
        tests = tests.annotate(x=F('elolower') + F('eloupper')).exclude(x__lt=0, passed=True)

    if 'hide-stopped' in params:
        tests = tests.exclude(passed=False, failed=False)

    if 'show-deleted' not in params:
        tests = tests.exclude(deleted=True)

    # Keywords match the dev branch name, ANDed against the database so we never
    # pull non-matching rows into Python. Any single keyword is enough to match.

    if keywords := params.get('keywords', '').split():
        query = Q()
        for keyword in keywords:
            query |= Q(dev__name__icontains=keyword)
        tests = tests.filter(query)

    # A workload is single-threaded only when both engines run with "Threads=1"

    dev_single  = Q(dev_options__contains='Threads=1 ')  | Q(dev_options__endswith='Threads=1')
    base_single = Q(base_options__contains='Threads=1 ') | Q(base_options__endswith='Threads=1')

    if params.get('threads') == 'single':
        tests = tests.filter(dev_single & base_single)

    elif params.get('threads') == 'multi':
        tests = tests.exclude(dev_single & base_single)

    # The time control type is determined by the shape of the stored string, so
    # it can be matched with prefix / substring lookups rather than in Python.

    TC      = OpenBench.utils.TimeControl
    tc_type = params.get('tc-type', '')

    if tc_type == TC.FIXED_NODES:
        tests = tests.filter(dev_time_control__startswith='N=')
    elif tc_type == TC.FIXED_DEPTH:
        tests = tests.filter(dev_time_control__startswith='D=')
    elif tc_type == TC.FIXED_TIME:
        tests = tests.filter(dev_time_control__startswith='MT=')
    elif tc_type == TC.CYCLIC:
        tests = tests.filter(dev_time_control__contains='/')
    elif tc_type == TC.FISCHER:
        tests = tests.exclude(dev_time_control__contains='=') \
                     .exclude(dev_time_control__contains='/')

    # A specific time control value is matched as a loose substring of the dev
    # control string, leaving it to the user to phrase it how it is stored.

    if tc_value := params.get('tc-value-input', ''):
        tests = tests.filter(dev_time_control__contains=tc_value)

    filtered = list(tests)

    # Echo the submitted values back so the form stays populated for tweaking

    form = {
        'keywords'      : params.get('keywords', ''),
        'info'          : params.get('info-contains', ''),
        'authors'       : params.get('authors', ''),
        'dev_engine'    : params.get('dev-engine', ''),
        'base_engine'   : params.get('base-engine', ''),
        'dev_network'   : params.get('dev-network', ''),
        'base_network'  : params.get('base-network', ''),
        'workload_type' : params.get('workload-type', ''),
        'book'          : params.get('opening-book', ''),
        'tc_type'       : params.get('tc-type', ''),
        'tc_value'      : params.get('tc-value-input', ''),
        'threads'       : params.get('threads', ''),
        'hide_greens'   : 'hide-greens'  in params,
        'hide_yellows'  : 'hide-yellows' in params,
        'hide_reds'     : 'hide-reds'    in params,
        'hide_blues'    : 'hide-blues'   in params,
        'hide_stopped'  : 'hide-stopped' in params,
        'show_deleted'  : 'show-deleted' in params,
    }

    error = 'No matching tests found' if not len(filtered) else None
    return render(request, 'search.html', { 'tests' : reversed(filtered), 'form' : form }, error=error)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                           GENERAL DATA TABLE VIEWS                          #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def users(request):

    data = { 'profiles' : Profile.objects.order_by('-games', '-tests') }
    return render(request, 'users.html', data)

def event(request, pk):

    try:
        with open(os.path.join(MEDIA_ROOT, LogEvent.objects.get(id=pk).log_file)) as fin:
            return render(request, 'event.html', { 'content' : fin.read() })
    except:
        return redirect(request, '/index/', error='No logs for event exist')

def events_actions(request, page=1):

    events = LogEvent.objects.all().filter(machine_id=0).order_by('-id')
    start, end, paging = OpenBench.utils.getPaging(events, int(page), 'events')

    data = { 'events' : events[start:end], 'paging' : paging };
    return render(request, 'events.html', data)

def events_errors(request, page=1):

    events = LogEvent.objects.all().exclude(machine_id=0).order_by('-id')
    start, end, paging = OpenBench.utils.getPaging(events, int(page), 'errors')

    data = { 'events' : events[start:end], 'paging' : paging };
    return render(request, 'errors.html', data)

def machines(request, pk=None):
    return redirect(request, '/workers/%s/' % pk if pk is not None else '/workers/')


# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                            TEST MANAGEMENT VIEWS                            #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def workload(request, workload_type, pk, action=None):

    if action != None:
        return modify_workload(request, pk, action)

    if not (workload := Test.objects.select_related('spsa_run').filter(id=int(pk)).first()):
        return redirect(request, '/index/', error='No such Workload exists')

    # Trying to view a Tune as a Test, for example
    if workload.workload_type_str() != workload_type:
        return django.http.HttpResponseRedirect('/%s/%d/' % (workload.workload_type_str(), int(pk)))

    return view_workload(request, workload, workload_type.upper())

def new_workload(request, workload_type):

    if workload_type.upper() not in [ 'TEST', 'TUNE', 'DATAGEN' ]:
        return redirect(request, '/index/', error='Unknown Workload type')

    return create_workload(request, workload_type.upper())

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                          NETWORK MANAGEMENT VIEWS                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def networks(request, engine=None, action=None, name=None, client=False):

    # Without an identifier and a valid action, all we can do is view the list
    if not name or action.upper() not in ['UPLOAD', 'DEFAULT', 'DELETE', 'DOWNLOAD', 'EDIT']:
        networks = Network.objects.all()
        if engine and engine in OPENBENCH_CONFIG['engines'].keys():
            networks = networks.filter(engine=engine)
        return render(request, 'networks.html', { 'networks' : list(networks.order_by('-id').values()) })

    # Require logins. Clients will be artifically logged in
    if not request.user.is_authenticated:
        return django.http.HttpResponseRedirect('/login/')

    # Require approver credentials, unless downloading as a client
    if not client and not Profile.objects.get(user=request.user).approver:
        return django.http.HttpResponseRedirect('/index/')

    # Split out Uploads, since there is no logic to disambiguate the name
    if action.upper() == 'UPLOAD':
        return OpenBench.utils.network_upload(request, engine, name)

    # Push off all the actual effort to OpenBench.utils for all actions
    actions = {
        'DEFAULT'  : OpenBench.utils.network_default,
        'DELETE'   : OpenBench.utils.network_delete,
        'DOWNLOAD' : OpenBench.utils.network_download,
        'EDIT'     : OpenBench.utils.network_edit,
    }

    # Update the Network, if we can find one for the given name/sha256
    if (network := OpenBench.utils.network_disambiguate(engine, name)):
        return actions[action.upper()](request, engine, network)

    # Otherwise we could not find the Network, and cannot do anything
    return redirect(request, '/networks/', error='No network found with matching SHA')

def network_form(request):

    # Require logins. Clients will be artifically logged in
    if not request.user.is_authenticated:
        return django.http.HttpResponseRedirect('/login/')

    # Require approver credentials, unless downloading as a client
    if not Profile.objects.get(user=request.user).approver:
        return django.http.HttpResponseRedirect('/index/')

    # Get requests should not be reaching this point
    if request.method == 'GET':
        return render(request, 'uploadnet.html', {})

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                             OPENBENCH SCRIPTING                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

@csrf_exempt
def scripts(request):

    login(request) # All requests are attached to a User

    if request.POST['action'] == 'UPLOAD_NETWORK':
        engine = request.POST['engine']
        name   = request.POST['name']
        return networks(request, engine, 'upload', name)

    if request.POST['action'] == 'CREATE_TEST':
        return new_workload(request, "TEST")

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                              CLIENT HOOK VIEWS                              #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def verify_worker(function):

    def wrapped_verify_worker(*args, **kwargs):

        # Get the machine, assuming it exists
        try: machine = Machine.objects.get(id=int(args[0].POST['machine_id']))
        except: return JsonResponse({ 'error' : 'Bad Client Version: Bad Machine Id' })

        # Ensure the Client is using the same version as the Server
        if machine.info['client_ver'] != OPENBENCH_CONFIG['client_version']:
            expected_ver = OPENBENCH_CONFIG['client_version']
            return JsonResponse({ 'error' : 'Bad Client Version: Expected %d' % (expected_ver)})

        # Prompt the worker to soft-restart if its config is out of date
        if machine.info.get('OPENBENCH_CONFIG_CHECKSUM') != eligibility_fingerprint():
            return JsonResponse({ 'error' : 'Server Configuration Changed: Reinitialise Worker' })

        # Use the secret token as our soft verification
        if machine.secret != args[0].POST['secret']:
            return JsonResponse({ 'error' : 'Bad Client Version: Invalid Secret Token' })

        # Otherwise, carry on, and pass along the machine
        return function(*args, machine)

    return wrapped_verify_worker

@csrf_exempt
def client_version_ref(request):
    # Enough information to download the right Client
    return JsonResponse({
        'client_version'  : OPENBENCH_CONFIG['client_version' ],
        'client_repo_url' : OPENBENCH_CONFIG['client_repo_url'],
        'client_repo_ref' : OPENBENCH_CONFIG['client_repo_ref'],
    })

@csrf_exempt
def client_match_runner_version_ref(request):

    # Enough information to build the right Fastchess version
    runner = OPENBENCH_CONFIG['variants']['standard']['runner']
    return JsonResponse({'fastchess_' + key: value for key, value in runner.items()})

@csrf_exempt
def client_get_build_info(request):

    ## Information pulled from the config about how to build each engine.
    ## Toss in a private flag as well to indicate the need for Github Tokens.

    data = {}
    for engine, config in OPENBENCH_CONFIG['engines'].items():
        data[engine] = config['build'].copy()
        data[engine]['private'] = config['private']
    return JsonResponse(data)

@csrf_exempt
@transaction.atomic
def client_worker_info(request):

    persistent_worker = None
    # Verify the User's credentials
    try:
        if request.POST.get('worker_id'):
            import hashlib
            worker = TrainingWorker.objects.select_related('owner').get(pk=request.POST['worker_id'], enabled=True, owner__is_active=True)
            if not secrets.compare_digest(worker.secret_hash, hashlib.sha256(request.POST.get('worker_token', '').encode()).hexdigest()) or worker.owner.username != request.POST.get('username') or not Profile.objects.filter(user=worker.owner, enabled=True).exists():
                raise UnableToAuthenticate()
            user = worker.owner
            persistent_worker = worker
        else:
            user = authenticate(request, True)
    except (TrainingWorker.DoesNotExist, ValueError, django.core.exceptions.ValidationError):
        return JsonResponse({'error': 'Bad worker credentials'})
    except UnableToAuthenticate:
        return JsonResponse({ 'error' : 'Bad Credentials' })

    # Request update before creating a machine
    info         = json.loads(request.POST['system_info'])
    expected_ver = OPENBENCH_CONFIG['client_version']

    if info.get('client_ver') != expected_ver:
        return JsonResponse({ 'error' : 'Bad Client Version: Expected %d' % (expected_ver)})

    # Create a new Machine for this session
    mode = request.POST.get('mode', 'automatic')
    if mode not in ('automatic', 'testing-only', 'training-only', 'paused'):
        return JsonResponse({'error': 'Invalid worker mode'})
    machine = Machine.objects.select_for_update().get(pk=persistent_worker.machine_id) if persistent_worker and persistent_worker.machine_id else None
    if machine is None and request.POST.get('previous_machine_id', '').isdigit():
        previous = Machine.objects.select_for_update().filter(pk=request.POST['previous_machine_id'], user=user).first()
        if previous and previous.info.get('disconnected'):
            return JsonResponse({'error': 'Bad Client Version: Worker disconnected by its owner.'})
        if previous and secrets.compare_digest(previous.secret, request.POST.get('previous_machine_secret', '')):
            machine = previous
    if machine is None:
        machine = Machine(user=user, info=info, mode=persistent_worker.mode if persistent_worker else mode)
    machine.workload = 0

    # Save the machine's latest information and Secret Token for this session
    if machine.info.get('custom_name'):
        info.update(custom_name=machine.info['custom_name'], machine_name=machine.info['custom_name'])
    if 'worker_settings' in machine.info:
        info['worker_settings'] = machine.info['worker_settings']
        info.update(info['worker_settings'])
    machine.info   = info
    machine.secret = secrets.token_hex(32)

    # Note the Config checksum at the time of init, in case it changes
    machine.info['OPENBENCH_CONFIG_CHECKSUM'] = eligibility_fingerprint()

    # Tag engines that the Machine can build and/or run with binaries
    machine.info['supported'] = []
    for engine, data in OPENBENCH_CONFIG['engines'].items():

        # Must have all CPU flags, for both Public and Private engines
        if any([flag not in machine.info['cpu_flags'] for flag in data['build']['cpuflags']]):
            continue

        # Private engines must have, or think they have, a Git Token
        if data['private'] and engine not in machine.info['tokens'].keys():
            continue

        # Public engines must have a compiler of a sufficient version
        if not data['private'] and engine not in machine.info['compilers'].keys():
            continue

        # Must match the Operating Systems supported by the engine
        if machine.info['os_name'] not in data['build']['systems']:
            continue

        # All requirements are met, and this Machine can play with the given engine
        machine.info['supported'].append(engine)

    # Finish up
    machine.save()

    # Pass back the Machine Id, and Secret Token for this session
    return JsonResponse({ 'machine_id' : machine.id, 'secret' : machine.secret })

@csrf_exempt
def client_get_network(request, engine, name):

    # Verify the User's credentials
    try: django.contrib.auth.login(request, authenticate(request, True))
    except UnableToAuthenticate: return HttpResponse('Bad Credentials')

    # Return the requested Neural Network file for the Client
    return networks(request, engine, 'DOWNLOAD', name, client=True)

@csrf_exempt
@verify_worker
@transaction.atomic
def client_get_workload(request, machine):
    from OpenBench.training_models import TRAINING_ACTIVE
    machine = Machine.objects.select_for_update().get(pk=machine.pk)
    machine.updated = timezone.now()
    machine.save(update_fields=['updated'])
    settings = {'settings': machine.info.get('worker_settings', {})}
    if TrainingRun.objects.filter(worker__machine=machine, state__in=TRAINING_ACTIVE).exists():
        return JsonResponse(settings)
    if machine.mode not in ('automatic', 'testing-only'):
        Machine.objects.filter(pk=machine.pk).update(workload=0, mnps=0, dev_mnps=0, base_mnps=0)
        return JsonResponse(settings)
    result = get_workload(request, machine)
    if not result:
        Machine.objects.filter(pk=machine.pk).update(workload=0, mnps=0, dev_mnps=0, base_mnps=0)
    return JsonResponse({**result, **settings})

@csrf_exempt
@verify_worker
def client_bench_error(request, machine):

    # Find and stop the test with the bad bench
    test = Test.objects.get(id=int(request.POST['test_id']))
    test.finished = True; test.save()

    # Log the error into the Events table
    LogEvent.objects.create(
        author     = machine.user.username,
        summary    = request.POST['error'],
        log_file   = '',
        machine_id = int(request.POST['machine_id']),
        test_id    = int(request.POST['test_id']))

    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_nps(request, machine):

    # Update the NPS counters for the GUI views
    machine.mnps      = float(request.POST['nps'     ]) / 1e6;
    machine.dev_mnps  = float(request.POST['dev_nps' ]) / 1e6;
    machine.base_mnps = float(request.POST['base_nps']) / 1e6;
    machine.save(update_fields=['mnps', 'dev_mnps', 'base_mnps', 'updated'])

    # Pass back an empty JSON response
    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_error(request, machine):

    # Report an error when working on test. This could be one three kinds.
    # 1. Error building the engine. Does not compile, for whatever reason.
    # 2. Error during actual gameplay. Timeloss, Disconnect, Crash, etc.

    # Log the Error into the Events table
    event = LogEvent.objects.create(
        author     = machine.user.username,
        summary    = request.POST['error'],
        log_file   = '',
        machine_id = int(request.POST['machine_id']),
        test_id    = int(request.POST['test_id']))

    # Save the Logs to /Media/ to be viewed later
    logfile = ContentFile(request.POST['logs'])
    FileSystemStorage().save('event%d.log' % (event.id), logfile)
    event.log_file = 'event%d.log' % (event.id); event.save()

    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_results(request, machine):

    # Returns {}, or { 'stop' : True }
    result = OpenBench.utils.update_test(request, machine)
    if Machine.objects.filter(pk=machine.pk).exclude(workload=int(request.POST['test_id'])).exists():
        result['stop'] = True
    return JsonResponse(result)

@csrf_exempt
@verify_worker
def client_heartbeat(request, machine):

    # Force a refresh of the updated timestamp
    machine.save(update_fields=['updated'])

    # Include a 'stop' header iff the test was finished
    finished = Test.objects.filter(id=int(request.POST['test_id'])).values_list('finished', flat=True).first()

    stopped = Machine.objects.filter(pk=machine.pk).exclude(workload=int(request.POST['test_id'])).exists()
    return JsonResponse({'stop': True} if finished or stopped else {})

@csrf_exempt
@verify_worker
def client_submit_nps_stats(request, _):

    result_id = int(request.POST['result_id'])

    # No risk from concurrent access
    Result.objects.filter(id=result_id).update(
        dev_nodes        = F('dev_nodes'       ) + int(request.POST['dev_nodes'       ]),
        dev_time         = F('dev_time'        ) + int(request.POST['dev_time'        ]),
        dev_time_scaled  = F('dev_time_scaled' ) + int(request.POST['dev_time_scaled' ]),
        base_nodes       = F('base_nodes'      ) + int(request.POST['base_nodes'      ]),
        base_time        = F('base_time'       ) + int(request.POST['base_time'       ]),
        base_time_scaled = F('base_time_scaled') + int(request.POST['base_time_scaled']),
        updated          = timezone.now(),
    )

    return JsonResponse({})

"""
@csrf_exempt
@verify_worker
def client_submit_pgn(request, machine):

    with transaction.atomic():

        # Format: test.result.book-index.pgn.bz2
        pgn            = PGN()
        pgn.test_id    = int(request.POST['test_id']   )
        pgn.result_id  = int(request.POST['result_id'] )
        pgn.book_index = int(request.POST['book_index'])
        pgn.save()

        # Save the .pgn.bz2 to /Media/
        FileSystemStorage().save(pgn.filename(), ContentFile(request.FILES['file'].read()))

    return JsonResponse({})
"""

##### ---- DANGER ZONE ----
##### This version of client_submit_pgn doesn't hold a database lock
##### during disk I/O and can incur data loss, but it's much more forgiving
##### in terms of not locking the DB for ages during times of heavy disk load. Beware!

@csrf_exempt
@verify_worker
def client_submit_pgn(request, machine):

    # Format: test.result.book-index.pgn.bz2
    pgn            = PGN()
    pgn.test_id    = int(request.POST['test_id']   )
    pgn.result_id  = int(request.POST['result_id'] )
    pgn.book_index = int(request.POST['book_index'])

    # Save the .pgn.bz2 to /Media/
    FileSystemStorage().save(pgn.filename(), ContentFile(request.FILES['file'].read()))

    # First time actually touching the database
    try:
        pgn.save()
    except Exception as error:
        print ('FAILED to create Database entry for %s' % (pgn.filename()))
        print ('ERROR: ', error)

    return JsonResponse({})

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def api_response(data):
    return HttpResponse(json.dumps(data, indent=4), content_type='application/json')

@csrf_exempt
def api_authenticate(request, require_enabled=False):

    try:

        # Force requiring an enabled user when require_login_to_view is set
        require_enabled = require_enabled or OPENBENCH_CONFIG['require_login_to_view']

        # Don't require a login for Public frameworks
        if not require_enabled:
            return True

        # Request is made from a browser, and is already logged in
        if request.user.is_authenticated:
            return Profile.objects.get(user=request.user).enabled

        # Request might be made from the command line. Check the headers
        user = django.contrib.auth.authenticate(
            username=request.POST['username'], password=request.POST['password'])
        return Profile.objects.get(user=user).enabled

    except Exception:
        import traceback
        traceback.print_exc()
        return False

@csrf_exempt
def api_configs(request, engine=None):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    if engine == None:
        engines = list(OPENBENCH_CONFIG['engines'].keys())
        books   = OPENBENCH_CONFIG['books']
        return api_response({ 'engines' : engines, 'books' : books })

    if engine in OPENBENCH_CONFIG['engines'].keys():
        return api_response(OPENBENCH_CONFIG['engines'][engine])

    return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_networks(request, engine):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    if engine in OPENBENCH_CONFIG['engines'].keys():

        default = None
        if (network := Network.objects.filter(engine=engine, default=True).first()):
            default = OpenBench.model_utils.network_to_dict(network)

        networks = [
            OpenBench.model_utils.network_to_dict(network)
            for network in Network.objects.filter(engine=engine)
        ]

        return api_response({ 'default' : default, 'networks' : networks })

    else:
        return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_network_download(request, engine, identifier):

    machine_authenticated = False
    if request.POST.get('machine_id', '').isdigit():
        machine = Machine.objects.select_related('user').filter(pk=request.POST['machine_id'], user__is_active=True).first()
        machine_authenticated = bool(machine and secrets.compare_digest(machine.secret, request.POST.get('secret', '')) and Profile.objects.filter(user=machine.user, enabled=True).exists())
    if not machine_authenticated and not api_authenticate(request, require_enabled=True):
        return api_response({ 'error' : 'API requires authentication for this endpoint' })

    if (network := Network.objects.filter(engine=engine, sha256=identifier).first()):
        return OpenBench.utils.network_download(request, engine, network)

    if (network := Network.objects.filter(engine=engine, name=identifier).first()):
        return OpenBench.utils.network_download(request, engine, network)

    return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' })

@csrf_exempt
def api_network_delete(request, engine, identifier):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    if not api_authenticate(request, require_enabled=True):
        return api_response({ 'error' : 'API requires authentication for this endpoint' })

    if not (network := OpenBench.utils.network_disambiguate(engine, identifier)):
        return api_response({ 'error' : 'Network %s for Engine %s not found' % (identifier, engine) })

    message, success = OpenBench.model_utils.network_delete(network)
    return api_response({ 'success' if success else 'error' : message })

@csrf_exempt
def api_build_info(request):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    data = {}
    for engine, config in OPENBENCH_CONFIG['engines'].items():
        data[engine] = config

    for network in Network.objects.filter(default=True):

        if network.engine not in data:
            continue

        data[network.engine]['network'] = {
            'sha'     : network.sha256,
            'name'    : network.name,
            'author'  : network.author,
            'created' : str(network.created)
        }

    return api_response(data)

@csrf_exempt
def api_pgns(request, pgn_id):

    # 0. Make sure the request has the correct permissions
    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    # 1. Make sure the workload actually exists for the requested PGN
    try: workload = Test.objects.get(pk=pgn_id)
    except: return api_response({ 'error' : 'Requested Workload Id does not exist' })

    # 2. Make sure there actually is a PGN attached to the Workload
    pgn_path = FileSystemStorage().path('PGNs/%d.pgn.tar' % (pgn_id))
    if not os.path.exists(pgn_path):
        return api_response({ 'error' : 'Unable to find PGN for Workload #%d' % (pgn_id) })

    # 3. Make sure the workload is not currently running
    if not workload.finished:
        return api_response({ 'error' : 'PGNs cannot be downloaded while the Workload is active' })

    # 4. Make sure no active workers are still on this workload
    if OpenBench.utils.getRecentMachines().filter(workload=pgn_id):
        return api_response({ 'error' : 'Some machines are still on this Workload. Try again shortly' })

    # 5. Make sure there are no pending .pgn.bz2 files to be processed
    if PGN.objects.filter(test_id=pgn_id).filter(processed=False):
        return api_response({ 'error' : 'Still processing individual PGNs into the archive. Try again shortly' })

    # Craft the download HTML response
    return OpenBench.utils.media_download_response(pgn_path, '%d.pgn.tar' % (pgn_id), -1)

@csrf_exempt
def api_spsa(request, workload_id, query):

    # 0. Make sure the request has the correct permissions
    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    # 1. Make sure the workload actually exists for the requested SPSA session
    try: workload = Test.objects.get(pk=workload_id)
    except: return api_response({ 'error' : 'Requested Workload Id does not exist' })

    if query == 'inputs':
        return HttpResponse(OpenBench.spsa_utils.spsa_original_input(workload), content_type='text/plain')

    if query == 'outputs':
        return HttpResponse(OpenBench.spsa_utils.spsa_optimal_values(workload), content_type='text/plain')

    if query == 'digest':
        return HttpResponse(OpenBench.spsa_utils.spsa_param_digest(workload), content_type='text/plain')

    if query == 'perturbation':
        return api_response({ 'perturbation' : OpenBench.spsa_utils.spsa_workload_assignment_dict(workload, 4) })

    valid_endpoints = [ 'inputs', 'outputs', 'digest', 'perturbation' ]
    return api_response({ 'error' : 'Valid /query/ endpoints are: [ %s ]' % (', '.join(valid_endpoints)) })

@csrf_exempt
def api_workload(request, workload_id, query):

    # 0. Make sure the request has the correct permissions
    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' })

    # 1. Make sure the workload actually exists for the requested query
    try: workload = Test.objects.get(pk=workload_id)
    except: return api_response({ 'error' : 'Requested Workload Id does not exist' })

    if query == 'results':
        return JsonResponse({ 'results' : fetch_results(workload_id) })

    if query == 'info':
        return api_response({ 'info' : OpenBench.model_utils.workload_to_dict(workload) })

    if query == 'summary':
        return api_response({ 'summary' : fetch_result_summaries(workload) })

    if query == 'llr':
        if workload.test_mode != 'SPRT':
            return JsonResponse({'error': 'LLR history is only available for SPRT tests'}, status=400)
        from OpenBench.llr_history import workload_llr_history
        response = JsonResponse(workload_llr_history(workload))
        response['Cache-Control'] = 'no-store'
        return response

    valid_endpoints = [ 'results', 'info', 'summary', 'llr' ]
    return api_response({ 'error' : 'Valid /query/ endpoints are: [ %s ]' % (', '.join(valid_endpoints)) })

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                BUSINESS VIEWS                               #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def buyEthereal(request):
    return render(request, 'buyEthereal.html')
