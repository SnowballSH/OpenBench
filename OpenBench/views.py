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

import csv, io, os, json, logging, secrets

import django.http
import django.shortcuts
import django.contrib.auth

import OpenBench.config
import OpenBench.model_utils
import OpenBench.page_queries
import OpenBench.spsa_utils
import OpenBench.utils

from OpenBench.workloads.create_workload import create_workload
from OpenBench.workloads.get_workload import filter_valid_workloads, get_workload
from OpenBench.workloads.modify_workload import modify_workload
from OpenBench.workloads.verify_workload import verify_workload
from OpenBench.workloads.view_workload import view_workload, fetch_results, fetch_result_summaries
from OpenBench.insights.api import workload_payload
from OpenBench.fleet.machine_detail import load_machine_detail
from OpenBench.fleet.machines import load_machines_page
from OpenBench.fleet.status import OfflineWindow
from OpenBench.fleet.users import load_user_rows

from OpenBench import machine_info
from OpenBench.config import OPENBENCH_CONFIG, OPENBENCH_STATIC_VERSION
from OpenBench.security import throttle
from OpenBench.security.csrf import fails_session_csrf
from OpenBench.security.fetch_metadata import is_cross_site
from OpenSite.settings import PROJECT_PATH

from OpenBench.models import *
from django.contrib.auth.models import User
from OpenSite.settings import MEDIA_ROOT

from django.db import DatabaseError, connection, transaction
from django.db.models import F, Q
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.core.files.storage import FileSystemStorage
from django.core.files.base import ContentFile
from django.utils import timezone

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                              GENERAL UTILITIES                              #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

logger = logging.getLogger(__name__)

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
    data.update({ 'config' : OPENBENCH_CONFIG })
    data.update({ 'static_version' : OPENBENCH_STATIC_VERSION })

    # Every page lists the Engines in the sidebar. Lazy, so pages that do not
    # reference it in their Template never execute the query.
    data.setdefault('engines', EngineConfig.objects.filter(enabled=True).order_by('name'))

    if OPENBENCH_CONFIG['require_login_to_view']:
        if not request.user.is_authenticated and not always_allow:
            return redirect(request, '/login/',  error=ERROR_MESSAGES['requires_login'])

    if request.user.is_authenticated:

        profile = OpenBench.page_queries.request_profile(request)
        data.update({'profile' : profile})

        if profile and not profile.enabled:
            request.session['error_message'] = ERROR_MESSAGES['disabled']

        elif not profile:
            request.session['error_message'] = ERROR_MESSAGES['fakeuser']

    if error:
        request.session['error_message'] = error

    if warning:
        request.session['warning_message'] = warning

    if status:
        request.session['status_message'] = status

    response = django.shortcuts.render(request, 'OpenBench/{0}'.format(template), data)

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

    # Credentials only ever come from the POST body, never from the session

    username = request.POST.get('username', '')
    password = request.POST.get('password', '')

    if not username or not password:
        raise UnableToAuthenticate()

    if throttle.is_throttled(request, username):
        logger.warning('Throttled login for username %r from %s on %r',
            username[:150], throttle.client_ip(request), request.path)
        raise throttle.LoginThrottled()

    user = django.contrib.auth.authenticate(request, username=username, password=password)

    if user and requireEnabled and not Profile.objects.filter(user=user, enabled=True).exists():
        user = None

    if user is None:
        logger.warning('Authentication failed for username %r from %s on %r',
            username[:150], throttle.client_ip(request), request.path)
        raise UnableToAuthenticate()

    return user

THROTTLED_MESSAGE = 'Too many failed logins. Try again later'

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                            ADMINISTRATIVE VIEWS                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def register(request):

    if OPENBENCH_CONFIG['require_manual_registration']:
        return redirect(request, '/login/', error=ERROR_MESSAGES['manual_registration'])

    if request.method == 'GET':
        return render(request, 'register.html', always_allow=True)

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
    django.contrib.auth.login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    Profile.objects.create(user=user)

    return redirect(request, '/index/')

