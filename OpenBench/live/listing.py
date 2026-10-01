from datetime import datetime

from django.utils import timezone

from OpenBench.diagnosis.listing import row_reason
from OpenBench.insights.serialize import Json, to_json
from OpenBench.listing_rows import listing_moment
from OpenBench.live.display import live_progress, live_reason, live_result, live_timing
from OpenBench.live.domain import LiveListing, LiveRow, Unchanged
from OpenBench.live.token import listing_token
from OpenBench.models import Test
from OpenBench.page_queries import front_page, listing_row_timing, unfinished_tests


def live_row(test: Test) -> LiveRow:
    result = live_result(test, detailed=False)
    return LiveRow(
        id=test.id,
        result=result,
        progress=live_progress(test, result.status),
        timing=live_timing(listing_row_timing(test)),
        reason=live_reason(row_reason(test)),
        moment=listing_moment(test),
    )


def live_listing(author: str | None, known_token: str | None, now: datetime | None = None) -> LiveListing | Unchanged:

    now = now or timezone.now()
    token = listing_token(unfinished_tests(author), now)
    if token == known_token:
        return Unchanged(token)

    shown = front_page(author).shown(now)
    return LiveListing(
        token=token,
        machine_status=shown.status,
        rows=[live_row(test) for test in (*shown.pending, *shown.active)],
    )


def listing_payload(listing: LiveListing | Unchanged) -> dict[str, Json]:
    if isinstance(listing, Unchanged):
        return {'token': listing.token, 'changed': False}
    return {
        'token': listing.token,
        'changed': True,
        'machine_status': listing.machine_status,
        'rows': to_json(listing.rows),
    }
