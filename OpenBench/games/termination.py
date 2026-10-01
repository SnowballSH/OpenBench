from collections.abc import Sequence

from OpenBench.games.domain import Adjudication, Colour, Termination
from OpenBench.games.pgn import Game, Move

HEADER_TERMINATIONS = (
    ('time', Termination.TIME_LOSS),
    ('illegal', Termination.ILLEGAL_MOVE),
    ('abandon', Termination.DISCONNECT),
    ('stall', Termination.DISCONNECT),
)

COMMENT_TERMINATIONS = (
    ('mates', Termination.CHECKMATE),
    ('checkmate', Termination.CHECKMATE),
    ('on time', Termination.TIME_LOSS),
    ('illegal', Termination.ILLEGAL_MOVE),
    ('disconnect', Termination.DISCONNECT),
    ('stall', Termination.DISCONNECT),
    ('repetition', Termination.REPETITION),
    ('fifty', Termination.FIFTY_MOVES),
    ('50-move', Termination.FIFTY_MOVES),
    ('50 move', Termination.FIFTY_MOVES),
    ('stalemate', Termination.STALEMATE),
    ('insufficient', Termination.INSUFFICIENT_MATERIAL),
)

DECISIVE = frozenset({'1-0', '0-1'})


def first_match(text: str, table: Sequence[tuple[str, Termination]]) -> Termination | None:
    lowered = text.lower()
    return next((termination for needle, termination in table if needle in lowered), None)


def stated_termination(game: Game) -> Termination | None:
    """What the match runner wrote, when the PGN still carries it."""

    decisive = game.result in DECISIVE
    comment = game.final_comment

    if stated := first_match(comment, COMMENT_TERMINATIONS):
        return stated
    if 'adjudication' in comment.lower():
        return Termination.WIN_ADJUDICATION if decisive else Termination.DRAW_ADJUDICATION

    header = game.headers.get('Termination', '')
    if stated := first_match(header, HEADER_TERMINATIONS):
        return stated
    if 'adjudication' in header.lower():
        return Termination.WIN_ADJUDICATION if decisive else Termination.DRAW_ADJUDICATION
    return None


def own_scores(moves: Sequence[Move], mover_of_last: bool, count: int) -> list[int | None]:
    start = len(moves) - 1 if mover_of_last else len(moves) - 2
    return [moves[index].score_cp for index in range(start, -1, -2)[:count]]


def resigned(game: Game, rules: Adjudication, first_mover: Colour) -> bool:

    if rules.win_score_cp is None:
        return False

    loser = Colour.BLACK if game.result == '1-0' else Colour.WHITE
    last_mover = first_mover if len(game.moves) % 2 else first_mover.other
    scores = own_scores(game.moves, loser is last_mover, rules.win_moves)

    return len(scores) == rules.win_moves and all(
        score is not None and score <= -rules.win_score_cp for score in scores
    )


def drawn_by_scores(game: Game, rules: Adjudication, first_move_number: int) -> bool:

    if rules.draw_score_cp is None:
        return False

    plies = 2 * rules.draw_moves
    scores = [move.score_cp for move in game.moves[-plies:]]
    move_number = first_move_number + len(game.moves) // 2

    return (
        len(scores) == plies
        and move_number >= rules.draw_from_move
        and all(score is not None and abs(score) <= rules.draw_score_cp for score in scores)
    )


def inferred_termination(game: Game, rules: Adjudication, first_mover: Colour, first_move_number: int) -> Termination:

    if game.result not in DECISIVE:
        drawn = drawn_by_scores(game, rules, first_move_number)
        return Termination.DRAW_ADJUDICATION if drawn else Termination.DRAW_BY_RULE

    if game.moves and game.moves[-1].mates:
        return Termination.CHECKMATE
    if resigned(game, rules, first_mover):
        return Termination.WIN_ADJUDICATION
    return Termination.UNEXPLAINED_WIN
