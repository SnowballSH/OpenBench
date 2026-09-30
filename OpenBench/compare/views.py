from dataclasses import replace

from django.http import HttpRequest, HttpResponse

from OpenBench import upstream
from OpenBench.compare.analysis import CompareQuery, parse_query
from OpenBench.compare.present import ComparePage, chart_payload, compare_page, side
from OpenBench.insights.workload import insights_without_contributions
from OpenBench.models import Test
from OpenBench.progress.views import viewer_refused

TEMPLATE = 'compare.html'
MISSING_ERROR = 'Workload {} does not exist'


def load_tests(a: int, b: int) -> dict[int, Test]:
    return Test.objects.select_related('dev', 'base', 'spsa_run').in_bulk([a, b])


def build_page(a: Test, b: Test) -> ComparePage:
    return compare_page(
        side('a', a, insights_without_contributions(a)),
        side('b', b, insights_without_contributions(b)),
    )


def render_form(request: HttpRequest, query: CompareQuery, status: int = 400) -> HttpResponse:
    response = upstream.render(request, TEMPLATE, {'query': query})
    if query.errors and response.status_code == 200:
        response.status_code = status
    return response


def compare(request: HttpRequest) -> HttpResponse:
    if viewer_refused(request):
        return upstream.render(request, TEMPLATE)

    query = parse_query(request.GET)
    if (pair := query.pair) is None:
        return render_form(request, query)

    tests = load_tests(*pair)
    if missing := tuple(MISSING_ERROR.format(workload_id) for workload_id in pair if workload_id not in tests):
        return render_form(request, replace(query, errors=missing), status=404)

    page = build_page(tests[pair[0]], tests[pair[1]])
    return upstream.render(request, TEMPLATE, {'query': query, 'page': page, 'payload': chart_payload(page)})
