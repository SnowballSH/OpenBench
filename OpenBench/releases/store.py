from datetime import datetime

from OpenBench.models import DefaultBranchCommit, EngineRelease
from OpenBench.releases.domain import ERROR_LIMIT, MAX_STANDING_CHECKS, BranchStanding, Release, ReleaseAnchor


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
    )


def load_anchor(engine: str) -> ReleaseAnchor | None:
    row = EngineRelease.objects.filter(engine=engine).first()
    return anchor_of(row) if row else None


def load_default_branch_commits(engine: str) -> dict[str, datetime | None]:
    rows = DefaultBranchCommit.objects.filter(engine=engine, on_default_branch=True)
    return dict(rows.values_list('sha', 'committed_at'))


def record_attempt(engine: str, now: datetime) -> None:
    EngineRelease.objects.update_or_create(engine=engine, defaults={'attempted_at': now})


def record_failure(engine: str, message: str, now: datetime) -> None:
    EngineRelease.objects.update_or_create(
        engine=engine, defaults={'attempted_at': now, 'error': message[:ERROR_LIMIT]}
    )


def record_release(engine: str, release: Release | None, default_branch: str, now: datetime) -> None:
    found = {'tag': release.tag, 'sha': release.sha, 'published_at': release.published_at} if release else {}
    absent = {} if release else {'tag': '', 'sha': '', 'published_at': None}
    EngineRelease.objects.update_or_create(
        engine=engine,
        defaults={
            **found,
            **absent,
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
            'default_branch': default_branch,
            'pinned': True,
            'fetched_at': now,
            'error': '',
        },
    )


def unpin_release(engine: str) -> bool:
    return bool(EngineRelease.objects.filter(engine=engine, pinned=True).update(pinned=False, attempted_at=None))


def record_standing(engine: str, standing: BranchStanding, now: datetime, final: bool = False) -> None:
    row, _ = DefaultBranchCommit.objects.get_or_create(engine=engine, sha=standing.sha, defaults={'checked_at': now})
    row.on_default_branch = standing.on_default_branch
    row.committed_at = standing.committed_at or row.committed_at
    row.checked_at = now
    row.checks = MAX_STANDING_CHECKS if final else row.checks + 1
    row.save()