def login(request):

    if request.method == 'GET':
        return render(request, 'login.html', always_allow=True)

    try:
        django.contrib.auth.login(request, authenticate(request))
        return redirect(request, '/index/')

    except throttle.LoginThrottled:
        return redirect(request, '/login/', error=THROTTLED_MESSAGE)

    except UnableToAuthenticate:
        return redirect(request, '/login/', error='Unable to authenticate user')

def logout(request):

    # A GET must never end a session, or any page could log a user out
    if request.method != 'POST':
        return redirect(request, '/index/')

    django.contrib.auth.logout(request)
    return redirect(request, '/index/', status='Logged out')

def profile(request):

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    if not OpenBench.page_queries.request_profile(request):
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
        django.contrib.auth.update_session_auth_hash(request, request.user)
        changes_message += '\nUpdated password'

    return redirect(request, '/profile/', status=changes_message.removeprefix('\n'))

def profile_config(request):

    if not request.user.is_authenticated:
        return redirect(request, '/login/')

    if not (profile := OpenBench.page_queries.request_profile(request)):
        return redirect(request, '/index/')

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

    front = OpenBench.page_queries.FrontPage(
        OpenBench.utils.get_pending_tests(), OpenBench.utils.get_active_tests(), OpenBench.utils.getMachineStatus)

    completed = OpenBench.utils.get_completed_tests()
    data      = OpenBench.page_queries.workload_list_data(completed, int(page), 'index', front)
    return render(request, 'index.html', { **data, 'server_insights' : True })

def user(request, username, page=1):

    front = OpenBench.page_queries.FrontPage(
        OpenBench.utils.get_pending_tests().filter(author=username),
        OpenBench.utils.get_active_tests().filter(author=username),
        lambda: OpenBench.utils.getMachineStatus(username))

    completed = OpenBench.utils.get_completed_tests().filter(author=username)
    data      = OpenBench.page_queries.workload_list_data(completed, int(page), 'user/%s' % (username), front)
    return render(request, 'index.html', data)

def greens(request, page=1):

    completed = OpenBench.utils.get_completed_tests().filter(passed=True)
    return render(request, 'index.html', OpenBench.page_queries.workload_list_data(completed, int(page), 'greens'))

SEARCH_TERMS_LIMIT = 20

def search(request, page=1):

    # Search uses GET so the parameters live in the URL and can be shared.
    # With no parameters at all, simply present the empty search form.

    # Disabled Books are still offered, so older Workloads remain searchable
    books = Book.objects.all().order_by('name')

    if not (params := request.GET):
        return render(request, 'search.html', { 'books' : books })

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

    # Each keyword or author is one more OR'd match, and SQLite caps expression depth
    if too_many := [name for name in ('keywords', 'authors') if len(params.get(name, '').split()) > SEARCH_TERMS_LIMIT]:
        error = 'Search at most %d %s' % (SEARCH_TERMS_LIMIT, ' and '.join(too_many))
        return render(request, 'search.html', { 'form' : form, 'books' : books }, error=error)

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

    tests = OpenBench.page_queries.listing_tests(tests.order_by('-id'))
    start, end, paging = OpenBench.utils.getPaging(tests, int(page), 'search')
    shown = list(tests[start:end])

    error = 'No matching tests found' if not shown else None
    data  = { 'tests' : shown, 'form' : form, 'books' : books, 'paging' : { **paging, 'query' : '?' + params.urlencode() } }
    return render(request, 'search.html', data, error=error)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                           GENERAL DATA TABLE VIEWS                          #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def users(request):
    return render(request, 'users.html', { 'rows' : load_user_rows(timezone.now()) })

def event(request, pk):

    try:
        with open(os.path.join(MEDIA_ROOT, LogEvent.objects.get(id=pk).log_file)) as fin:
            return render(request, 'event.html', { 'content' : fin.read() })
    except:
        return redirect(request, '/index/', error='No logs for event exist')

