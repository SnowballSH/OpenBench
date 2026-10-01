import contextlib
import importlib.util
import io
import math
import random
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

CLIENT_PGN_UTIL = Path(__file__).resolve().parents[2] / 'Client' / 'pgn_util.py'

PIECES = ('', '', 'N', 'B', 'R', 'Q', 'K')
FILES = 'abcdefgh'
RANKS = '234567'

QUIET_ENDING_PLIES = 20
MIN_PLIES = 24
MAX_PLIES = 420
ADJUDICATED_DRAW_PLIES = 96
WINNING_CP = 650
SWING_CP = 380


@dataclass(frozen=True, slots=True)
class Opening:
    fen: str
    draw_rate: float
    white_share: float

    @property
    def white_moves_first(self) -> bool:
        return self.fen.split()[1] == 'w'


OPENINGS = (
    Opening('r1bqkbnr/1ppp1ppp/p1n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4', 0.58, 0.60),
    Opening('rnbqkb1r/1p2pppp/p2p1n2/8/3NP3/2N5/PPP2PPP/R1BQKB1R w KQkq - 0 6', 0.34, 0.62),
    Opening('rnbqk2r/ppp1ppbp/3p1np1/8/2PPP3/2N5/PP3PPP/R1BQKBNR w KQkq - 0 5', 0.22, 0.80),
    Opening('rnbqkb1r/ppp2ppp/4pn2/3p4/2PP4/2N5/PP2PPPP/R1BQKBNR w KQkq - 2 4', 0.90, 0.55),
    Opening('rnbqkbnr/ppp2ppp/4p3/3p4/3PP3/2N5/PPP2PPP/R1BQKBNR b KQkq - 1 3', 0.48, 0.64),
    Opening('rnbqkbnr/pp2pppp/2p5/3pP3/3P4/8/PPP2PPP/RNBQKBNR b KQkq - 0 3', 0.52, 0.60),
    Opening('rnbqkbnr/pppp1ppp/8/4p3/2P5/2N5/PP1PPPPP/R1BQKBNR b KQkq - 1 2', 0.62, 0.52),
    Opening('r1bqk1nr/pppp1ppp/2n5/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4', 0.97, 0.50),
    Opening('rnbqkb1r/p1pp1ppp/1p2pn2/8/2PP4/5N2/PP2PPPP/RNBQKB1R w KQkq - 0 4', 0.66, 0.58),
    Opening('rnb1kbnr/ppp1pppp/8/3q4/8/2N5/PPPP1PPP/R1BQKBNR b KQkq - 1 3', 0.26, 0.84),
    Opening('rnbqkbnr/ppppp1pp/8/5p2/3P4/6P1/PPP1PP1P/RNBQKBNR b KQkq - 0 2', 0.24, 0.86),
    Opening('rnbqkbnr/pppp1ppp/8/4p3/4PP2/8/PPPP2PP/RNBQKBNR b KQkq - 0 2', 0.20, 0.22),
    Opening('rnbqkbnr/ppp1pppp/8/3p4/8/5NP1/PPPPPP1P/RNBQKB1R b KQkq - 0 2', 0.72, 0.52),
    Opening('rnbqkb1r/pppppppp/8/3nP3/8/8/PPPP1PPP/RNBQKBNR w KQkq - 1 3', 0.30, 0.78),
    Opening('rnbqkbnr/pp2pppp/2p5/3p4/2PP4/8/PP2PPPP/RNBQKBNR w KQkq - 0 3', 0.99, 0.50),
    Opening('rnbqkbnr/pp1ppppp/8/2p5/4P3/2P5/PP1P1PPP/RNBQKBNR b KQkq - 0 2', 0.60, 0.50),
)


@dataclass(frozen=True, slots=True)
class Ending:
    termination: str
    reason: str
    kind: str


