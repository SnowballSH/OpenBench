import re
from dataclasses import dataclass
from enum import StrEnum

from OpenBench.games.pgn import MATE_CP

ADVANTAGE_THRESHOLDS_CP = (100, 300, 500)
BOOK_EVAL_CLAMP_CP = 1000
MAX_PLY = 600
HISTOGRAM_BIN_PLIES = 20
MAX_TRACKED_OPENINGS = 8192
OPENING_TABLE_ROWS = 10
MIN_PAIRS_FOR_OPENING_TABLES = 2


class Colour(StrEnum):
    WHITE = 'white'
    BLACK = 'black'

    @property
    def other(self) -> Colour:
        return Colour.BLACK if self is Colour.WHITE else Colour.WHITE


class Side(StrEnum):
    DEV = 'dev'
    BASE = 'base'

    @property
    def other(self) -> Side:
        return Side.BASE if self is Side.DEV else Side.DEV


class Outcome(StrEnum):
    WIN = 'W'
    DRAW = 'D'
    LOSS = 'L'

    @property
    def reversed(self) -> Outcome:
        return {Outcome.WIN: Outcome.LOSS, Outcome.DRAW: Outcome.DRAW, Outcome.LOSS: Outcome.WIN}[self]


class Termination(StrEnum):
    CHECKMATE = 'checkmate'
    WIN_ADJUDICATION = 'win_adjudication'
    DRAW_ADJUDICATION = 'draw_adjudication'
    REPETITION = 'repetition'
    FIFTY_MOVES = 'fifty_moves'
    STALEMATE = 'stalemate'
    INSUFFICIENT_MATERIAL = 'insufficient_material'
    DRAW_BY_RULE = 'draw_by_rule'
    TIME_LOSS = 'time_loss'
    ILLEGAL_MOVE = 'illegal_move'
    DISCONNECT = 'disconnect'
    UNEXPLAINED_WIN = 'unexplained_win'
    OTHER = 'other'


TERMINATION_LABELS = {
    Termination.CHECKMATE: 'Checkmate',
    Termination.WIN_ADJUDICATION: 'Win adjudication',
    Termination.DRAW_ADJUDICATION: 'Draw adjudication',
    Termination.REPETITION: 'Threefold repetition',
    Termination.FIFTY_MOVES: 'Fifty-move rule',
    Termination.STALEMATE: 'Stalemate',
    Termination.INSUFFICIENT_MATERIAL: 'Insufficient material',
    Termination.DRAW_BY_RULE: 'Draw by rule or tablebase',
    Termination.TIME_LOSS: 'Time loss',
    Termination.ILLEGAL_MOVE: 'Illegal move',
    Termination.DISCONNECT: 'Crash or disconnect',
    Termination.UNEXPLAINED_WIN: 'Time loss, illegal move, crash or tablebase',
    Termination.OTHER: 'Other',
}


@dataclass(frozen=True, slots=True)
class Phase:
    key: str
    label: str
    first_ply: int
    last_ply: int | None


PHASES = (
    Phase('early', 'Plies 1 to 40', 1, 40),
    Phase('middle', 'Plies 41 to 80', 41, 80),
    Phase('late', 'Ply 81 onwards', 81, None),
)


def phase_of(ply: int) -> Phase:
    return next(phase for phase in PHASES if phase.last_ply is None or ply <= phase.last_ply)


@dataclass(frozen=True, slots=True)
class Adjudication:
    win_score_cp: int | None = None
    win_moves: int = 0
    draw_score_cp: int | None = None
    draw_moves: int = 0
    draw_from_move: int = 0


ADJUDICATION_FIELD = re.compile(r'(\w+)=(-?\d+)')


def adjudication_fields(setting: str) -> dict[str, int]:
    return {name: int(value) for name, value in ADJUDICATION_FIELD.findall(setting)}


def adjudication_of(win_adj: str, draw_adj: str) -> Adjudication:
    win, draw = adjudication_fields(win_adj), adjudication_fields(draw_adj)
    return Adjudication(
        win_score_cp=min(win['score'], MATE_CP) if 'score' in win else None,
        win_moves=max(1, win.get('movecount', 1)),
        draw_score_cp=draw.get('score'),
        draw_moves=max(1, draw.get('movecount', 1)),
        draw_from_move=draw.get('movenumber', 0),
    )
