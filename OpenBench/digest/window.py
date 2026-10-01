import re
from datetime import UTC, datetime

from OpenBench.digest.domain import DEFAULT_CHOICE, MAX_WINDOW, DigestWindow, Preset, WindowChoice

TIMESTAMP = re.compile(
    r'\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)'
)

SINCE_ERROR = f'since must be one of {", ".join(preset.value for preset in Preset)}'
FROM_ERROR = 'from must be an ISO 8601 timestamp with an offset, such as 2026-10-01T06:00:00Z'
BOTH_ERROR = 'send since or from, not both'
FUTURE_ERROR = 'from must not be in the future'


class BadWindow(ValueError):
    pass


def parse_preset(raw: str) -> Preset:
    try:
        return Preset(raw.strip().lower())
    except ValueError:
        raise BadWindow(SINCE_ERROR) from None


def parse_timestamp(raw: str) -> datetime:
    if not TIMESTAMP.fullmatch(raw):
        raise BadWindow(FROM_ERROR)
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise BadWindow(FROM_ERROR) from None


def parse_choice(since: str | None, start: str | None) -> WindowChoice:
    if since and start:
        raise BadWindow(BOTH_ERROR)
    if start:
        return WindowChoice(None, parse_timestamp(start))
    return WindowChoice(parse_preset(since), None) if since else DEFAULT_CHOICE


def resolve(choice: WindowChoice, now: datetime) -> DigestWindow:
    if choice.preset is not None:
        return DigestWindow(choice.preset, now - choice.preset.span, now, clamped=False)
    if choice.since is None or choice.since > now:
        raise BadWindow(FUTURE_ERROR)
    earliest = now - MAX_WINDOW
    start = max(choice.since, earliest).astimezone(UTC).replace(second=0, microsecond=0)
    return DigestWindow(None, start, now, clamped=choice.since < earliest)