DECISIVE_ENDINGS = (
    (0.20, Ending('normal', '{winner} mates', 'mate')),
    (0.72, Ending('adjudication', '{winner} wins by adjudication', 'resign')),
    (0.05, Ending('time forfeit', '{loser} loses on time', 'forfeit')),
    (0.02, Ending('abandoned', '{loser} disconnects', 'forfeit')),
    (0.01, Ending('illegal move', '{loser} makes an illegal move', 'forfeit')),
)
DRAWN_ENDINGS = (
    (0.55, Ending('adjudication', 'Draw by adjudication', 'flat')),
    (0.30, Ending('normal', 'Draw by 3-fold repetition', 'rule')),
    (0.10, Ending('normal', 'Draw by fifty moves rule', 'rule')),
    (0.03, Ending('normal', 'Draw by insufficient mating material', 'rule')),
    (0.02, Ending('normal', 'Draw by stalemate', 'rule')),
)


@dataclass(frozen=True, slots=True)
class MatchSetup:
    engine: str = 'Avalanche'
    time_control: str = '8+0.08'
    dev_edge: float = 0.02
    dev_nps: int = 1_180_000
    base_nps: int = 1_150_000


def client_pgn_util() -> ModuleType:
    spec = importlib.util.spec_from_file_location('openbench_client_pgn_util', CLIENT_PGN_UTIL)
    if spec is None or spec.loader is None:
        raise ImportError(f'Cannot load the Client PGN formatter from {CLIENT_PGN_UTIL}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upload_batch(raw_files: Sequence[str], scale_factor: float, compact: bool) -> bytes:
    """Turns match-runner PGN files into the bzip2 batch the Client uploads, with the Client's own code."""

    with tempfile.TemporaryDirectory() as folder:
        paths = [Path(folder) / f'{index}.pgn' for index in range(len(raw_files))]
        for path, text in zip(paths, raw_files, strict=True):
            path.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):
            compressed: bytes = client_pgn_util().compress_pgn_files(
                [str(path) for path in paths], scale_factor, compact
            )
    return compressed


def pick[T](weighted: Sequence[tuple[float, T]], rng: random.Random) -> T:
    return rng.choices([item for _, item in weighted], weights=[weight for weight, _ in weighted])[0]


def placeholder_move(rng: random.Random, quiet: bool) -> str:
    return rng.choice(PIECES[2:] if quiet else PIECES) + rng.choice(FILES) + rng.choice(RANKS)


def white_result(opening: Opening, white_edge: float, rng: random.Random) -> str:
    if rng.random() < opening.draw_rate:
        return '1/2-1/2'
    return '1-0' if rng.random() < min(0.98, max(0.02, opening.white_share + white_edge)) else '0-1'


def ply_count(ending: Ending, rng: random.Random) -> int:
    if ending.kind == 'flat':
        return min(MAX_PLIES, ADJUDICATED_DRAW_PLIES + round(rng.expovariate(1 / 50)))
    centre = 150 if ending.kind == 'rule' else 110
    return max(MIN_PLIES, min(MAX_PLIES, round(rng.gauss(centre, 36))))


def white_evals(result: str, ending: Ending, start_cp: float, plies: int, rng: random.Random) -> list[float]:

    target = {'1-0': WINNING_CP, '0-1': -WINNING_CP}.get(result, 0.0)
    if ending.kind == 'forfeit':
        target *= 0.15
    if ending.kind == 'rule':
        target = rng.choice((-45.0, 45.0))

    swing_at = rng.randrange(plies) if rng.random() < 0.12 else None
    swing = rng.choice((-SWING_CP, SWING_CP))
    evals = []
    for ply in range(plies):
        progress = (ply / max(1, plies - 1)) ** 2
        value = start_cp + (target - start_cp) * progress + rng.gauss(0, 12)
        if swing_at is not None and abs(ply - swing_at) < 6 and plies - ply > 12:
            value += swing
        evals.append(value)

    if ending.kind == 'flat':
        evals[-24:] = [rng.uniform(-6, 6) for _ in evals[-24:]]
    if ending.kind in ('resign', 'mate'):
        evals[-8:] = [math.copysign(WINNING_CP + 40 * step, target) for step in range(len(evals[-8:]))]
    return evals