def events_actions(request, page=1):

    events = LogEvent.objects.all().filter(machine_id=0).order_by('-id')
    start, end, paging = OpenBench.utils.getPaging(events, int(page), 'events')

    data = { 'events' : OpenBench.page_queries.attach_event_workloads(events[start:end]), 'paging' : paging };
    return render(request, 'events.html', data)

def events_errors(request, page=1):

    events = LogEvent.objects.all().exclude(machine_id=0).order_by('-id')
    start, end, paging = OpenBench.utils.getPaging(events, int(page), 'errors')

    data = { 'events' : OpenBench.page_queries.attach_event_workloads(events[start:end]), 'paging' : paging };
    return render(request, 'errors.html', data)

def machines(request, pk=None):

    if pk is None:
        page = load_machines_page(timezone.now(), OfflineWindow.parse(request.GET.get('show')))
        return render(request, 'machines.html', { 'page' : page })

    if not (detail := load_machine_detail(int(pk), timezone.now())):
        return redirect(request, '/machines/', error='Machine does not exist')

    return render(request, 'machine.html', { 'detail' : detail, 'machine' : detail.machine })


# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                            TEST MANAGEMENT VIEWS                            #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def workload(request, workload_type, pk, action=None):

    if action != None:
        if request.method != 'POST':
            return redirect(request, '/%s/%d/' % (workload_type, int(pk)), error='Workload actions must be submitted from the Workload page')
        if is_cross_site(request):
            return redirect(request, '/index/', error='Workload actions must be made from OpenBench itself')
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

NETWORK_CHANGES = frozenset({ 'UPLOAD', 'DEFAULT', 'DELETE' })

def networks(request, engine=None, action=None, name=None, client=False):

    # Without an identifier and a valid action, all we can do is view the list
    if not name or action.upper() not in ['UPLOAD', 'DEFAULT', 'DELETE', 'DOWNLOAD', 'EDIT']:
        networks = Network.objects.all()
        if engine and EngineConfig.objects.filter(name=engine).exists():
            networks = networks.filter(engine=engine)
        return render(request, 'networks.html', { 'networks' : list(networks.order_by('-id').values()) })

    # Require logins. Clients will be artifically logged in
    if not request.user.is_authenticated:
        return django.http.HttpResponseRedirect('/login/')

    # Require approver credentials, unless downloading as a client
    if not client and not Profile.objects.get(user=request.user).approver:
        return django.http.HttpResponseRedirect('/index/')

    # Changes are CSRF-protected forms. A GET is an old link, so change nothing
    if action.upper() in NETWORK_CHANGES and request.method != 'POST':
        return redirect(request, '/networks/%s/' % (engine), error='Network changes must be submitted from the Networks page')

    # Defense in depth, for browsers that report where the request came from
    is_change = action.upper() in NETWORK_CHANGES or (action.upper() == 'EDIT' and request.method == 'POST')
    if is_change and is_cross_site(request):
        return redirect(request, '/networks/', error='Network changes must be made from OpenBench itself')

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
    return redirect(request, '/networks/', error='No network found with matching Sha')

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
#                          CONFIGURATION MANAGEMENT                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# Everything under /manage/ may be viewed by anyone. Only those with manage
# permissions may make changes, which is enforced here, as well as visually
# within the Templates, by way of the can_manage flag.

def has_manage_permissions(request):
    profile = OpenBench.page_queries.request_profile(request)
    return bool(profile and (profile.superuser or profile.user.is_superuser))

def manage(request):
    return redirect(request, '/manage/books/')

