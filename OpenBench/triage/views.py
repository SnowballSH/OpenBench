from django.http import FileResponse, HttpRequest, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_safe

from OpenBench import upstream
from OpenBench.models import LogEvent
from OpenBench.progress.views import viewer_refused
from OpenBench.triage.actions import action_rows, operator_events
from OpenBench.triage.event_page import event_detail
from OpenBench.triage.flat import error_rows
from OpenBench.triage.groups import error_events, group_rows, with_affected
from OpenBench.triage.logs import log_name, log_path
from OpenBench.triage.query import ErrorQuery, parse_limit
from OpenBench.triage.serialize import group_json

ERRORS_TEMPLATE = 'errors.html'
EVENTS_TEMPLATE = 'events.html'
EVENT_TEMPLATE = 'event.html'
AUTHENTICATION_ERROR = 'API requires authentication for this server'
NO_EVENT = 'No such error event exists'
NO_LOG = 'No logs for event exist'


def grouped_errors(request: HttpRequest, query: ErrorQuery, page: int) -> HttpResponse:
    now = timezone.now()
    rows, truncated = group_rows(query, now)
    start, end, paging = upstream.paging(len(rows), page, 'errors')
    data = {
        'query': query,
        'groups': with_affected(rows[start:end], error_events(query)),
        'total': len(rows),
        'truncated': truncated,
        'paging': {**paging, 'query': query.querystring},
    }
    return upstream.render(request, ERRORS_TEMPLATE, data)


def listed_errors(request: HttpRequest, query: ErrorQuery, page: int) -> HttpResponse:
    events = error_events(query).order_by('-id')
    start, end, paging = upstream.paging(events.count(), page, 'errors')
    data = {
        'query': query,
        'events': error_rows(list(events[start:end]), timezone.now()),
        'paging': {**paging, 'query': query.querystring},
    }
    return upstream.render(request, ERRORS_TEMPLATE, data)


def errors(request: HttpRequest, page: str | None = None) -> HttpResponse:
    if viewer_refused(request):
        return upstream.render(request, ERRORS_TEMPLATE)

    query = ErrorQuery.parse(request.GET)
    show = listed_errors if query.is_list else grouped_errors
    return show(request, query, int(page or 1))


def events(request: HttpRequest, page: str | None = None) -> HttpResponse:
    if viewer_refused(request):
        return upstream.render(request, EVENTS_TEMPLATE)

    actions = operator_events()
    start, end, paging = upstream.paging(actions.count(), int(page or 1), 'events')
    rows = action_rows(list(actions[start:end]), timezone.now())
    return upstream.render(request, EVENTS_TEMPLATE, {'rows': rows, 'paging': paging})


def error_event(pk: int) -> LogEvent | None:
    return LogEvent.objects.filter(id=pk, machine_id__gt=0).first()


def event(request: HttpRequest, pk: int) -> HttpResponse:
    if viewer_refused(request):
        return upstream.render(request, EVENT_TEMPLATE)

    if (found := error_event(pk)) is None:
        return upstream.redirect(request, '/errors/', error=NO_EVENT)

    detail = event_detail(found, timezone.now())
    return upstream.render(request, EVENT_TEMPLATE, {'detail': detail})


def raw_log(found: LogEvent | None) -> FileResponse | None:
    if found is None or (path := log_path(found)) is None:
        return None
    return FileResponse(
        path.open('rb'), as_attachment=True, filename=log_name(found.id), content_type='text/plain; charset=utf-8'
    )


@require_safe
def event_raw(request: HttpRequest, pk: str) -> HttpResponse | FileResponse:
    if viewer_refused(request):
        return upstream.redirect(request, '/login/', error=upstream.error_message('requires_login'))

    return raw_log(error_event(int(pk))) or upstream.redirect(request, '/errors/', error=NO_LOG)


@csrf_exempt
def api_error_log(request: HttpRequest, event_id: int) -> HttpResponse | FileResponse:
    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': AUTHENTICATION_ERROR}, status=401)

    return raw_log(error_event(event_id)) or upstream.api_response({'error': NO_LOG}, status=404)


@csrf_exempt
def api_errors(request: HttpRequest) -> HttpResponse:
    if not upstream.api_authenticate(request):
        return upstream.api_response({'error': AUTHENTICATION_ERROR}, status=401)

    params = request.POST.copy()
    params.update(request.GET)
    query = ErrorQuery.parse(params)
    now = timezone.now()
    rows, truncated = group_rows(query, now)
    shown = with_affected(rows[: parse_limit(params.get('limit'))], error_events(query))
    return upstream.api_response(
        {
            'as_of': now.isoformat(),
            'total': len(rows),
            'truncated': truncated,
            'groups': [group_json(row) for row in shown],
        }
    )
