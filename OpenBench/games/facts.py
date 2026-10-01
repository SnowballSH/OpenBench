from dataclasses import dataclass, field

from OpenBench.games.domain import (
    ADVANTAGE_THRESHOLDS_CP,
    BOOK_EVAL_CLAMP_CP,
    Adjudication,
    Colour,
    Outcome,
    Side,
    Termination,
    phase_of,
)
from OpenBench.games.pgn import Game
from OpenBench.games.termination import inferred_termination, stated_termination

DEV_OUTCOMES = {
    ('1-0', Colour.WHITE): Outcome.WIN,
    ('1-0', Colour.BLACK): Outcome.LOSS,
    ('0-1', Colour.WHITE): Outcome.LOSS,
    ('0-1', Colour.BLACK): Outcome.WIN,
    ('1/2-1/2', Colour.WHITE): Outcome.DRAW,
    ('1/2-1/2', Colour.BLACK): Outcome.DRAW,
}

FEN_FIELDS_IN_KEY = 4
BOOK_LINE_KEY_CHARS = 120


@dataclass(slots=True)
class PhaseUsage:
    depth_moves: int = 0
    depth: int = 0
    timed_moves: int = 0
    time_ms: int = 0
    nodes: int = 0


@dataclass(frozen=True, slots=True)
class GameFacts:
    dev_colour: Colour
    outcome: Outcome | None
    plies: int
    opening: str
    round: str
    termination: Termination
    termination_inferred: bool
    first_eval_white_cp: int | None
    peak_cp: dict[Side, int | None]
    usage: dict[tuple[Side, str], PhaseUsage] = field(default_factory=dict)

    def advantages_reached(self, side: Side) -> list[int]:
        peak = self.peak_cp[side]
        return [threshold for threshold in ADVANTAGE_THRESHOLDS_CP if peak is not None and peak >= threshold]


def engine_role(name: str) -> str:
    return name.rsplit('-', 1)[-1]


def dev_colour_of(game: Game) -> Colour | None:
    roles = (engine_role(game.headers['White']), engine_role(game.headers['Black']))
    if roles == (Side.DEV, Side.BASE):
        return Colour.WHITE
    if roles == (Side.BASE, Side.DEV):
        return Colour.BLACK
    return None


def first_mover_of(game: Game) -> Colour:
    fields = game.headers.get('FEN', '').split()
    return Colour.BLACK if len(fields) > 1 and fields[1] == 'b' else Colour.WHITE


def first_move_number_of(game: Game) -> int:
    fields = game.headers.get('FEN', '').split()
    return int(fields[5]) if len(fields) > 5 and fields[5].isdigit() else 1


def opening_key(game: Game) -> str:
    if fen := game.headers.get('FEN'):
        return ' '.join(fen.split()[:FEN_FIELDS_IN_KEY])
    book_line = ' '.join(move.san for move in game.moves if move.book)
    return book_line[:BOOK_LINE_KEY_CHARS] or 'startpos'


def game_facts(game: Game, rules: Adjudication) -> GameFacts | None:

    if (dev_colour := dev_colour_of(game)) is None:
        return None

    first_mover = first_mover_of(game)
    side_of = {dev_colour: Side.DEV, dev_colour.other: Side.BASE}
    peak: dict[Side, int | None] = {Side.DEV: None, Side.BASE: None}
    usage: dict[tuple[Side, str], PhaseUsage] = {}
    first_eval: int | None = None
    mover = first_mover

    for ply, move in enumerate(game.moves, start=1):
        side = side_of[mover]

        if move.score_cp is not None:
            if first_eval is None:
                clamped = max(-BOOK_EVAL_CLAMP_CP, min(BOOK_EVAL_CLAMP_CP, move.score_cp))
                first_eval = clamped if mover is Colour.WHITE else -clamped
            previous = peak[side]
            peak[side] = move.score_cp if previous is None else max(previous, move.score_cp)

        if move.depth is not None:
            spent = usage.setdefault((side, phase_of(ply).key), PhaseUsage())
            spent.depth_moves += 1
            spent.depth += move.depth
            if move.time_ms is not None and move.nodes is not None:
                spent.timed_moves += 1
                spent.time_ms += move.time_ms
                spent.nodes += move.nodes

        mover = mover.other

    stated = stated_termination(game)
    return GameFacts(
        dev_colour=dev_colour,
        outcome=DEV_OUTCOMES.get((game.result, dev_colour)),
        plies=len(game.moves),
        opening=opening_key(game),
        round=game.headers.get('Round', ''),
        termination=stated or inferred_termination(game, rules, first_mover, first_move_number_of(game)),
        termination_inferred=stated is None,
        first_eval_white_cp=first_eval,
        peak_cp=peak,
        usage=usage,
    )