def manage_books(request, name=None, action=None):

    can_manage = has_manage_permissions(request)

    # Without a name, all we can do is view the list of Books. The list also
    # carries the creation form, which posts back to <new name>/create/
    if not name:
        data = { 'books' : Book.objects.order_by('name'), 'can_manage' : can_manage }
        return render(request, 'manage_books.html', data)

    # Changes are CSRF-protected forms. A GET is an old link, so change nothing
    if action and request.method != 'POST':
        return redirect(request, '/manage/books/', error='Book changes must be submitted from the Books page')

    # Creating is the only action for a Book that does not exist yet
    if action and action.upper() == 'CREATE':
        if not can_manage:
            return redirect(request, '/manage/books/', error='You may not create Books')
        return OpenBench.utils.book_create(request, name)

    if not (book := Book.objects.filter(name=name).first()):
        return redirect(request, '/manage/books/', error='No such Book exists')

    # Anyone may view a single Book, but only Managers may change one
    if not action:
        return render(request, 'manage_book.html', { 'book' : book, 'can_manage' : can_manage })

    if not can_manage:
        return redirect(request, '/manage/books/', error='You may not modify Books')

    # Push off all the actual effort to OpenBench.utils for all actions
    actions = {
        'EDIT'   : OpenBench.utils.book_edit,
        'DELETE' : OpenBench.utils.book_delete,
    }

    if action.upper() not in actions:
        return redirect(request, '/manage/books/', error='Unknown action for a Book')

    return actions[action.upper()](request, book)

def manage_engines(request, name=None, action=None):

    can_manage = has_manage_permissions(request)

    # Without a name, all we can do is view the list of Engines. The list also
    # carries the creation form, which posts back to <new name>/create/
    if not name:
        data = { 'configs' : EngineConfig.objects.order_by('name'), 'can_manage' : can_manage }
        return render(request, 'manage_engines.html', data)

    # Changes are CSRF-protected forms. A GET is an old link, so change nothing
    if action and request.method != 'POST':
        return redirect(request, '/manage/engines/', error='Engine changes must be submitted from the Engines page')

    # Creating is the only action for an Engine that does not exist yet
    if action and action.upper() == 'CREATE':
        if not can_manage:
            return redirect(request, '/manage/engines/', error='You may not modify Engines')
        return OpenBench.utils.engine_create(request, name)

    if not (config := EngineConfig.objects.filter(name=name).first()):
        return redirect(request, '/manage/engines/', error='No such Engine exists')

    # Anyone may view a single Engine, but only Managers may change one
    if not action:
        data = {
            'engine_config'     : config,
            'can_manage'        : can_manage,
            'presets'           : json.dumps(config.presets, indent=4),
        }
        return render(request, 'manage_engine.html', data)

    if not can_manage:
        return redirect(request, '/manage/engines/', error='You may not modify Engines')

    # Push off all the actual effort to OpenBench.utils for all actions
    actions = {
        'EDIT'   : OpenBench.utils.engine_edit,
        'DELETE' : OpenBench.utils.engine_delete,
    }

    if action.upper() not in actions:
        return redirect(request, '/manage/engines/', error='Unknown action for an Engine')

    return actions[action.upper()](request, config)

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                             OPENBENCH SCRIPTING                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

@csrf_exempt
def scripts(request):

    # Exempt from CSRF, so a foreign page must not log a browser into its own account
    if is_cross_site(request):
        return HttpResponse('Cross-site requests are refused', status=403, content_type='text/plain')

    # Exempt from CSRF, so the request must carry its own credentials
    try: user = authenticate(request, requireEnabled=True)
    except throttle.LoginThrottled:
        return redirect(request, '/login/', error=THROTTLED_MESSAGE)
    except UnableToAuthenticate:
        return redirect(request, '/login/', error='Unable to authenticate user')

    django.contrib.auth.login(request, user)

    if request.POST.get('action') == 'UPLOAD_NETWORK':

        if not Profile.objects.filter(user=user, approver=True).exists():
            return redirect(request, '/index/', error='Only Approvers may upload Networks')

        if (missing := [ field for field in ('engine', 'name') if not request.POST.get(field) ]):
            return redirect(request, '/networks/', error='UPLOAD_NETWORK requires %s' % (', '.join(missing)))

        return networks(request, request.POST['engine'], 'upload', request.POST['name'])

    if request.POST.get('action') == 'CREATE_TEST':
        return new_workload(request, "TEST")

    return redirect(request, '/index/', error='Unknown scripts action')

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
        if machine.info.get('OPENBENCH_CONFIG_CHECKSUM') != ServerState.checksum():
            return JsonResponse({ 'error' : 'Bad Client Version: Server Configuration Changed' })

        # Use the secret token as our soft verification
        if not secrets.compare_digest(machine.secret.encode(), args[0].POST.get('secret', '').encode()):
            return JsonResponse({ 'error' : 'Bad Client Version: Invalid Secret Token' })

        # Otherwise, carry on, and pass along the machine
        return function(*args, machine)

    return wrapped_verify_worker

