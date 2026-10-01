from OpenBench.diagnosis.report import diagnose_workloads
from OpenBench.digest import sources
from OpenBench.digest.domain import DigestReport, DigestWindow
from OpenBench.digest.errors import error_digest
from OpenBench.digest.fleet import fleet_activity
from OpenBench.digest.headline import headline
from OpenBench.digest.trunk import trunk_movements
from OpenBench.digest.workloads import finished_digest, running_digest
from OpenBench.page_queries import is_listed
from OpenBench.progress.conditions import time_class
from OpenBench.progress.sources import load_runs, load_usage


def digest_report(window: DigestWindow) -> DigestReport:
    now = window.until
    finished = [test for test in sources.load_finished(window) if is_listed(test)]
    unfinished = [test for test in sources.load_unfinished(window) if is_listed(test)]
    diagnoses = diagnose_workloads([*finished, *unfinished], now)
    runs = load_runs(None, time_class, load_usage(None))

    done = finished_digest(sources.load_finished_counts(window), finished, diagnoses)
    running = running_digest(unfinished, window, diagnoses)
    trunk = trunk_movements(runs, window)
    fleet = fleet_activity(
        window,
        sources.load_hour_maxima(window),
        sources.load_baselines(window),
        runs,
        sources.load_active_hosts(window),
    )
    errors = error_digest(window, now)

    return DigestReport(
        generated_at=now,
        window=window,
        headline=headline(window, done, running, trunk, fleet, errors),
        finished=done,
        running=running,
        trunk=trunk,
        fleet=fleet,
        errors=errors,
    )
