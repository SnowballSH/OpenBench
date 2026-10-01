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

import django.urls, OpenBench.converters, OpenBench.views, OpenBench.insights.views, OpenBench.progress.views, OpenBench.storage.views
import OpenBench.compare.views
import OpenBench.games.views
import OpenBench.security.robots

urlpatterns = [

    # Links for account management
    django.urls.path(r'register/', OpenBench.views.register),
    django.urls.path(r'login/', OpenBench.views.login),
    django.urls.path(r'logout/', OpenBench.views.logout),
    django.urls.path(r'profile/', OpenBench.views.profile),
    django.urls.path(r'profileConfig/', OpenBench.views.profile_config),

    # Links for viewing test tables. Page numbers and ids are bounded, so int()
    # and SQLite's 64-bit integers never see an oversized number; <id:...>
    # applies the same bound to the other routes
    django.urls.re_path(r'^index(?:/(?P<page>[0-9]{1,10}))?/$', OpenBench.views.index),
    django.urls.re_path(r'^user/(?P<username>[^/]+)(?:/(?P<page>[0-9]{1,10}))?/$', OpenBench.views.user),
    django.urls.re_path(r'^greens(?:/(?P<page>[0-9]{1,10}))?/$', OpenBench.views.greens),

    django.urls.re_path(r'^search(?:/(?P<page>[0-9]{1,10}))?/$', OpenBench.views.search),

    # Engine progress over time, for every engine or one
    django.urls.path(r'progress/', OpenBench.progress.views.progress),
    django.urls.path(r'progress/<str:engine>/', OpenBench.progress.views.progress),

    # Two Workloads side by side
    django.urls.path(r'compare/', OpenBench.compare.views.compare),

    # Links for viewing general information tables
    django.urls.path(r'users/', OpenBench.views.users),
    django.urls.path(r'event/<id:pk>/', OpenBench.views.event),
    django.urls.re_path(r'^events(?:/(?P<page>[0-9]{1,10}))?/$', OpenBench.views.events_actions),
    django.urls.re_path(r'^errors(?:/(?P<page>[0-9]{1,10}))?/$', OpenBench.views.events_errors),
    django.urls.re_path(r'^machines(?:/(?P<pk>[0-9]{1,18}))?/$', OpenBench.views.machines),

    # Links to create, view or manage Workloads (Tests, Tunes, Datagen)
    django.urls.re_path(r'^(?P<workload_type>tune|test|datagen)/new/$', OpenBench.views.new_workload),
    django.urls.re_path(r'^(?P<workload_type>tune|test|datagen)/(?P<pk>[0-9]{1,18})(?:/(?P<action>\w+))?/$', OpenBench.views.workload),

    # Links for viewing and managing Networks
    django.urls.path(r'networks/', OpenBench.views.networks),
    django.urls.path(r'networks/<str:engine>/', OpenBench.views.networks),
    django.urls.path(r'networks/<str:engine>/<str:action>/', OpenBench.views.networks),
    django.urls.path(r'networks/<str:engine>/<str:action>/<str:name>/', OpenBench.views.networks),
    django.urls.path(r'newNetwork/', OpenBench.views.network_form),

    # Links for viewing and managing the Server's configuration
    django.urls.path(r'manage/', OpenBench.views.manage),
    django.urls.path(r'manage/books/', OpenBench.views.manage_books),
    django.urls.path(r'manage/books/<str:name>/', OpenBench.views.manage_books),
    django.urls.path(r'manage/books/<str:name>/<str:action>/', OpenBench.views.manage_books),

    django.urls.path(r'manage/engines/', OpenBench.views.manage_engines),
    django.urls.path(r'manage/engines/<str:name>/', OpenBench.views.manage_engines),
    django.urls.path(r'manage/engines/<str:name>/<str:action>/', OpenBench.views.manage_engines),

    django.urls.path(r'manage/storage/', OpenBench.storage.views.manage_storage),

    # Links for interacting with OpenBench via scripting
    django.urls.path(r'scripts/', OpenBench.views.scripts),

    # Links for the Client to work with the Server
    django.urls.path(r'clientVersionRef/', OpenBench.views.client_version_ref),
    django.urls.path(r'clientMatchRunnerVersionRef/', OpenBench.views.client_match_runner_version_ref),
    django.urls.path(r'clientGetBuildInfo/', OpenBench.views.client_get_build_info),
    django.urls.path(r'clientWorkerInfo/', OpenBench.views.client_worker_info),
    django.urls.path(r'clientGetWorkload/', OpenBench.views.client_get_workload),
    django.urls.path(r'clientGetNetwork/<str:engine>/<str:name>/', OpenBench.views.client_get_network),
    django.urls.path(r'clientBenchError/', OpenBench.views.client_bench_error),
    django.urls.path(r'clientSubmitNPS/', OpenBench.views.client_submit_nps),
    django.urls.path(r'clientSubmitError/', OpenBench.views.client_submit_error),
    django.urls.path(r'clientSubmitResults/', OpenBench.views.client_submit_results),
    django.urls.path(r'clientSubmitNPSStats/', OpenBench.views.client_submit_nps_stats),
    django.urls.path(r'clientHeartbeat/', OpenBench.views.client_heartbeat),
    django.urls.path(r'clientSubmitPGN/', OpenBench.views.client_submit_pgn),

    # Nice endpoints, which can be hit from the website or with credentials cleanly
    django.urls.path(r'api/active/', OpenBench.views.api_active),
    django.urls.path(r'api/config/', OpenBench.views.api_configs),
    django.urls.path(r'api/config/<str:engine>/', OpenBench.views.api_configs),
    django.urls.path(r'api/networks/<str:engine>/', OpenBench.views.api_networks),
    django.urls.path(r'api/networks/<str:engine>/<str:identifier>/', OpenBench.views.api_network_download),
    django.urls.path(r'api/networks/<str:engine>/<str:identifier>/delete/', OpenBench.views.api_network_delete),
    django.urls.path(r'api/buildinfo/', OpenBench.views.api_build_info),
    django.urls.path(r'api/pgns/<id:pgn_id>/', OpenBench.views.api_pgns),
    django.urls.path(r'api/spsa/<id:workload_id>/<str:query>/', OpenBench.views.api_spsa),
    django.urls.re_path(r'^api/workload/(?P<workload_id>[0-9]{1,18})/history\.csv$', OpenBench.insights.views.api_workload_history_csv),
    django.urls.path(r'api/workload/<id:workload_id>/games/', OpenBench.games.views.api_workload_games),
    django.urls.path(r'api/workload/<id:workload_id>/<str:query>/', OpenBench.views.api_workload),
    django.urls.path(r'api/insights/server/', OpenBench.insights.views.api_server_insights),
    django.urls.path(r'api/storage/', OpenBench.storage.views.api_storage),
    django.urls.path(r'api/progress/', OpenBench.progress.views.api_progress),

    # Liveness and database readiness, for the reverse proxy and deployers
    django.urls.path(r'health/', OpenBench.views.health),

    # Every page needs a login, so crawlers are asked to stay out
    django.urls.path(r'robots.txt', OpenBench.security.robots.robots_txt),

    # Redirect anything else to the Index
    django.urls.path(r'', OpenBench.views.index),

    # Link for Ethereal Sales
    django.urls.path(r'Ethereal/', OpenBench.views.buyEthereal),
]