NOT_ASSIGNED = { 'error' : 'Workload is not assigned to this Machine' }

def is_assigned(machine, test_id, result_id=None):

    # A Result row exists for each (Test, Machine) pair the Machine was given
    results = Result.objects.filter(machine=machine, test_id=test_id)
    return results.filter(id=result_id).exists() if result_id is not None else results.exists()

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
    return JsonResponse({
        'fastchess_min_version' : OPENBENCH_CONFIG['fastchess_min_version'],
        'fastchess_repo_url'    : OPENBENCH_CONFIG['fastchess_repo_url'],
        'fastchess_repo_ref'    : OPENBENCH_CONFIG['fastchess_repo_ref'],
    })

@csrf_exempt
def client_get_build_info(request):

    ## Information pulled from the config about how to build each engine.
    ## Toss in a private flag as well to indicate the need for Github Tokens.

    data = {}
    for config in EngineConfig.objects.all():
        data[config.name] = { **config.build(), 'private' : config.private }
    return JsonResponse(data)

@csrf_exempt
def client_worker_info(request):

    # Verify the User's credentials
    try: user = authenticate(request, True)
    except throttle.LoginThrottled:
        return JsonResponse({ 'error' : THROTTLED_MESSAGE })
    except UnableToAuthenticate:
        return JsonResponse({ 'error' : 'Bad Credentials' })

    # Request update before creating a machine
    info         = machine_info.decode_system_info(request.POST.get('system_info'))
    expected_ver = OPENBENCH_CONFIG['client_version']

    if info is None:
        return JsonResponse({ 'error' : 'Malformed system_info' })

    if info.get('client_ver') != expected_ver:
        return JsonResponse({ 'error' : 'Bad Client Version: Expected %d' % (expected_ver)})

    # The Client treats any error as a failed registration, and retries later
    if malformed := machine_info.malformed_fields(info):
        return JsonResponse({ 'error' : 'Malformed system_info: %s' % (', '.join(malformed)) })

    # Create a new Machine for this session
    machine = Machine(user=user, info=info)

    # Save the machine's latest information and Secret Token for this session
    machine.info   = info
    machine.secret = secrets.token_hex(32)

    # Note the Config checksum at the time of init, in case it changes
    machine.info['OPENBENCH_CONFIG_CHECKSUM'] = ServerState.checksum()

    # Tag engines that the Machine can build and/or run with binaries
    machine.info['supported'] = supported_engines(machine.info)

    # Finish up
    machine.save()

    # Pass back the Machine Id, and Secret Token for this session
    return JsonResponse({ 'machine_id' : machine.id, 'secret' : machine.secret })

def supported_engines(info):

    supported = []
    for config in EngineConfig.objects.all():

        build = config.build()

        # Must have all CPU flags, for both Public and Private engines
        if any([flag not in info['cpu_flags'] for flag in build['cpuflags']]):
            continue

        # Private engines must have, or think they have, a Git Token
        if config.private and config.name not in info['tokens'].keys():
            continue

        # Public engines must have a compiler of a sufficient version
        if not config.private and config.name not in info['compilers'].keys():
            continue

        # Must match the Operating Systems supported by the engine
        if info['os_name'] not in build['systems']:
            continue

        # All requirements are met, and this Machine can play with the given engine
        supported.append(config.name)

    return supported

