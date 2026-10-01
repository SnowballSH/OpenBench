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

# Module serves a singular purpose, to invoke:
# >>> view_workload(request, workload, type)
#
# A Workload can be a "TEST", which is an SPRT, or FIXED type
# A Workload can be a "TUNE", which is an SPSA tuning session
# A Workload can be a "DATAGEN", which is a Data Generation session

import datetime

from django.db.models import BooleanField, ExpressionWrapper, F, Q
from django.utils import timezone

import OpenBench.page_queries
import OpenBench.views
import OpenBench.stats
from OpenBench.diagnosis.report import diagnose_workload
from OpenBench.insights.grouping import sum_by_key
from OpenBench.machine_info import text_of
from OpenBench.insights.speed import nodes_per_second
from OpenBench.triage.sources import worker_error_count
from OpenBench.models import *
from OpenBench.workloads.confirmation import ExistingConfirmation, confirmation_for
from OpenBench.workloads.page import workload_page

def view_workload(request, workload, workload_type):

    assert workload_type in [ 'TEST', 'TUNE', 'DATAGEN' ]

    # The individual per-machine Result rows are never sent with the page; they
    # are fetched on demand via the "Fetch Individual Results" button. The
    # aggregate summary is fetched automatically once the page loads.

    data = {
        'workload'      : workload,
        'worker_errors' : worker_error_count(workload.id),
    }

    if workload_type == 'TEST':
        data['type']= workload_type
        data['dev_text'] = 'Dev'

    if workload_type == 'TUNE':
        data['type'] = workload_type
        data['dev_text'] = ''

    if workload_type == 'DATAGEN':
        data['type'] = workload_type
        data['dev_text'] = 'Dev'

    # What an unfinished Workload is waiting for, or the Worker error that stopped it; see docs/INSIGHTS.md
    if (diagnosis := diagnose_workload(workload)).shown:
        data['diagnosis'] = diagnosis

    # A passed STC test offers its LTC confirmation as a prefilled create form; see docs/WORKLOADS.md
    confirmation = confirmation_for(workload, OpenBench.page_queries.request_profile(request))
    data['confirmed_by' if isinstance(confirmation, ExistingConfirmation) else 'confirmation'] = confirmation

    # The header, summary strip and section list; see docs/UI.md, "Workload page"
    data['page'] = workload_page(workload, data['worker_errors'])

    return OpenBench.views.render(request, 'workload.html', data)

def fetch_results(workload):

    # One minute prior to now
    target = timezone.now() - datetime.timedelta(minutes=1)

    # Create `active` field for current machines
    qs = Result.objects.filter(test=workload).select_related('machine__user').annotate(
        active=ExpressionWrapper(
            Q(machine__updated__gte=target) &
            Q(test_id=F('machine__workload')),
            output_field=BooleanField()
        )
    )

    # Drop Results that have played nothing and are no longer active
    qs = qs.filter(Q(games__gt=0) | Q(active=True))

    # Hand back the raw pentanomial buckets; the individual results table is
    # formatted client-side in OpenBench/static/workload_utils.js
    qs = qs.values(
        'machine__id',
        'machine__user__username',
        'games',
        'LL', 'LD', 'DD', 'DW', 'WW',
        'timeloss',
        'crashes',
        'active',
    )

    return list(qs)

def fetch_result_summaries(workload):

    # Aggregate the pentanomial counters across every Result of the workload,
    # grouped three ways: by the User who ran it, and by the reporting Machine's
    # cpu_name and isa_name. We only ever sum penta; the trinomial counts and
    # crash/timeloss/active fields are intentionally left out.
    qs = Result.objects.filter(test=workload).select_related('machine__user')
    qs = qs.values(
        'machine__user__username',
        'machine__info',
        'LL', 'LD', 'DD', 'DW', 'WW',
        'dev_nodes',
        'dev_time',
        'dev_time_scaled',
        'base_nodes',
        'base_time',
        'base_time_scaled',
    )

    rows = list(qs)

    def penta(row):
        return (row['LL'], row['LD'], row['DD'], row['DW'], row['WW'])

    def nodes(row):
        return (
            row['dev_nodes'], row['dev_time'], row['dev_time_scaled'],
            row['base_nodes'], row['base_time'], row['base_time_scaled']
        )

    groupings = {
        'user'     : lambda row: row['machine__user__username'],
        'cpu_name' : lambda row: text_of(row['machine__info'], 'cpu_name'),
        'isa_name' : lambda row: text_of(row['machine__info'], 'isa_name'),
    }

    # Turn a { key: penta } bucket into ready-to-display rows: the penta as a
    # single "(a, b, c, d, e)" string, a point-estimate Elo with its symmetric
    # error bar, the pair count, and the share of the grouping's total. Largest
    # contributor comes first. Tunes play perturbed copies of one engine, so
    # their rows carry no Elo.

    with_elo = workload.test_mode != 'SPSA'

    def elo_display(penta):
        lower, mu, upper = OpenBench.stats.Elo(penta)
        return '%.2f ± %.2f' % (mu, (upper - lower) / 2)

    def summarize(bucket, nps_stats):
        total_pairs = sum(sum(penta) for penta in bucket.values())
        rows = [{
            'key'             : key,
            'penta'           : '(%d, %d, %d, %d, %d)' % tuple(penta),
            **({ 'elo' : elo_display(penta) } if with_elo else {}),
            'pairs'           : sum(penta),
            'percent'         : '%.2f' % (100.0 * sum(penta) / total_pairs if total_pairs else 0.0),
            'dev_nps'         : nodes_per_second(nps_stats[key][0], nps_stats[key][1]),
            'dev_nps_scaled'  : nodes_per_second(nps_stats[key][0], nps_stats[key][2]),
            'base_nps'        : nodes_per_second(nps_stats[key][3], nps_stats[key][4]),
            'base_nps_scaled' : nodes_per_second(nps_stats[key][3], nps_stats[key][5]),
        } for key, penta in bucket.items()]
        return sorted(rows, key=lambda row: row['pairs'], reverse=True)

    return {
        name : summarize(sum_by_key(rows, key_of, penta, 'Unknown'), sum_by_key(rows, key_of, nodes, 'Unknown'))
        for name, key_of in groupings.items()
    }
