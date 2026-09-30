from django.http import HttpRequest

TRUSTED_FETCH_SITES = frozenset({'same-origin', 'none'})


def is_cross_site(request: HttpRequest) -> bool:

    # Current browsers send Sec-Fetch-Site on every request. Scripts and the
    # Client never send it, and hold no ambient browser session to abuse
    site = request.headers.get('Sec-Fetch-Site')
    return site is not None and site not in TRUSTED_FETCH_SITES
