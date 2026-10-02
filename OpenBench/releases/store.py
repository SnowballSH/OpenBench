from datetime import datetime
from typing import Any

from OpenBench.models import DefaultBranchCommit, EngineRelease
from OpenBench.releases.domain import ERROR_LIMIT, MAX_STANDING_CHECKS, BranchStanding, Release, ReleaseAnchor

NO_METADATA = {'bench': 0, 'network': ''}


def anchor_of(row: EngineRelease) -> ReleaseAnchor:
    return ReleaseAnchor(
        engine=row.engine,
        tag=row.tag,
        sha=row.sha,
        published_at=row.published_at,
        default_branch=row.default_branch,
        pinned=row.pinned,
        fetched_at=row.fetched_at,
        attempted_at=row.attempted_at,
        error=row.error,
        bench=row.bench or None,
        network=row.network,
    )


def load_anchor(engine: str) -> ReleaseAnchor | None:
    row = EngineRelease.objects.filter(engine=engine).first()
    return anchor_of(row) if row else None


def load_default_branch_commits(engine: str) -> dict[str, datetime | None]:
    rows = DefaultBranchCommit.objects.filter(engine=engine, on_default_branch=True)
    return dict(rows.values_list('sha', 'committed_at'))


def record_attempt(engine: str, attempted_at: datetime | None) -> None:
    EngineRelease.objects.update_or_create(engine=engine, defaults={'attempted_at': attempted_at})


def record_failure(engine: str, message: str, now: datetime) -> None:
    EngineRelease.objects.update_or_create(
        engine=engine, defaults={'attempted_at': now, 'error': message[:ERROR_LIMIT]}
    )


def metadata_of_another_release(engine: str, sha: str) -> dict[str, Any]:
    # A bench and a network describe one commit, so they never carry over to a different release
    same = EngineRelease.objects.filter(engine=engine, sha=sha).exists()
    return {} if same else NO_METADATA


def record_release(engine: str, release: Release | None, default_branch: str, now: datetime) -> None:
    found = release or Release('', '', None)
    EngineRelease.objects.update_or_create(
        engine=engine,
        defaults={
            'tag': found.tag,
            'sha': found.sha,
            'published_at': found.published_at,
            **metadata_of_another_release(engine, found.sha),
            'default_branch': default_branch,
            'pinned': False,
            'fetched_at': now,
            'attempted_at': now,
            'error': '',
        },
    )


def pin_release(engine: str, release: Release, default_branch: str, now: datetime) -> None:
    EngineRelease.objects.update_or_create(
        engine=engine,
        defaults={
            'tag': release.tag,
            'sha': release.sha,
            'published_at': release.published_at,
            **metadata_of_another_release(engine, release.sha),
            'default_branch': default_branch,
            'pinned': True,
            'fetched_at': now,
            'error': '',
        },
    )


def unpin_release(engine: str) -> bool:
    return bool(EngineRelease.objects.filter(engine=engine, pinned=True).update(pinned=False, attempted_at=None))


def set_metadata(engine: str, bench: int | None, network: str | None) -> bool:
    changes: dict[str, Any] = {}
    if bench is not None:
        changes['bench'] = bench
    if network is not None:
        changes['network'] = network
    return bool(EngineRelease.objects.filter(engine=engine).exclude(sha='').update(**changes))


def record_standing(engine: str, standing: BranchStanding, now: datetime, final: bool = False) -> None:
    row, _ = DefaultBranchCommit.objects.get_or_create(engine=engine, sha=standing.sha, defaults={'checked_at': now})
    row.on_default_branch = standing.on_default_branch
    row.committed_at = standing.committed_at or row.committed_at
    row.checked_at = now
    row.checks = MAX_STANDING_CHECKS if final else row.checks + 1
    row.failures = 0
    row.save()


def record_standing_failure(engine: str, sha: str, now: datetime) -> None:
    row, _ = DefaultBranchCommit.objects.get_or_create(engine=engine, sha=sha, defaults={'checked_at': now})
    row.checked_at = now
    row.failures += 1
    row.save()