@csrf_exempt
def client_get_network(request, engine, name):

    # Verify the User's credentials
    try: django.contrib.auth.login(request, authenticate(request, True))
    except throttle.LoginThrottled: return HttpResponse(THROTTLED_MESSAGE, status=429)
    except UnableToAuthenticate: return HttpResponse('Bad Credentials')

    # Return the requested Neural Network file for the Client
    return networks(request, engine, 'DOWNLOAD', name, client=True)

@csrf_exempt
@verify_worker
def client_get_workload(request, machine):
    return JsonResponse(get_workload(request, machine))

@csrf_exempt
@verify_worker
def client_bench_error(request, machine):

    if not is_assigned(machine, int(request.POST['test_id'])):
        return JsonResponse(NOT_ASSIGNED)

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

    if not is_assigned(machine, int(request.POST['test_id'])):
        return JsonResponse(NOT_ASSIGNED)

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
    return JsonResponse(OpenBench.utils.update_test(request, machine))

@csrf_exempt
@verify_worker
def client_heartbeat(request, machine):

    # Force a refresh of the updated timestamp
    machine.save(update_fields=['updated'])

    # Include a 'stop' header iff the test was finished
    finished = Test.objects.filter(id=int(request.POST['test_id'])).values_list('finished', flat=True).first()

    return JsonResponse([{}, { 'stop' : True }][bool(finished)])

@csrf_exempt
@verify_worker
def client_submit_nps_stats(request, machine):

    result_id = int(request.POST['result_id'])

    if not is_assigned(machine, int(request.POST['test_id']), result_id):
        return JsonResponse(NOT_ASSIGNED)

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

@csrf_exempt
@verify_worker
def client_submit_pgn(request, machine):

    if not is_assigned(machine, int(request.POST['test_id']), int(request.POST['result_id'])):
        return JsonResponse(NOT_ASSIGNED)

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

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def api_response(data, status=200):
    return HttpResponse(json.dumps(data, indent=4), content_type='application/json', status=status)

def api_user(request):

    # The enabled User behind an API request, from the browser session or else
    # from credentials in the POST body. None if there is no such User. Raises
    # LoginThrottled, which LoginThrottleMiddleware turns into a 429

    if request.user.is_authenticated:
        return request.user if Profile.objects.filter(user=request.user, enabled=True).exists() else None

    try: return authenticate(request, requireEnabled=True)
    except UnableToAuthenticate: return None

@csrf_exempt
def api_authenticate(request, require_enabled=False):

    # Force requiring an enabled user when require_login_to_view is set
    require_enabled = require_enabled or OPENBENCH_CONFIG['require_login_to_view']

    # Don't require a login for Public frameworks
    return not require_enabled or api_user(request) is not None

ACTIVE_INFO_TYPES = {
    'concurrency'    : int,
    'physical_cores' : int,
    'logical_cores'  : int,
    'ram_total_mb'   : int,
    'syzygy_max'     : int,
    'noisy'          : bool,
    'cpu_flags'      : list,
    'os_name'        : str,
    'compilers'      : dict,
    'tokens'         : dict,
}

ACTIVE_OPTIONAL_TYPES = { 'focus' : list, 'only' : list }

def parse_active_info(request):

    # Returns the system_info subset a Client would register with, or None

    try: info = json.loads(request.POST['system_info'])
    except (KeyError, ValueError): return None

    if type(info) != dict:
        return None

    for key, kind in ACTIVE_INFO_TYPES.items():
        if type(info.get(key)) != kind:
            return None

    for key, kind in ACTIVE_OPTIONAL_TYPES.items():
        if key in info and type(info[key]) != kind:
            return None

    if not all(x.isascii() and x.isdigit() and len(x) <= 18 for x in request.POST.getlist('blacklist')):
        return None

    return { key : info[key] for key in (*ACTIVE_INFO_TYPES, *ACTIVE_OPTIONAL_TYPES) if key in info }

