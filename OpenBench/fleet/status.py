from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from OpenBench.insights.server import ACTIVE_MACHINE

UNKNOWN = 'Unknown'


class Presence(StrEnum):
    ONLINE = 'online'
    OFFLINE = 'offline'


class OfflineWindow(StrEnum):
    NONE = 'active'
    DAY = '24h'
    WEEK = '7d'

    @property
    def span(self) -> timedelta:
        return {
            OfflineWindow.NONE: ACTIVE_MACHINE,
            OfflineWindow.DAY: timedelta(days=1),
            OfflineWindow.WEEK: timedelta(days=7),
        }[self]

    @property
    def label(self) -> str:
        return {
            OfflineWindow.NONE: 'Online now',
            OfflineWindow.DAY: 'Seen in 24h',
            OfflineWindow.WEEK: 'Seen in 7d',
        }[self]

    @classmethod
    def parse(cls, value: str | None) -> Self:
        try:
            return cls(value or cls.NONE)
        except ValueError:
            return cls.NONE


def presence(updated: datetime, now: datetime) -> Presence:
    return Presence.ONLINE if now - updated <= ACTIVE_MACHINE else Presence.OFFLINE


def relative_age(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))

    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if seconds >= size:
            return f'{seconds // size}{unit} ago'

    return 'just now' if seconds < 10 else f'{seconds}s ago'
