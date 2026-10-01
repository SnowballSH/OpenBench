from collections import Counter
from dataclasses import dataclass, field, fields
from typing import Any

from OpenBench.games.domain import MAX_PLY, MAX_TRACKED_OPENINGS, Colour, Outcome, Side
from OpenBench.games.facts import GameFacts

STATE_VERSION = 1

PAIR_KINDS = ('WW', 'WD', 'WL', 'DD', 'DL', 'LL')
OUTCOME_LETTERS = 'WDL'
OPENING_FIELDS = (*PAIR_KINDS, Colour.WHITE.value, Colour.BLACK.value)

type PendingGame = tuple[Colour, Outcome]


def pair_kind(first: Outcome, second: Outcome) -> str:
    return ''.join(sorted((first.value, second.value), key=OUTCOME_LETTERS.index))


def swept_by(dev_as_white: Outcome, dev_as_black: Outcome) -> Colour | None:
    if dev_as_white is Outcome.WIN and dev_as_black is Outcome.LOSS:
        return Colour.WHITE
    if dev_as_white is Outcome.LOSS and dev_as_black is Outcome.WIN:
        return Colour.BLACK
    return None


@dataclass(slots=True)
class Aggregate:
    """Order-independent counters over every analysed game; sums of two aggregates describe both."""

    totals: Counter[str] = field(default_factory=Counter)
    colour: Counter[str] = field(default_factory=Counter)
    pairs: Counter[str] = field(default_factory=Counter)
    sweeps: Counter[str] = field(default_factory=Counter)
    terminations: Counter[str] = field(default_factory=Counter)
    decisive_plies: Counter[str] = field(default_factory=Counter)
    drawn_plies: Counter[str] = field(default_factory=Counter)
    advantage: Counter[str] = field(default_factory=Counter)
    usage: Counter[str] = field(default_factory=Counter)
    openings: dict[str, list[int]] = field(default_factory=dict)

    def add_malformed(self) -> None:
        self.totals['malformed'] += 1

    def add_damaged_member(self) -> None:
        self.totals['damaged_members'] += 1

    def add_unpaired(self, games: int) -> None:
        self.totals['unpaired'] += games

    def add_game(self, facts: GameFacts) -> None:

        if facts.outcome is None:
            self.totals['unfinished'] += 1
            return

        self.totals['games'] += 1
        self.colour[f'{facts.dev_colour}.{facts.outcome}'] += 1
        self.terminations[facts.termination] += 1
        self.totals['inferred_terminations'] += facts.termination_inferred

        lengths = self.drawn_plies if facts.outcome is Outcome.DRAW else self.decisive_plies
        lengths[str(min(facts.plies, MAX_PLY))] += 1

        self.add_evals(facts, facts.outcome)
        for (side, phase), spent in facts.usage.items():
            self.usage[f'{side}.{phase}.depth_moves'] += spent.depth_moves
            self.usage[f'{side}.{phase}.depth'] += spent.depth
            self.usage[f'{side}.{phase}.timed_moves'] += spent.timed_moves
            self.usage[f'{side}.{phase}.time_ms'] += spent.time_ms
            self.usage[f'{side}.{phase}.nodes'] += spent.nodes

    def add_evals(self, facts: GameFacts, outcome: Outcome) -> None:

        if facts.first_eval_white_cp is None:
            return

        self.totals['scored_games'] += 1
        self.totals['book_eval_cp'] += facts.first_eval_white_cp
        self.totals['book_eval_abs_cp'] += abs(facts.first_eval_white_cp)

        for side, result in ((Side.DEV, outcome), (Side.BASE, outcome.reversed)):
            for threshold in facts.advantages_reached(side):
                self.advantage[f'{side}.{threshold}.{result}'] += 1

    def add_pair(self, opening: str, dev_as_white: Outcome, dev_as_black: Outcome) -> None:

        kind = pair_kind(dev_as_white, dev_as_black)
        sweep = swept_by(dev_as_white, dev_as_black)

        self.pairs[kind] += 1
        if sweep:
            self.sweeps[sweep] += 1

        if opening not in self.openings and len(self.openings) >= MAX_TRACKED_OPENINGS:
            self.totals['untracked_opening_pairs'] += 1
            return

        row = self.openings.setdefault(opening, [0] * len(OPENING_FIELDS))
        row[OPENING_FIELDS.index(kind)] += 1
        if sweep:
            row[OPENING_FIELDS.index(sweep.value)] += 1

    def merge(self, other: Aggregate) -> None:

        for item in fields(self):
            if item.name != 'openings':
                getattr(self, item.name).update(getattr(other, item.name))

        for opening, row in other.openings.items():
            if opening not in self.openings and len(self.openings) >= MAX_TRACKED_OPENINGS:
                self.totals['untracked_opening_pairs'] += sum(row[: len(PAIR_KINDS)])
            else:
                mine = self.openings.setdefault(opening, [0] * len(OPENING_FIELDS))
                mine[:] = [count + added for count, added in zip(mine, row, strict=True)]

    def to_state(self) -> dict[str, Any]:
        return {item.name: dict(getattr(self, item.name)) for item in fields(self)}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Aggregate:
        counters = {item.name: Counter(state.get(item.name, {})) for item in fields(cls) if item.name != 'openings'}
        return cls(**counters, openings={key: list(row) for key, row in state.get('openings', {}).items()})


@dataclass(slots=True)
class PairTracker:
    """Pairs the two games of one opening inside one uploaded batch."""

    aggregate: Aggregate
    waiting: dict[tuple[str, str], list[PendingGame]] = field(default_factory=dict)

    def add(self, facts: GameFacts) -> None:

        if facts.outcome is None:
            return

        key = (facts.round, facts.opening)
        queue = self.waiting.setdefault(key, [])
        partner = next((game for game in queue if game[0] is not facts.dev_colour), None)

        if partner is None:
            queue.append((facts.dev_colour, facts.outcome))
            return

        queue.remove(partner)
        as_white, as_black = (
            (facts.outcome, partner[1]) if facts.dev_colour is Colour.WHITE else (partner[1], facts.outcome)
        )
        self.aggregate.add_pair(facts.opening, as_white, as_black)

    def close(self) -> None:
        self.aggregate.add_unpaired(sum(len(queue) for queue in self.waiting.values()))
        self.waiting.clear()
