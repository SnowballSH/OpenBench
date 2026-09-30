from django.http import Http404, HttpRequest, HttpResponse

from OpenBench import upstream
from OpenBench.compare.analysis import CompareQuery, parse_query
from OpenBench.compare.present import ComparePage, chart_payload, compare_page, side
from OpenBench.insights.workload import insights_without_contributions
from OpenBench.models import Test
from OpenBench.progress.views import viewer_refused

TEMPLATE = 'compare.html'
MISSING_ERROR = 'Workload {} does not exist'


def load_page(a: int, b: int) -> ComparePage:
    tests = Test.objects.select_related('dev', 'base', 'spsa_run').in_bulk([a, b])
    for workload_id in (a, b):
        if workload_id not in tests:
            raise Http404(MISSING_ERROR.format(workload_id))
    return compare_page(
        side('a', tests[a], insights_without_contributions(tests[a])),
        side('b', tests[b], insights_without_contributions(tests[b])),
    )


def render_form(request: HttpRequest, query: CompareQuery) -> HttpResponse:
    response = upstream.render(request, TEMPLATE, {'query': query})
    if query.errors and response.status_code == 200:
        response.status_code = 400
    return response


def compare(request: HttpRequest) -> HttpResponse:
    if viewer_refused(request):
        return upstream.render(request, TEMPLATE)

    query = parse_query(request.GET)
    if (pair := query.pair) is None:
        return render_form(request, query)

    page = load_page(*pair)
    return upstream.render(request, TEMPLATE, {'query': query, 'page': page, 'payload': chart_payload(page)})
