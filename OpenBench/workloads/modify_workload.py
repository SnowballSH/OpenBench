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
#   >>> modify_workload(request, id, action)
#
# Given a request, a workload id, and an action, attempt to modify the workload as
# requested. This will return the user to the index, or a login page, with some
# indication as to success, or a reason for failure.

import OpenBench.views

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import *
from OpenBench.workloads.verify_workload import parse_integer

def modify_workload(request, id, action=None):

    actions = {
        'APPROVE' : approve_workload, 'RESTART' : restart_workload,
        'STOP'    : stop_workload,    'DELETE'  : delete_workload,
        'RESTORE' : restore_workload, 'MODIFY'  : tweak_workload,
    }

    # Make sure the requested Action is a known one
    if action not in actions.keys():
        return OpenBench.views.redirect(request, '/index/', error='Unknown Workload action')

    # Make sure that the workload exists
    if not (workload := Test.objects.filter(id=id).first()):
        return OpenBench.views.redirect(request, '/index/', error='No such Workload exists')

    # Must be logged in to interact with workloads
    if not request.user.is_authenticated:
        return OpenBench.views.redirect(request, '/login/', error='Only users may modify Workloads')

    # Must be an approver, or interacting with their own workload
    profile = Profile.objects.get(user=request.user)
    if not profile.approver and workload.author != request.user.username:
        return OpenBench.views.redirect(request, '/index/', error='You cannot interact with another user\'s Workload')

    # Must be an approver
    if action == 'APPROVE' and not profile.approver:
        return OpenBench.views.redirect(request, '/index/', error='You cannot approve this Workload')

    # Cross-approval requires a second approver, unless a superuser is acting
    if action == 'APPROVE' and self_approval_forbidden(request, workload):
        return OpenBench.views.redirect(request, '/index/', error='You cannot approve your own Workload')

    # Make the change; Record the change; Save the change
    message = actions[action](request, profile, workload)
    LogEvent.objects.create(author=request.user.username, summary=action, log_file='', test_id=id)
    workload.save()

    # Send back to the index, notifying them of the success
    return OpenBench.views.redirect(request, '/index/', status=message)

def self_approval_forbidden(request, workload) -> bool:
    return OPENBENCH_CONFIG['use_cross_approval'] \
       and workload.author == request.user.username \
       and not request.user.is_superuser

def approve_workload(request, profile, workload):
    workload.approved = True;
    return 'Workload was Approved!'

def restart_workload(request, profile, workload):
    workload.finished = False;
    return 'Workload was Restarted!'

def stop_workload(request, profile, workload):
    workload.finished = True;
    return 'Workload was Stopped!'

def delete_workload(request, profile, workload):
    workload.deleted = True
    return 'Workload was Deleted!'

def restore_workload(request, profile, workload):
    workload.deleted = False
    return 'Workload was Restored!'

def tweak_workload(request, profile, workload):

    # Priority can be any integer value
    if (priority := parse_integer(request.POST.get('priority'))) is not None:
        workload.priority = priority

    # Throughput must be at least 1
    if (throughput := parse_integer(request.POST.get('throughput'))) is not None:
        workload.throughput = max(1, throughput)

    # Must be at least one. Cannot be changed for Tuning workloads
    if (workload_size := parse_integer(request.POST.get('workload_size'))) is not None:
        if workload.test_mode != 'SPSA':
            workload.workload_size = max(1, workload_size)

    if 'info' in request.POST:
        workload.info = request.POST['info']

    return 'Workload was Modified!'
