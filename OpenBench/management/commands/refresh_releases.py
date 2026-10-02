from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from OpenBench.releases.github import GitHubReleases
from OpenBench.releases.service import refresh_all


class Command(BaseCommand):
    help = "Ask GitHub for each engine's latest release and which tested commits are on its default branch"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('engines', nargs='*', help='Engines to refresh; every enabled engine when omitted')
        parser.add_argument('--force', action='store_true', help='Ask even when the last attempt is recent')

    def handle(self, *args: Any, **options: Any) -> None:
        outcomes = refresh_all(GitHubReleases(), timezone.now(), options['force'], options['engines'] or None)
        for outcome in outcomes:
            state = outcome.error or ('refreshed' if outcome.attempted else 'not due')
            self.stdout.write(f'{outcome.engine}: {state}; {outcome.standings} commits checked')
