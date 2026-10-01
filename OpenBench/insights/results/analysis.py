from collections.abc import Sequence
from dataclasses import dataclass

from OpenBench.insights.contributions import ResultRow
from OpenBench.insights.domain import WorkloadFacts
from OpenBench.insights.results.breakdown import OutcomeBreakdown, outcome_breakdown
from OpenBench.insights.results.consistency import Consistency, check_consistency
from OpenBench.insights.results.hosts import combine, group_by_cpu, group_by_host
from OpenBench.insights.results.outlook import SprtOutlook, sprt_outlook
from OpenBench.insights.results.speeds import SpeedComparison, compare_speed
from OpenBench.insights.results.verdict import Verdict, give_verdict
from OpenBench.insights.strength import StrengthSummary


@dataclass(frozen=True, slots=True)
class ResultsAnalysis:
    verdict: Verdict
    outcomes: OutcomeBreakdown
    outlook: SprtOutlook | None
    speed: SpeedComparison | None
    consistency: Consistency


def outlook_of(facts: WorkloadFacts) -> SprtOutlook | None:
    if facts.finished or facts.sprt is None:
        return None
    return sprt_outlook(facts.outcomes, facts.llr, facts.sprt)


def analyse_results(facts: WorkloadFacts, strength: StrengthSummary, rows: Sequence[ResultRow]) -> ResultsAnalysis:
    use_penta = facts.outcomes.use_penta
    cpus = group_by_cpu(group_by_host(rows, use_penta), use_penta)
    outlook = outlook_of(facts)
    return ResultsAnalysis(
        verdict=give_verdict(facts, strength, outlook),
        outcomes=outcome_breakdown(facts.outcomes),
        outlook=outlook,
        speed=compare_speed(combine([cpu.tally for cpu in cpus], use_penta).speed, cpus),
        consistency=check_consistency(cpus),
    )
