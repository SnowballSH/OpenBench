from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_safe

ROBOTS_URL = '/robots.txt'
DISALLOW_ALL = 'User-agent: *\nDisallow: /\n'


@require_safe
def robots_txt(request: HttpRequest) -> HttpResponse:
    return HttpResponse(DISALLOW_ALL, content_type='text/plain; charset=utf-8')
