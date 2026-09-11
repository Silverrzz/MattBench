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

import django.urls, OpenBench.views
import OpenBench.configuration_views
import OpenBench.presets
from OpenBench import training_api, training_views, dataset_library
from OpenBench.schedule_builder_views import schedule_builder
from OpenBench import training_checkpoints, training_datasets
from OpenBench import worker_views

urlpatterns = [

    django.urls.path('profile/huggingface/', training_views.connection),
    django.urls.path('training/', training_views.training_index),
    django.urls.path('training/page/<int:page>/', training_views.training_index),
    django.urls.path('training/new/', training_views.new_training),
    django.urls.path('training/datasets/', dataset_library.library),
    django.urls.path('training/datasets/new/', dataset_library.library, {'create': True}),
    django.urls.path('training/datasets/<uuid:dataset_id>/', dataset_library.library),
    django.urls.path('training/schedules/', training_views.schedules),
    django.urls.path('training/schedules/new/', training_views.schedules, {'create': True}),
    django.urls.path('training/schedules/builder/', schedule_builder),
    django.urls.path('training/schedules/<uuid:schedule_id>/builder/', schedule_builder),
    django.urls.path('training/schedules/<uuid:schedule_id>/', training_views.schedules),
    django.urls.path('training/workers/', training_views.workers),
    django.urls.path('workers/', worker_views.index),
    django.urls.path('workers/<int:pk>/', worker_views.detail),
    django.urls.path('workers/<uuid:pk>/', worker_views.detail),
    django.urls.path('training/<int:pk>/', training_views.training_detail),
    django.urls.path('training/<int:pk>/log/', training_views.training_log),
    django.urls.path('training/<int:pk>/configuration/', training_views.training_configuration),
    django.urls.path('training/<int:pk>/checkpoints/', training_views.checkpoint_list),
    django.urls.path('training/<int:pk>/artifacts/<int:artifact_id>/', training_views.artifact),
    django.urls.path('datagen/<int:pk>/huggingface/', training_views.dataset_upload),
    django.urls.path('api/training/register/', training_api.register),
    django.urls.path('api/training/claim/', training_api.claim),
    django.urls.path('api/training/<int:pk>/recover/', training_api.recover),
    django.urls.path('api/training/<int:pk>/report/', training_api.report),
    django.urls.path('api/training/<int:pk>/control/', training_api.control),
    django.urls.path('api/training/<int:pk>/dataset/<int:file_index>/', training_api.dataset_access),
    django.urls.path('api/training/<int:pk>/dataset/<int:file_index>/file/', training_api.dataset_file),
    django.urls.path('api/training/<int:pk>/xet-token/', training_api.xet_token),
    django.urls.path('api/training/<int:pk>/artifacts/', training_api.upload_artifact),
    django.urls.path('api/training/<int:pk>/checkpoints/', training_checkpoints.checkpoint_ready),
    django.urls.path('api/training/<int:pk>/resume/', training_checkpoints.resume_download),
    django.urls.path('api/training/<int:pk>/dataset/prepare/', training_datasets.prepare),
    django.urls.path('api/training/<int:pk>/dataset/prepare/token/', training_datasets.dataset_token),
    django.urls.path('api/training/<int:pk>/dataset/prepare/publish/', training_datasets.publish_dataset),
    django.urls.path('api/lifecycle/events/', training_views.lifecycle_events),

    django.urls.path('presets/<str:kind>/', OpenBench.presets.presets),

    django.urls.path('manage/', OpenBench.configuration_views.manage),
    django.urls.path('manage/<str:section>/', OpenBench.configuration_views.manage),
    django.urls.path('manage/<str:section>/new/', OpenBench.configuration_views.manage, {'identifier': 'new'}),
    django.urls.path('manage/<str:section>/<uuid:identifier>/', OpenBench.configuration_views.manage),

    # Links for account management
    django.urls.path(r'register/', OpenBench.views.register),
    django.urls.path(r'login/', OpenBench.views.login),
    django.urls.path(r'logout/', OpenBench.views.logout),
    django.urls.path(r'profile/', OpenBench.views.profile),
    django.urls.path(r'profileConfig/', OpenBench.views.profile_config),

    # Links for viewing test tables
    django.urls.re_path(r'^index(?:/(?P<page>\d+))?/$', OpenBench.views.index),
    django.urls.re_path(r'^user/(?P<username>[^/]+)(?:/(?P<page>\d+))?/$', OpenBench.views.user),
    django.urls.re_path(r'^greens(?:/(?P<page>\d+))?/$', OpenBench.views.greens),

    django.urls.path(r'search/', OpenBench.views.search),

    # Links for viewing general information tables
    django.urls.path(r'users/', OpenBench.views.users),
    django.urls.path(r'event/<int:pk>/', OpenBench.views.event),
    django.urls.re_path(r'^events(?:/(?P<page>\d+))?/$', OpenBench.views.events_actions),
    django.urls.re_path(r'^errors(?:/(?P<page>\d+))?/$', OpenBench.views.events_errors),
    django.urls.re_path(r'^machines(?:/(?P<pk>\d+))?/$', OpenBench.views.machines),

    # Links to create, view or manage Workloads (Tests, Tunes, Datagen)
    django.urls.re_path(r'^(?P<workload_type>tune|test|datagen)/new/$', OpenBench.views.new_workload),
    django.urls.re_path(r'^(?P<workload_type>tune|test|datagen)/(?P<pk>\d+)(?:/(?P<action>\w+))?/$', OpenBench.views.workload),

    # Links for viewing and managing Networks
    django.urls.path(r'networks/', OpenBench.views.networks),
    django.urls.path(r'networks/<str:engine>/', OpenBench.views.networks),
    django.urls.path(r'networks/<str:engine>/<str:action>/', OpenBench.views.networks),
    django.urls.path(r'networks/<str:engine>/<str:action>/<str:name>/', OpenBench.views.networks),
    django.urls.path(r'newNetwork/', OpenBench.views.network_form),

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
    django.urls.path(r'api/config/', OpenBench.views.api_configs),
    django.urls.path(r'api/config/<str:engine>/', OpenBench.views.api_configs),
    django.urls.path(r'api/networks/<str:engine>/', OpenBench.views.api_networks),
    django.urls.path(r'api/networks/<str:engine>/<str:identifier>/', OpenBench.views.api_network_download),
    django.urls.path(r'api/networks/<str:engine>/<str:identifier>/delete/', OpenBench.views.api_network_delete),
    django.urls.path(r'api/buildinfo/', OpenBench.views.api_build_info),
    django.urls.path(r'api/pgns/<int:pgn_id>/', OpenBench.views.api_pgns),
    django.urls.path(r'api/spsa/<int:workload_id>/<str:query>/', OpenBench.views.api_spsa),
    django.urls.path(r'api/workload/<int:workload_id>/<str:query>/', OpenBench.views.api_workload, name='api_workload'),

    # Redirect anything else to the Index
    django.urls.path(r'', OpenBench.views.index),

    # Link for Ethereal Sales
    django.urls.path(r'Ethereal/', OpenBench.views.buyEthereal),
]