@csrf_exempt
def api_active(request):

    # How many workloads a Client with this system_info and blacklist could be
    # assigned right now. Uses the same filters as clientGetWorkload, and never
    # writes: the Machine below is never saved

    if request.method != 'POST':
        return api_response({ 'error' : 'POST required' }, status=405)

    if not api_authenticate(request, require_enabled=True):
        return api_response({ 'error' : 'Bad Credentials' }, status=401)

    if not (info := parse_active_info(request)):
        return api_response({ 'error' : 'Malformed system_info or blacklist' }, status=400)

    machine = Machine(info={ **info, 'supported' : supported_engines(info) })
    candidates, _ = filter_valid_workloads(request, machine)
    return api_response({ 'assignable' : len(candidates) })

@csrf_exempt
def api_configs(request, engine=None):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' }, status=401)

    if engine == None:
        engines = list(EngineConfig.objects.filter(enabled=True).order_by('name').values_list('name', flat=True))
        books   = {
            book.name : { 'sha' : book.sha, 'source' : book.source }
            for book in Book.objects.filter(enabled=True).order_by('name')
        }
        return api_response({ 'engines' : engines, 'books' : books })

    if (config := EngineConfig.objects.filter(name=engine).first()):
        return api_response(OpenBench.model_utils.engine_config_to_dict(config))

    return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' }, status=404)

@csrf_exempt
def api_networks(request, engine):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' }, status=401)

    if EngineConfig.objects.filter(name=engine).exists():

        default = None
        if (network := Network.objects.filter(engine=engine, default=True).first()):
            default = OpenBench.model_utils.network_to_dict(network)

        networks = [
            OpenBench.model_utils.network_to_dict(network)
            for network in Network.objects.filter(engine=engine)
        ]

        return api_response({ 'default' : default, 'networks' : networks })

    else:
        return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' }, status=404)

@csrf_exempt
def api_network_download(request, engine, identifier):

    # Checked once, so a bad password counts as a single failure
    if not api_authenticate(request, require_enabled=True):
        return api_response({ 'error' : 'API requires authentication for this endpoint' }, status=401)

    if (network := OpenBench.utils.network_disambiguate(engine, identifier)):
        return OpenBench.utils.network_download(request, engine, network, identifier)

    if not EngineConfig.objects.filter(name=engine).exists():
        return api_response({ 'error' : 'Engine not found. Check /api/config/ for a full list' }, status=404)

    return api_response({ 'error' : 'Network %s for Engine %s not found' % (identifier, engine) }, status=404)

@csrf_exempt
def api_network_delete(request, engine, identifier):

    if request.method != 'POST':
        return api_response({ 'error' : 'POST required' }, status=405)

    # Exempt from CSRF, so refuse a foreign page riding on a browser session
    if is_cross_site(request):
        return api_response({ 'error' : 'Cross-site requests are refused' }, status=403)

    if fails_session_csrf(request):
        return api_response({ 'error' : 'Browser sessions must send a CSRF token' }, status=403)

    if not (user := api_user(request)):
        return api_response({ 'error' : 'API requires authentication for this endpoint' }, status=401)

    # Matches the website, where only Approvers may delete Networks
    if not Profile.objects.filter(user=user, approver=True).exists():
        return api_response({ 'error' : 'Only Approvers may delete Networks' }, status=403)

    if not (network := OpenBench.utils.network_disambiguate(engine, identifier)):
        return api_response({ 'error' : 'Network %s for Engine %s not found' % (identifier, engine) }, status=404)

    message, success = OpenBench.model_utils.network_delete(network)
    return api_response({ 'success' if success else 'error' : message }, status=200 if success else 409)

