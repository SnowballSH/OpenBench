from dataclasses import dataclass
from enum import StrEnum

from OpenBench.insights.domain import SprtBounds, WorkloadFacts, WorkloadStatus
from OpenBench.insights.results.outlook import SprtOutlook
from OpenBench.insights.sprt import MIN_GAMES
from OpenBench.insights.strength import EloInterval, StrengthSummary

VERY_LIKELY_PERCENT = 98
LIKELY_PERCENT = 90
POSSIBLE_PERCENT = 70
SMALL_ELO = 5.0
MODERATE_ELO = 20.0
CERTAINTY_MARGIN = 0.01
MINUS = '−'


class VerdictKind(StrEnum):
    TOO_EARLY = 'too_early'
    PASSED = 'passed'
    FAILED = 'failed'
    VERY_LIKELY_GAIN = 'very_likely_gain'
    LIKELY_GAIN = 'likely_gain'
    POSSIBLE_GAIN = 'possible_gain'
    INCONCLUSIVE = 'inconclusive'
    POSSIBLE_LOSS = 'possible_loss'
    LIKELY_LOSS = 'likely_loss'
    VERY_LIKELY_LOSS = 'very_likely_loss'


class VerdictTone(StrEnum):
    POSITIVE = 'positive'
    NEGATIVE = 'negative'
    NEUTRAL = 'neutral'


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: VerdictKind
    tone: VerdictTone
    text: str


GAIN_KINDS = (
    (VERY_LIKELY_PERCENT, VerdictKind.VERY_LIKELY_GAIN, VerdictKind.VERY_LIKELY_LOSS),
    (LIKELY_PERCENT, VerdictKind.LIKELY_GAIN, VerdictKind.LIKELY_LOSS),
    (POSSIBLE_PERCENT, VerdictKind.POSSIBLE_GAIN, VerdictKind.POSSIBLE_LOSS),
)

HEADLINES = {
    VerdictKind.VERY_LIKELY_GAIN: 'Very likely a {size}gain',
    VerdictKind.LIKELY_GAIN: 'Likely a {size}gain',
    VerdictKind.POSSIBLE_GAIN: 'Possibly a {size}gain',
    VerdictKind.INCONCLUSIVE: 'No clear difference',
    VerdictKind.POSSIBLE_LOSS: 'Possibly a {size}loss',
    VerdictKind.LIKELY_LOSS: 'Likely a {size}loss',
    VerdictKind.VERY_LIKELY_LOSS: 'Very likely a {size}loss',
}

TONES = {
    VerdictKind.PASSED: VerdictTone.POSITIVE,
    VerdictKind.VERY_LIKELY_GAIN: VerdictTone.POSITIVE,
    VerdictKind.LIKELY_GAIN: VerdictTone.POSITIVE,
    VerdictKind.FAILED: VerdictTone.NEGATIVE,
    VerdictKind.VERY_LIKELY_LOSS: VerdictTone.NEGATIVE,
    VerdictKind.LIKELY_LOSS: VerdictTone.NEGATIVE,
}


def leaning(los: float) -> VerdictKind:

    # Decided on the percentage as it is printed, so the headline never contradicts the number beside it
    percent = round(100 * los)
    for threshold, gain, loss in GAIN_KINDS:
        if percent >= threshold:
            return gain
        if percent <= 100 - threshold:
            return loss
    return VerdictKind.INCONCLUSIVE


def size_word(elo: EloInterval) -> str:

    # Sized by the end of the interval nearest zero, and left out when the interval still includes zero
    if elo.lower <= 0.0 <= elo.upper:
        return ''
    nearest = min(abs(elo.lower), abs(elo.upper))
    if nearest < SMALL_ELO:
        return 'small '
    return 'moderate ' if nearest < MODERATE_ELO else 'large '


def signed(value: float) -> str:
    text = f'{abs(value):.1f}'
    if float(text) == 0.0:
        return text
    return ('+' if value > 0 else MINUS) + text


def elo_text(elo: EloInterval) -> str:
    margin = max(elo.upper - elo.value, elo.value - elo.lower)
    return f'{signed(elo.value)} ± {margin:.1f} Elo'


