from datetime import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from OpenBench.models import EngineConfig
from OpenBench.releases import store
from OpenBench.releases.domain import BranchStanding, Release
from OpenBench.workloads.presets import default_base_branch

FALLBACK_BRANCH = 'master'


def parse_time(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as error:
        raise CommandError(f'--published is not an ISO 8601 time: {raw}') from error
    return moment if moment.tzinfo else timezone.make_aware(moment)


class Command(BaseCommand):
    help = 'Set the release an engine is measured against without asking GitHub, or hand it back to GitHub'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('engine')
        parser.add_argument('tag', nargs='?')
        parser.add_argument('sha', nargs='?')
        parser.add_argument('--published', help='When the release was published, ISO 8601')
        parser.add_argument('--default-branch', help='The default branch; else the one known, the preset one or master')
        parser.add_argument('--on-default-branch', nargs='*', default=[], metavar='SHA')
        parser.add_argument('--unpin', action='store_true', help='Let refresh_releases follow GitHub again')

    def handle(self, *args: Any, **options: Any) -> None:
        config = EngineConfig.objects.filter(name=options['engine']).first()
        if config is None:
            raise CommandError(f'No engine named {options["engine"]}')

        if options['unpin']:
            released = store.unpin_release(config.name)
            self.stdout.write(f'{config.name}: {"follows GitHub again" if released else "was not pinned"}')
            return

        if not options['tag'] or not options['sha']:
            raise CommandError('Give the release as <engine> <tag> <sha>')

        now = timezone.now()
        known = store.load_anchor(config.name)
        branch = (
            options['default_branch']
            or (known.default_branch if known else '')
            or default_base_branch(config)
            or FALLBACK_BRANCH
        )
        release = Release(options['tag'], options['sha'].lower(), parse_time(options['published']))
        store.pin_release(config.name, release, branch, now)
        for sha in options['on_default_branch']:
            store.record_standing(config.name, BranchStanding(sha.lower(), True, None), now)
        self.stdout.write(f'{config.name}: measured against {release.tag} ({release.sha}), default branch {branch}')