@csrf_exempt
def api_build_info(request):

    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' }, status=401)

    data = {}
    for config in EngineConfig.objects.filter(enabled=True).order_by('name'):
        data[config.name] = OpenBench.model_utils.engine_config_to_dict(config)

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
        return api_response({ 'error' : 'API requires authentication for this server' }, status=401)

    # 1. Make sure the workload actually exists for the requested PGN
    try: workload = Test.objects.get(pk=pgn_id)
    except: return api_response({ 'error' : 'Requested Workload Id does not exist' }, status=404)

    # 2. Make sure there actually is a PGN attached to the Workload
    pgn_path = FileSystemStorage().path('PGNs/%d.pgn.tar' % (pgn_id))
    if not os.path.exists(pgn_path):
        return api_response({ 'error' : 'Unable to find PGN for Workload #%d' % (pgn_id) }, status=404)

    # 3. Make sure the workload is not currently running
    if not workload.finished:
        return api_response({ 'error' : 'PGNs cannot be downloaded while the Workload is active' }, status=409)

    # 4. Make sure no active workers are still on this workload
    if OpenBench.utils.getRecentMachines().filter(workload=pgn_id):
        return api_response({ 'error' : 'Some machines are still on this Workload. Try again shortly' }, status=409)

    # 5. Make sure there are no pending .pgn.bz2 files to be processed
    if PGN.objects.filter(test_id=pgn_id).filter(processed=False):
        return api_response({ 'error' : 'Still processing individual PGNs into the archive. Try again shortly' }, status=409)

    # Craft the download HTML response
    return OpenBench.utils.media_download_response(pgn_path, '%d.pgn.tar' % (pgn_id))

@csrf_exempt
def api_spsa(request, workload_id, query):

    # 0. Make sure the request has the correct permissions
    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' }, status=401)

    # 1. Make sure the workload actually exists for the requested SPSA session
    try: workload = Test.objects.get(pk=workload_id)
    except: return api_response({ 'error' : 'Requested Workload Id does not exist' }, status=404)

    if workload.test_mode != 'SPSA':
        return api_response({ 'error' : 'Requested Workload is not an SPSA tune' }, status=404)

    if query == 'inputs':
        return HttpResponse(OpenBench.spsa_utils.spsa_original_input(workload), content_type='text/plain')

    if query == 'outputs':
        return HttpResponse(OpenBench.spsa_utils.spsa_optimal_values(workload), content_type='text/plain')

    if query == 'digest':
        return HttpResponse(OpenBench.spsa_utils.spsa_param_digest(workload), content_type='text/plain')

    if query == 'perturbation':
        return api_response({ 'perturbation' : OpenBench.spsa_utils.spsa_workload_assignment_dict(workload, 4) })

    valid_endpoints = [ 'inputs', 'outputs', 'digest', 'perturbation' ]
    return api_response({ 'error' : 'Valid /query/ endpoints are: [ %s ]' % (', '.join(valid_endpoints)) }, status=404)

@csrf_exempt
def api_workload(request, workload_id, query):

    # 0. Make sure the request has the correct permissions
    if not api_authenticate(request):
        return api_response({ 'error' : 'API requires authentication for this server' }, status=401)

    # 1. Make sure the workload actually exists for the requested query
    try: workload = Test.objects.get(pk=workload_id)
    except: return api_response({ 'error' : 'Requested Workload Id does not exist' }, status=404)

    if query == 'results':
        return api_response({ 'results' : fetch_results(workload_id) })

    if query == 'info':
        return api_response({ 'info' : OpenBench.model_utils.workload_to_dict(workload) })

    if query == 'summary':
        return api_response({ 'summary' : fetch_result_summaries(workload) })

    if query == 'insights':
        return api_response({ 'insights' : workload_payload(workload) })

    valid_endpoints = [ 'results', 'info', 'summary', 'insights' ]
    return api_response({ 'error' : 'Valid /query/ endpoints are: [ %s ]' % (', '.join(valid_endpoints)) }, status=404)

@csrf_exempt
@require_http_methods([ 'GET', 'HEAD' ])
def health(request):

    # Reveals nothing beyond whether the database answers, so it needs no login
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1 FROM OpenBench_serverstate LIMIT 1')
    except DatabaseError:
        return JsonResponse({ 'status' : 'unavailable' }, status=503)

    return JsonResponse({ 'status' : 'ok' })

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                BUSINESS VIEWS                               #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def buyEthereal(request):
    return render(request, 'buyEthereal.html')