def score_text(own_cp: float, mate_in: int | None) -> str:
    if mate_in is not None:
        return f'{"+" if own_cp > 0 else "-"}M{mate_in}'
    return f'{own_cp / 100:+.2f}'


def move_comment(own_cp: float, mate_in: int | None, ply: int, nps: int, rng: random.Random) -> str:
    depth = round(16 + 10 * min(1.0, ply / 120) + rng.gauss(0, 1.5))
    seconds = max(0.012, 0.36 * math.exp(-ply / 70) + rng.uniform(0, 0.08))
    nodes = round(nps * seconds * (1 + 0.25 * min(1.0, ply / 120)) * rng.uniform(0.9, 1.1))
    return f'{score_text(own_cp, mate_in)}/{depth} {seconds:.3f}s, n={nodes}, sd={depth + rng.randrange(4, 12)}'


def raw_game(setup: MatchSetup, opening: Opening, round_number: int, dev_is_white: bool, rng: random.Random) -> str:
    """One game as the match runner writes it: full headers, move numbers and a closing reason."""

    edge = setup.dev_edge if dev_is_white else -setup.dev_edge
    result = white_result(opening, edge, rng)
    ending = pick(DRAWN_ENDINGS if result == '1/2-1/2' else DECISIVE_ENDINGS, rng)
    plies = ply_count(ending, rng)
    white_to_move = opening.white_moves_first
    if ending.kind == 'mate' and (plies % 2 == 1) != ((result == '1-0') == white_to_move):
        plies += 1

    start_cp = 260 * (opening.white_share - 0.5) + rng.gauss(0, 10)
    evals = white_evals(result, ending, start_cp, plies, rng)
    first_number = int(opening.fen.split()[5])
    tokens: list[str] = []

    for ply in range(plies):
        white_moves = white_to_move == (ply % 2 == 0)
        number = first_number + (ply + (0 if white_to_move else 1)) // 2
        if white_moves:
            tokens.append(f'{number}.')
        elif ply == 0:
            tokens.append(f'{number}...')

        last = ply == plies - 1
        mate_in = (plies - ply + 1) // 2 if ending.kind == 'mate' and plies - ply <= 6 else None
        quiet = ending.kind == 'flat' and plies - ply <= QUIET_ENDING_PLIES
        san = placeholder_move(rng, quiet) + ('#' if last and ending.kind == 'mate' else '')
        own_cp = evals[ply] if white_moves else -evals[ply]
        nps = setup.dev_nps if white_moves == dev_is_white else setup.base_nps
        comment = move_comment(own_cp, mate_in, ply + 1, nps, rng)
        if last:
            winner, loser = ('White', 'Black') if result == '1-0' else ('Black', 'White')
            comment += ', ' + ending.reason.format(winner=winner, loser=loser)
        tokens.append(f'{san} {{{comment}}}')

    white, black = (
        (f'{setup.engine}-dev', f'{setup.engine}-base')
        if dev_is_white
        else (f'{setup.engine}-base', f'{setup.engine}-dev')
    )
    headers = {
        'Event': 'Fastchess Tournament',
        'Site': '?',
        'Date': '2026.10.01',
        'Round': str(round_number),
        'White': white,
        'Black': black,
        'Result': result,
        'SetUp': '1',
        'FEN': opening.fen,
        'GameDuration': '00:00:21',
        'GameEndTime': '2026-10-01T12:00:00 +0000',
        'PlyCount': str(plies),
        'Termination': ending.termination,
        'TimeControl': setup.time_control,
    }
    header_text = '\n'.join(f'[{name} "{value}"]' for name, value in headers.items())
    movetext = ' '.join([*tokens, result])
    return f'{header_text}\n\n{movetext}\n\n'


def raw_runner_file(setup: MatchSetup, pairs: int, rng: random.Random) -> str:
    """One match-runner PGN: each round plays an opening twice with the colours reversed."""

    games = []
    for round_number in range(1, pairs + 1):
        opening = rng.choice(OPENINGS)
        games.append(raw_game(setup, opening, round_number, True, rng))
        games.append(raw_game(setup, opening, round_number, False, rng))
    return ''.join(games)
