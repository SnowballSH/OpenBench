from datetime import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from OpenBench.models import EngineConfig, Network
from OpenBench.releases import store
from OpenBench.releases.domain import NO_NETWORK, BranchStanding, Release, is_commit_sha
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


def commit_sha(raw: str) -> str:
    sha = raw.strip().lower()
    if not is_commit_sha(sha):
        raise CommandError(f'Not a full 40-character commit sha: {raw}')
    return sha


def parse_bench(raw: int | None) -> int | None:
    if raw is not None and raw <= 0:
        raise CommandError('--bench must be a positive node count')
    return raw


def parse_network(engine: str, raw: str | None) -> str | None:
    if raw is None or raw.lower() == NO_NETWORK:
        return raw and NO_NETWORK
    if not Network.objects.filter(engine=engine, sha256=raw).exists():
        raise CommandError(f'{engine} has no network {raw}; give its sha as listed on /networks/, or "{NO_NETWORK}"')
    return raw


class Command(BaseCommand):
    help = (
        'Set the release an engine is measured against without asking GitHub (this pins it), record the '
        "release's bench and network (this does not), or hand the release back to GitHub with --unpin"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('engine')
        parser.add_argument('tag', nargs='?')
        parser.add_argument('sha', nargs='?')
        parser.add_argument('--published', help='When the release was published, ISO 8601')
        parser.add_argument('--default-branch', help='The default branch; else the one known, the preset one or master')
        parser.add_argument('--bench', type=int, help="The release commit's bench, for the create form")
        parser.add_argument('--network', help=f'The network the release runs: its sha, or "{NO_NETWORK}"')
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

        bench = parse_bench(options['bench'])
        network = parse_network(config.name, options['network'])
        merged = [commit_sha(sha) for sha in options['on_default_branch']]
        described = bench is not None or network is not None

        if options['tag'] and options['sha']:
            self.pin(config, options, commit_sha(options['sha']))
        elif options['tag'] or not (described or merged):
            raise CommandError('Give the release as <engine> <tag> <sha>, or --bench, --network or --on-default-branch')

        if described and not store.set_metadata(config.name, bench, network):
            raise CommandError(f'No release of {config.name} is known yet, so there is nothing to describe')
        if described:
            self.stdout.write(f'{config.name}: release bench and network recorded')

        now = timezone.now()
        for sha in merged:
            store.record_standing(config.name, BranchStanding(sha, True, None), now)

    def pin(self, config: EngineConfig, options: dict[str, Any], sha: str) -> None:
        known = store.load_anchor(config.name)
        branch = (
            options['default_branch']
            or (known.default_branch if known else '')
            or default_base_branch(config)
            or FALLBACK_BRANCH
        )
        release = Release(options['tag'], sha, parse_time(options['published']))
        store.pin_release(config.name, release, branch, timezone.now())
        self.stdout.write(f'{config.name}: measured against {release.tag} ({release.sha}), default branch {branch}')