def percent_text(probability: float) -> str:
    if probability > 1.0 - CERTAINTY_MARGIN:
        return 'above 99%'
    if probability < CERTAINTY_MARGIN:
        return 'below 1%'
    return f'{100 * probability:.0f}%'


def games_text(games: int) -> str:
    if games < 1_000:
        return str(games)
    if games < 10_000:
        return f'{games / 1_000:.1f}k'
    if games < 1_000_000:
        return f'{games / 1_000:.0f}k'
    return f'{games / 1_000_000:.1f}M'


def bound_scale(facts: WorkloadFacts) -> str:
    return 'normalized Elo' if facts.outcomes.use_penta else 'BayesElo'


def bounds_text(bounds: SprtBounds) -> str:
    return f'[{bounds.elo0:g}, {bounds.elo1:g}]'


def measurement(elo: EloInterval, los: float) -> str:
    return f'{elo_text(elo)}, LOS {percent_text(los)}'


def outlook_sentence(outlook: SprtOutlook | None) -> str:

    if outlook is None:
        return 'There are too few games to forecast how the SPRT will end.'

    games = outlook.remaining_games
    return (
        f'Forecast: {percent_text(outlook.pass_probability)} chance to pass, '
        f'with roughly {games_text(games.median)} more games needed '
        f'({100 * outlook.interval:.0f}% range {games_text(games.lower)} to {games_text(games.upper)}).'
    )


def progress_sentence(facts: WorkloadFacts) -> str:
    played = games_text(facts.outcomes.games)
    if facts.finished or facts.target_games is None:
        return f'Based on {played} games.'
    return f'{played} of {games_text(facts.target_games)} games played.'


def decided_text(facts: WorkloadFacts, bounds: SprtBounds, elo: EloInterval, los: float) -> str:
    passed = facts.status == WorkloadStatus.PASSED
    scale = bound_scale(facts)
    favoured, conclusion = (
        ('upper bound over its lower one', f'worth less than {bounds.elo0:g} {scale}')
        if passed
        else ('lower bound over its upper one', f'worth {bounds.elo1:g} {scale} or more')
    )
    return (
        f'{"Passed" if passed else "Failed"}: after {games_text(facts.outcomes.games)} games the SPRT with {scale} '
        f'bounds {bounds_text(bounds)} favoured its {favoured}, so dev is very unlikely to be {conclusion}. '
        f'Measured {measurement(elo, los)}; an SPRT stops the moment it is convinced, '
        'so this estimate is biased towards the bound it stopped at.'
    )


def leaning_text(kind: VerdictKind, facts: WorkloadFacts, elo: EloInterval, los: float, tail: str) -> str:
    headline = HEADLINES[kind].format(size=size_word(elo))
    suffix = ' yet' if kind == VerdictKind.INCONCLUSIVE and not facts.finished else ''
    return f'{headline}{suffix}: {measurement(elo, los)}. {tail}'


def verdict_of(kind: VerdictKind, text: str) -> Verdict:
    return Verdict(kind, TONES.get(kind, VerdictTone.NEUTRAL), text)


def give_verdict(facts: WorkloadFacts, strength: StrengthSummary, outlook: SprtOutlook | None) -> Verdict:

    elo, los = strength.elo, strength.los
    if elo is None or los is None or facts.outcomes.games < MIN_GAMES:
        return verdict_of(VerdictKind.TOO_EARLY, f'No verdict yet: fewer than {MIN_GAMES} games have been played.')
    if strength.normalized_elo is None:
        return verdict_of(VerdictKind.TOO_EARLY, 'No verdict yet: every result so far is the same.')

    if facts.sprt and facts.status in (WorkloadStatus.PASSED, WorkloadStatus.FAILED):
        kind = VerdictKind.PASSED if facts.status == WorkloadStatus.PASSED else VerdictKind.FAILED
        return verdict_of(kind, decided_text(facts, facts.sprt, elo, los))

    kind = leaning(los)
    tail = outlook_sentence(outlook) if facts.sprt and not facts.finished else progress_sentence(facts)
    return verdict_of(kind, leaning_text(kind, facts, elo, los, tail))
