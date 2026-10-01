import bz2
import functools
import io
import os
import random
import tarfile
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase, TestCase

from OpenBench.games.aggregate import STATE_VERSION, Aggregate, PairTracker, pair_kind
from OpenBench.games.archive import Budget, analyse_archive, analyse_member, on_member_boundary
from OpenBench.games.domain import Adjudication, Colour, Outcome, Side, Termination, adjudication_of
from OpenBench.games.facts import game_facts
from OpenBench.games.pgn import MATE_CP, Game, parse_games
from OpenBench.games.report import game_report, length_summary
from OpenBench.games.service import VIEW_BUDGET, archive_path, refresh, refresh_after_archiving
from OpenBench.games.synthetic import MatchSetup, client_pgn_util, raw_runner_file, upload_batch
from OpenBench.models import PGN, GameAnalysis
from OpenBench.pgn_watcher import PGNWatcher
from OpenBench.tests.fixtures import (
    create_engine_config,
    create_test,
    create_user,
    credentials,
    ensure_book,
    present,
    use_temporary_media,
)

WHITE_FIRST = 'rnbqkb1r/ppp2ppp/4pn2/3p4/2PP4/2N5/PP2PPPP/R1BQKBNR w KQkq - 2 4'
BLACK_FIRST = 'rnbqkbnr/ppp2ppp/4p3/3p4/3PP3/2N5/PPP2PPP/R1BQKBNR b KQkq - 1 3'
ONCE = 'rnbqkbnr/pp2pppp/2p5/3p4/2PP4/8/PP2PPPP/RNBQKBNR w KQkq - 0 3'
RULES = adjudication_of('movecount=3 score=400', 'movenumber=40 movecount=8 score=10')
UNLIMITED = Budget(compressed_bytes=1 << 40, games=1 << 40, seconds=3600.0)
ONE_MEMBER = Budget(compressed_bytes=0, games=0, seconds=3600.0)
DEV, BASE = 'Avalanche-dev', 'Avalanche-base'
NO_DEADLINE = float('inf')
SETTLE = 'OpenBench.games.archive.TAIL_SETTLE_SECONDS'


def comment(score: str, depth: int = 12, reason: str = '') -> str:
    return f'{score}/{depth} 0.100s, n=120000, sd={depth + 5}' + (f', {reason}' if reason else '')


def raw_game(
    result: str,
    scores: list[str],
    dev_is_white: bool = True,
    fen: str = WHITE_FIRST,
    round_number: int = 1,
    mate: bool = False,
    reason: str = '',
    termination: str = 'normal',
    sans: dict[int, str] | None = None,
) -> str:
    """A game as fastchess writes it, before the Client reformats it for upload."""

    white_first = fen.split()[1] == 'w'
    number = int(fen.split()[5])
    tokens = []
    for ply, score in enumerate(scores):
        white_moves = white_first == (ply % 2 == 0)
        if white_moves:
            tokens.append(f'{number}.')
        elif ply == 0:
            tokens.append(f'{number}...')
        last = ply == len(scores) - 1
        san = (sans or {}).get(ply, 'Qh7' if white_moves else 'Qh2') + ('#' if last and mate else '')
        tokens.append(f'{san} {{{comment(score, reason=reason if last else "")}}}')
        number += not white_moves

    white, black = (DEV, BASE) if dev_is_white else (BASE, DEV)
    headers = {
        'Event': 'Fastchess Tournament',
        'Site': '?',
        'Date': '2026.10.01',
        'Round': str(round_number),
        'White': white,
        'Black': black,
        'Result': result,
        'SetUp': '1',
        'FEN': fen,
        'GameEndTime': '2026-10-01T12:00:00 +0000',
        'PlyCount': str(len(scores)),
        'Termination': termination,
        'TimeControl': '8+0.08',
    }
    header_text = '\n'.join(f'[{name} "{value}"]' for name, value in headers.items())
    return f'{header_text}\n\n{" ".join([*tokens, result])}\n\n'


def uploaded(raw: str, compact: bool = True) -> str:
    return bz2.decompress(upload_batch([raw], 1.0, compact)).decode()


def parsed(text: str) -> list[Game | None]:
    return list(parse_games(text.splitlines()))


def only_game(raw: str, compact: bool = True) -> Game:
    games = parsed(uploaded(raw, compact))
    assert len(games) == 1
    return present(games[0])


def facts_of(raw: str, compact: bool = True, rules: Adjudication = RULES):
    return present(game_facts(only_game(raw, compact), rules))


def aggregate_of(*raws: str, compact: bool = True) -> Aggregate:
    aggregate = Aggregate()
    analyse_member(io.BytesIO(upload_batch(list(raws), 1.0, compact)), aggregate, RULES, NO_DEADLINE)
    return aggregate


def write_archive(path: Path, members: Sequence[tuple[str, bytes]], mode: Literal['w', 'a'] = 'w') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, mode) as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))


@functools.cache
def synthetic_batches(count: int, seed: int = 7, compact: bool = True) -> tuple[tuple[str, bytes], ...]:
    rng = random.Random(seed)
    return tuple(
        (f'1.1.{index}.pgn.bz2', upload_batch([raw_runner_file(MatchSetup(), 4, rng)], 1.0, compact))
        for index in range(count)
    )


class ClientDialectTests(SimpleTestCase):
    def test_compact_upload_keeps_score_and_depth_only(self):
        game = only_game(raw_game('1-0', ['+0.31', '-0.25', '+M1'], mate=True, reason='White mates'))

        self.assertEqual(game.headers['White'], DEV)
        self.assertEqual(game.headers['ScaleFactor'], '1.0')
        self.assertNotIn('Termination', game.headers)
        self.assertEqual(game.result, '1-0')
        self.assertEqual([move.san for move in game.moves], ['Qh7', 'Qh2', 'Qh7#'])
        self.assertEqual([move.score_cp for move in game.moves], [31, -25, MATE_CP])
        self.assertEqual([move.depth for move in game.moves], [12, 12, 12])
        self.assertEqual({move.time_ms for move in game.moves}, {None})
        self.assertTrue(game.moves[-1].mates)
        self.assertEqual(game.final_comment, '+M1/12')

    def test_verbose_upload_adds_time_nodes_and_seldepth(self):
        game = only_game(raw_game('1/2-1/2', ['+0.31', '-0.25']), compact=False)

        self.assertIn('GameEndTime', game.headers)
        self.assertEqual([(move.time_ms, move.nodes, move.seldepth) for move in game.moves], [(100, 120000, 17)] * 2)

    def test_engine_pgncomment_lines_do_not_leak_into_the_numbers(self):
        raw = raw_game('1/2-1/2', ['+0.31', '-0.25']).replace(
            'sd=17}', 'sd=17, line="info string pgncomment +9.99/99 7.5s n=1"}', 1
        )
        for compact in (True, False):
            first = only_game(raw, compact).moves[0]
            self.assertEqual((first.score_cp, first.depth), (31, 12))
            self.assertIn(first.time_ms, (None, 100))

    def test_book_moves_and_missing_comments_carry_no_score(self):
        raw = (
            f'[White "{DEV}"]\n[Black "{BASE}"]\n[Result "0-1"]\n\n'
            '1. e4 {book} e5 {book} 2. Nf3 Nc6 {-0.20/9 0.050s, n=10, sd=11} 0-1\n\n'
        )
        game = only_game(raw)

        self.assertEqual([move.book for move in game.moves], [True, True, False, False])
        self.assertEqual([move.score_cp for move in game.moves], [None, None, None, -20])
        self.assertEqual(present(game_facts(game, RULES)).opening, 'e4 e5')

    def test_the_synthetic_generator_is_stable_under_the_client_formatter(self):
        raw = raw_runner_file(MatchSetup(), 6, random.Random(3))
        for compact in (True, False):
            once = uploaded(raw, compact)
            self.assertEqual(uploaded(once, compact), once)
            games = parsed(once)
            self.assertEqual(len(games), 12)
            self.assertNotIn(None, games)

    def test_unprocessed_runner_output_parses_too(self):
        raw = raw_game('1/2-1/2', ['+0.31', '-0.25', '0.00'], fen=BLACK_FIRST, reason='Draw by 3-fold repetition')
        wrapped = raw.replace(' {', '\n{', 2)
        game = present(parsed(wrapped)[0])

        self.assertEqual([move.san for move in game.moves], ['Qh2', 'Qh7', 'Qh2'])
        self.assertEqual(game.moves[0].nodes, 120000)
        self.assertIn('repetition', game.final_comment)

    def test_client_loader_finds_the_formatter(self):
        self.assertTrue(callable(client_pgn_util().compress_pgn_files))


class MalformedGameTests(SimpleTestCase):
    GOOD = f'[White "{DEV}"]\n[Black "{BASE}"]\n[Result "1-0"]\n\ne4 {{+0.10/5}} 1-0\n\n'

    def assert_skipped(self, bad: str) -> None:
        games = parsed(self.GOOD + bad + self.GOOD)
        self.assertEqual([game is None for game in games], [False, True, False], bad)

    def test_bad_blocks_are_reported_and_their_neighbours_kept(self):
        self.assert_skipped(f'[White "{DEV}"]\n[Black "{BASE}"]\n\ne4 {{+0.10/5}} 1-0\n\n')
        self.assert_skipped(f'[White "{DEV}"]\n[Black "{BASE}"]\n[Result "1-0"]\n\ne4 {{+0.10/5}}\n\n')
        self.assert_skipped(f'[White "{DEV}"]\n[Black "{BASE}"]\n[Result "1-0"]\n\ne4 {{+0.10/5}} 0-1\n\n')
        self.assert_skipped(f'[White "{DEV}"]\n[Black "{BASE}"]\n[Result "1-0"]\n\n<html> 1-0\n\n')
        self.assert_skipped(f'[White "{DEV}"]\n[Black "{BASE}"]\n[Result "7-0"]\n\ne4 7-0\n\n')
        self.assert_skipped(f'[White "{DEV}"\n[Black "{BASE}"]\n[Result "1-0"]\n\ne4 1-0\n\n')
        self.assert_skipped('e4 e5 1-0\n\n')

    def test_a_game_cut_off_mid_move_is_malformed(self):
        games = parsed(self.GOOD + self.GOOD[:-12])
        self.assertEqual([game is None for game in games], [False, True])

    def test_a_missing_blank_line_between_games_still_splits_them(self):
        self.assertEqual(len([game for game in parsed(self.GOOD.rstrip() + '\n' + self.GOOD) if game]), 2)

    def test_games_between_unknown_engines_are_not_counted_as_games(self):
        text = self.GOOD.replace(DEV, 'Stockfish').replace(BASE, 'Other')
        aggregate = Aggregate()
        analyse_member(io.BytesIO(bz2.compress(text.encode())), aggregate, RULES, NO_DEADLINE)
        self.assertEqual((aggregate.totals['games'], aggregate.totals['malformed']), (0, 1))

    def test_unfinished_games_are_set_aside(self):
        aggregate = aggregate_of(raw_game('*', ['+0.10', '-0.10']), raw_game('1-0', ['+0.10'], mate=True))
        self.assertEqual((aggregate.totals['games'], aggregate.totals['unfinished']), (1, 1))
        self.assertEqual(aggregate.totals['unpaired'], 1)


class TerminationTests(SimpleTestCase):
    def test_uploads_lose_the_reason_so_it_is_inferred(self):
        cases = [
            (raw_game('1-0', ['+9.00', '-9.00', '+M1'], mate=True), Termination.CHECKMATE),
            (raw_game('1-0', ['+5.00', '-4.00'] * 3), Termination.WIN_ADJUDICATION),
            (raw_game('0-1', ['-5.00', '+4.50'] * 3), Termination.WIN_ADJUDICATION),
            (raw_game('1-0', ['+5.00', '-4.00', '+5.00', '-3.99', '+5.00', '-4.00']), Termination.UNEXPLAINED_WIN),
            (raw_game('1-0', ['+0.20', '-0.20']), Termination.UNEXPLAINED_WIN),
            (raw_game('1/2-1/2', ['0.00', '+0.05'] * 40), Termination.DRAW_ADJUDICATION),
            (raw_game('1/2-1/2', ['0.00', '+0.05'] * 20), Termination.DRAW_BY_RULE),
            (raw_game('1/2-1/2', ['+0.30', '-0.30'] * 40), Termination.DRAW_BY_RULE),
        ]
        for raw, expected in cases:
            facts = facts_of(raw)
            self.assertEqual(facts.termination, expected)
            self.assertTrue(facts.termination_inferred)

    def test_a_capture_or_pawn_move_restarts_the_draw_count(self):
        flat = ['0.00', '+0.05'] * 40
        for ply, san, expected in (
            (64, 'Rxe5', Termination.DRAW_ADJUDICATION),
            (64, 'e5', Termination.DRAW_ADJUDICATION),
            (65, 'Rxe5', Termination.DRAW_BY_RULE),
            (70, 'h4', Termination.DRAW_BY_RULE),
            (79, 'gxh8=Q+', Termination.DRAW_BY_RULE),
            (70, 'O-O', Termination.DRAW_ADJUDICATION),
        ):
            facts = facts_of(raw_game('1/2-1/2', flat, sans={ply: san}))
            self.assertEqual(facts.termination, expected, (ply, san))

    def test_a_score_outside_the_bound_restarts_the_draw_count(self):
        flat = ['0.00'] * 80
        self.assertEqual(
            facts_of(raw_game('1/2-1/2', [*flat[:70], '+0.30', *flat[71:]])).termination, Termination.DRAW_BY_RULE
        )
        self.assertEqual(
            facts_of(raw_game('1/2-1/2', [*flat[:63], '+0.30', *flat[64:]])).termination,
            Termination.DRAW_ADJUDICATION,
        )

    def test_the_draw_move_number_counts_from_the_fen_as_fastchess_does(self):
        late = WHITE_FIRST.replace(' 2 4', ' 2 30')
        self.assertEqual(
            facts_of(raw_game('1/2-1/2', ['0.00'] * 22, fen=late)).termination, Termination.DRAW_ADJUDICATION
        )
        self.assertEqual(facts_of(raw_game('1/2-1/2', ['0.00'] * 20, fen=late)).termination, Termination.DRAW_BY_RULE)

        black_late = BLACK_FIRST.replace(' 1 3', ' 1 30')
        self.assertEqual(
            facts_of(raw_game('1/2-1/2', ['0.00'] * 21, fen=black_late)).termination, Termination.DRAW_ADJUDICATION
        )
        self.assertEqual(facts_of(raw_game('1/2-1/2', ['0.00'] * 22)).termination, Termination.DRAW_BY_RULE)

    def test_the_loser_may_have_moved_last(self):
        raw = raw_game('1-0', ['+5.00', '-4.00', '+5.00', '-4.00', '+5.00', '-4.00', '+5.00'])
        self.assertEqual(facts_of(raw).termination, Termination.WIN_ADJUDICATION)

    def test_no_adjudication_rules_means_no_adjudication(self):
        off = adjudication_of('None', 'None')
        self.assertEqual(
            facts_of(raw_game('1-0', ['+5.00', '-4.00'] * 3), rules=off).termination, Termination.UNEXPLAINED_WIN
        )
        self.assertEqual(facts_of(raw_game('1/2-1/2', ['0.00'] * 90), rules=off).termination, Termination.DRAW_BY_RULE)

    def test_a_reason_still_in_the_pgn_is_used_as_written(self):
        cases = {
            'Black loses on time': Termination.TIME_LOSS,
            'Black makes an illegal move': Termination.ILLEGAL_MOVE,
            'Black disconnects': Termination.DISCONNECT,
            "Black's connection stalls": Termination.DISCONNECT,
            'White wins by adjudication': Termination.WIN_ADJUDICATION,
            'White mates': Termination.CHECKMATE,
        }
        for reason, expected in cases.items():
            game = present(parsed(raw_game('1-0', ['+0.50', '-0.50'], reason=reason))[0])
            facts = present(game_facts(game, RULES))
            self.assertEqual((facts.termination, facts.termination_inferred), (expected, False), reason)

        drawn = {
            'Draw by 3-fold repetition': Termination.REPETITION,
            'Draw by fifty moves rule': Termination.FIFTY_MOVES,
            'Draw by stalemate': Termination.STALEMATE,
            'Draw by insufficient mating material': Termination.INSUFFICIENT_MATERIAL,
            'Draw by adjudication': Termination.DRAW_ADJUDICATION,
        }
        for reason, expected in drawn.items():
            game = present(parsed(raw_game('1/2-1/2', ['+0.50', '-0.50'], reason=reason))[0])
            self.assertEqual(present(game_facts(game, RULES)).termination, expected, reason)

    def test_a_termination_header_is_a_fallback(self):
        game = present(parsed(raw_game('0-1', ['+0.50', '-0.50'], termination='time forfeit'))[0])
        self.assertEqual(present(game_facts(game, RULES)).termination, Termination.TIME_LOSS)

    def test_a_score_of_fifty_centipawns_is_not_the_fifty_move_rule(self):
        self.assertEqual(facts_of(raw_game('1/2-1/2', ['+0.50', '-0.50'])).termination, Termination.DRAW_BY_RULE)


class GameFactsTests(SimpleTestCase):
    def test_outcome_and_colour_follow_the_engine_names(self):
        as_white = facts_of(raw_game('0-1', ['+0.10', '-0.10']))
        as_black = facts_of(raw_game('0-1', ['+0.10', '-0.10'], dev_is_white=False))

        self.assertEqual((as_white.dev_colour, as_white.outcome), (Colour.WHITE, Outcome.LOSS))
        self.assertEqual((as_black.dev_colour, as_black.outcome), (Colour.BLACK, Outcome.WIN))

    def test_first_eval_is_seen_from_white_whoever_moves_first(self):
        self.assertEqual(facts_of(raw_game('1/2-1/2', ['+0.40', '-0.30'])).first_eval_white_cp, 40)
        self.assertEqual(facts_of(raw_game('1/2-1/2', ['-0.40', '+0.30'], fen=BLACK_FIRST)).first_eval_white_cp, 40)

    def test_peaks_belong_to_the_side_that_reported_them(self):
        facts = facts_of(raw_game('1/2-1/2', ['-0.40', '+3.20', '-0.10', '+0.30'], fen=BLACK_FIRST))

        self.assertEqual(facts.peak_cp, {Side.BASE: -10, Side.DEV: 320})
        self.assertEqual(facts.advantages_reached(Side.DEV), [100, 300])
        self.assertEqual(facts.advantages_reached(Side.BASE), [])

    def test_opening_key_ignores_the_move_counters(self):
        self.assertEqual(facts_of(raw_game('1-0', ['+0.10'])).opening, ' '.join(WHITE_FIRST.split()[:4]))

    def test_usage_is_split_by_side_and_phase(self):
        facts = facts_of(raw_game('1/2-1/2', ['+0.10'] * 42), compact=False)

        early_dev = facts.usage[(Side.DEV, 'early')]
        self.assertEqual(
            (early_dev.depth_moves, early_dev.depth, early_dev.time_ms, early_dev.nodes), (20, 240, 2000, 2400000)
        )
        self.assertEqual(facts.usage[(Side.BASE, 'middle')].timed_moves, 1)

    def test_compact_games_have_depth_but_no_timing(self):
        spent = facts_of(raw_game('1/2-1/2', ['+0.10'] * 4)).usage[(Side.DEV, 'early')]
        self.assertEqual((spent.depth_moves, spent.timed_moves), (2, 0))


class AggregationTests(SimpleTestCase):
    def pair(self, as_white: str, as_black: str, round_number: int, fen: str = WHITE_FIRST) -> list[str]:
        return [
            raw_game(as_white, ['+0.10', '-0.10'], round_number=round_number, fen=fen),
            raw_game(as_black, ['+0.10', '-0.10'], round_number=round_number, fen=fen, dev_is_white=False),
        ]

    def report(self, aggregate: Aggregate):
        row = GameAnalysis(state=aggregate.to_state(), games=aggregate.totals['games'], members=1, complete=True)
        row.updated = mock.sentinel.updated
        return game_report(row, 0)

    def test_pair_kinds_are_order_free(self):
        self.assertEqual(pair_kind(Outcome.LOSS, Outcome.WIN), 'WL')
        self.assertEqual(pair_kind(Outcome.DRAW, Outcome.WIN), 'WD')
        self.assertEqual(pair_kind(Outcome.DRAW, Outcome.DRAW), 'DD')

    def test_pairs_split_the_middle_pentanomial_bucket(self):
        games = [
            *self.pair('1-0', '1-0', 1),
            *self.pair('0-1', '0-1', 2),
            *self.pair('1/2-1/2', '1/2-1/2', 3),
            *self.pair('1-0', '0-1', 4),
            *self.pair('1-0', '1/2-1/2', 5, fen=BLACK_FIRST),
            *self.pair('0-1', '1-0', 6, fen=BLACK_FIRST),
        ]
        report = self.report(aggregate_of(*games))
        pairs = report.pairs

        self.assertEqual((pairs.ww, pairs.wd, pairs.wl, pairs.dd, pairs.dl, pairs.ll), (1, 1, 2, 1, 0, 1))
        self.assertEqual(pairs.pentanomial, [1, 0, 3, 1, 1])
        self.assertAlmostEqual(present(pairs.middle_wl_share), 2 / 3)
        self.assertEqual((pairs.white_sweeps, pairs.black_sweeps), (1, 1))
        self.assertEqual(report.limits.unpaired_games, 0)

        colour = report.colour
        self.assertEqual((colour.dev_as_white.wins, colour.dev_as_white.draws, colour.dev_as_white.losses), (3, 1, 2))
        self.assertEqual((colour.dev_as_black.wins, colour.dev_as_black.draws, colour.dev_as_black.losses), (2, 2, 2))
        self.assertEqual((colour.white.wins, colour.white.draws, colour.white.losses), (5, 3, 4))
        self.assertAlmostEqual(present(colour.white.score), 6.5 / 12)

    def test_openings_rank_lopsided_colour_bound_and_drawn(self):
        games = [
            *self.pair('1-0', '0-1', 1),
            *self.pair('1-0', '0-1', 2),
            *self.pair('1/2-1/2', '1/2-1/2', 3, fen=BLACK_FIRST),
            *self.pair('1/2-1/2', '1/2-1/2', 4, fen=BLACK_FIRST),
            *self.pair('1-0', '1-0', 5, fen=ONCE),
        ]
        openings = self.report(aggregate_of(*games)).openings
        white_first, black_first = (' '.join(fen.split()[:4]) for fen in (WHITE_FIRST, BLACK_FIRST))

        self.assertEqual((openings.tracked, openings.repeated), (3, 2))
        self.assertEqual(
            [(row.opening, row.pairs, row.dev_score) for row in openings.lopsided], [(white_first, 2, 1.0)]
        )
        self.assertEqual(openings.colour_bound, [])
        self.assertEqual([(row.opening, row.pairs) for row in openings.drawn], [(black_first, 2)])
        self.assertEqual(openings.always_drawn, 1)

        swept = self.report(
            aggregate_of(*self.pair('1-0', '1-0', 1), *self.pair('1/2-1/2', '1/2-1/2', 2))
        ).openings.colour_bound
        self.assertEqual([(row.white_sweeps, row.black_sweeps) for row in swept], [(1, 0)])

    def test_the_same_opening_in_another_round_is_another_pair(self):
        first, second = self.pair('1-0', '1/2-1/2', 1), self.pair('0-1', '1-0', 2)
        aggregate = aggregate_of(first[0], second[0], second[1], first[1])
        self.assertEqual(dict(aggregate.pairs), {'WD': 1, 'LL': 1})

    def test_games_without_a_partner_are_counted(self):
        aggregate = aggregate_of(*self.pair('1-0', '1-0', 1), raw_game('1-0', ['+0.10'], round_number=2))
        self.assertEqual((sum(aggregate.pairs.values()), aggregate.totals['unpaired']), (1, 1))

    def test_pairs_never_span_two_batches(self):
        first, second = self.pair('1-0', '1-0', 1)
        aggregate = Aggregate()
        for raw in (first, second):
            analyse_member(io.BytesIO(upload_batch([raw], 1.0, True)), aggregate, RULES, NO_DEADLINE)
        self.assertEqual((sum(aggregate.pairs.values()), aggregate.totals['unpaired']), (0, 2))

    def test_openings_beyond_the_limit_are_counted_not_tracked(self):
        aggregate = Aggregate()
        with mock.patch('OpenBench.games.aggregate.MAX_TRACKED_OPENINGS', 1):
            aggregate.add_pair('a', Outcome.WIN, Outcome.WIN)
            aggregate.add_pair('b', Outcome.WIN, Outcome.WIN)
            aggregate.add_pair('a', Outcome.DRAW, Outcome.DRAW)
        self.assertEqual(list(aggregate.openings), ['a'])
        self.assertEqual(aggregate.totals['untracked_opening_pairs'], 1)
        self.assertEqual(sum(aggregate.pairs.values()), 3)

    def test_length_quartiles_interpolate(self):
        summary = length_summary({10: 1, 20: 1, 30: 1, 40: 1, 100: 1})
        self.assertEqual((summary.q1, summary.median, summary.q3, summary.mean, summary.longest), (20, 30, 40, 40, 100))
        self.assertEqual(length_summary({10: 1, 20: 1}).median, 15)
        self.assertEqual(length_summary({}).median, None)

    def test_lengths_separate_decisive_from_drawn(self):
        games = [raw_game('1-0', ['+0.10'] * 21), raw_game('1/2-1/2', ['+0.10'] * 50), raw_game('0-1', ['+0.10'] * 20)]
        lengths = self.report(aggregate_of(*games)).lengths

        self.assertEqual((lengths.decisive.games, lengths.decisive.median), (2, 20.5))
        self.assertEqual((lengths.drawn.games, lengths.drawn.median), (1, 50))
        self.assertEqual(
            [(row.first_ply, row.last_ply, row.decisive, row.drawn) for row in lengths.histogram],
            [(1, 20, 1, 0), (21, 40, 1, 0), (41, 60, 0, 1)],
        )

    def test_terminations_are_shares_of_finished_games(self):
        games = [raw_game('1-0', ['+0.10'], mate=True)] * 3 + [raw_game('1/2-1/2', ['+0.30', '-0.30'])]
        rows = self.report(aggregate_of(*games)).terminations.rows
        self.assertEqual(
            [(row.key, row.games, row.share) for row in rows],
            [(Termination.CHECKMATE, 3, 0.75), (Termination.DRAW_BY_RULE, 1, 0.25)],
        )

    def test_unconverted_advantages_and_book_balance(self):
        games = [
            raw_game('1/2-1/2', ['+3.50', '-0.20']),
            raw_game('1-0', ['+1.50', '-1.50']),
            raw_game('0-1', ['-0.50', '+1.20'], dev_is_white=False),
        ]
        evals = present(self.report(aggregate_of(*games)).evals)
        rows = {(row.side, row.threshold_cp): row for row in evals.advantage}

        dev = rows[(Side.DEV, 100)]
        self.assertEqual((dev.reached, dev.won, dev.drawn, dev.lost), (3, 2, 1, 0))
        self.assertAlmostEqual(present(dev.not_won_share), 1 / 3)
        self.assertEqual((rows[(Side.DEV, 300)].reached, rows[(Side.DEV, 300)].drawn), (1, 1))
        self.assertEqual(rows[(Side.BASE, 100)].reached, 0)
        self.assertEqual((evals.book.games, evals.book.mean_white_cp, evals.book.mean_abs_cp), (3, 150, 550 / 3))
        self.assertFalse(evals.has_timing)

    def test_verbose_games_report_speed_by_phase(self):
        evals = present(self.report(aggregate_of(raw_game('1/2-1/2', ['+0.10'] * 42), compact=False)).evals)
        early, middle, late = evals.phases

        self.assertTrue(evals.has_timing)
        self.assertEqual(
            (early.dev.moves, early.dev.mean_depth, early.dev.nps, early.dev.mean_time_ms), (20, 12, 1.2e6, 100)
        )
        self.assertEqual((middle.base.moves, late.dev.moves, late.dev.nps), (1, 0, None))

    def test_games_without_scores_have_no_eval_section(self):
        text = f'[White "{DEV}"]\n[Black "{BASE}"]\n[Result "1-0"]\n\ne4 {{unknown}} e5 {{unknown}} 1-0\n\n'
        aggregate = Aggregate()
        analyse_member(io.BytesIO(bz2.compress(text.encode())), aggregate, RULES, NO_DEADLINE)
        self.assertIsNone(self.report(aggregate).evals)

    def test_state_round_trips_through_json(self):
        aggregate = aggregate_of(*self.pair('1-0', '0-1', 1))
        self.assertEqual(Aggregate.from_state(aggregate.to_state()).to_state(), aggregate.to_state())

    def test_pair_tracker_forgets_nothing(self):
        aggregate = Aggregate()
        tracker = PairTracker(aggregate)
        tracker.add(facts_of(raw_game('1-0', ['+0.10'])))
        tracker.close()
        self.assertEqual((aggregate.totals['unpaired'], tracker.waiting), (1, {}))


class ArchiveTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / '1.pgn.tar'
        self.enterContext(mock.patch(SETTLE, 0.0))

    def analyse(self, offset: int = 0, aggregate: Aggregate | None = None, budget: Budget = UNLIMITED):
        aggregate = aggregate if aggregate is not None else Aggregate()
        return aggregate, analyse_archive(self.path, offset, aggregate, RULES, budget)

    def test_two_halves_equal_one_pass(self):
        batches = synthetic_batches(6)
        write_archive(self.path, batches)
        whole, progress = self.analyse()
        self.assertTrue(progress.reached_end)
        self.assertEqual((progress.members, whole.totals['games']), (6, 48))

        write_archive(self.path, batches[:3])
        stored, first = self.analyse()
        write_archive(self.path, batches[3:], mode='a')
        resumed, second = self.analyse(first.offset, Aggregate.from_state(stored.to_state()))

        self.assertEqual((first.members, second.members), (3, 3))
        self.assertEqual(second.offset, progress.offset)
        self.assertEqual(resumed.to_state(), whole.to_state())

    def test_a_budget_stops_between_batches_and_resumes(self):
        write_archive(self.path, synthetic_batches(4))
        whole, _ = self.analyse()

        aggregate, offset, passes = Aggregate(), 0, 0
        while True:
            aggregate, progress = self.analyse(offset, Aggregate.from_state(aggregate.to_state()), ONE_MEMBER)
            offset, passes = progress.offset, passes + 1
            if progress.reached_end:
                break
            self.assertEqual(progress.members, 1)

        self.assertEqual(passes, 4)
        self.assertEqual(aggregate.to_state(), whole.to_state())

    def test_a_truncated_batch_keeps_its_complete_games_and_is_flagged(self):
        good, cut = synthetic_batches(2)
        write_archive(self.path, [good, (cut[0], cut[1][: len(cut[1]) // 2]), ('1.1.9.pgn.bz2', b'not bzip2')])
        aggregate, progress = self.analyse()

        self.assertEqual((progress.members, progress.reached_end), (3, True))
        self.assertEqual(aggregate.totals['damaged_members'], 2)
        self.assertGreaterEqual(aggregate.totals['games'], 8)

    def test_a_batch_still_being_appended_is_left_for_the_next_pass(self):
        batches = synthetic_batches(2)
        write_archive(self.path, batches)
        complete = self.path.read_bytes()
        whole, end = self.analyse()

        with tarfile.open(self.path) as tar:
            second = tar.getmembers()[1]
        self.path.write_bytes(complete[: second.offset_data + second.size - 10])
        partial, progress = self.analyse()
        self.assertEqual((progress.members, partial.totals['games']), (1, 8))

        self.path.write_bytes(complete)
        resumed, progress = self.analyse(progress.offset, partial)
        self.assertEqual((progress.members, progress.offset), (1, end.offset))
        self.assertEqual(resumed.to_state(), whole.to_state())

    def test_a_header_written_into_the_old_padding_is_not_skipped(self):
        first, second = synthetic_batches(2)
        write_archive(self.path, [first])
        _, end = self.analyse()
        self.assertEqual(self.path.stat().st_size, tarfile.RECORDSIZE)

        header = tarfile.TarInfo(second[0])
        header.size = len(second[1])
        self.assertLess(end.offset + tarfile.BLOCKSIZE + header.size, tarfile.RECORDSIZE)
        with self.path.open('r+b') as handle:
            handle.seek(end.offset)
            handle.write(header.tobuf())

        with mock.patch(SETTLE, 30.0):
            racing, progress = self.analyse(end.offset)
            self.assertEqual((progress.members, progress.offset, progress.reached_end), (0, end.offset, True))
            self.assertEqual(racing.to_state(), Aggregate().to_state())

            write_archive(self.path, [first, second])
            finished, progress = self.analyse(end.offset)
        self.assertEqual((progress.members, finished.totals['games'], finished.totals['damaged_members']), (1, 8, 0))

    def test_a_damaged_last_batch_is_counted_once_the_archive_has_settled(self):
        write_archive(self.path, [*synthetic_batches(1), ('1.1.9.pgn.bz2', b'not bzip2')])
        with mock.patch(SETTLE, 30.0):
            fresh, progress = self.analyse()
            self.assertEqual((progress.members, fresh.totals['damaged_members']), (1, 0))

            os.utime(self.path, (0, 0))
            settled, progress = self.analyse(progress.offset, fresh)
        self.assertEqual((progress.members, settled.totals['damaged_members'], settled.totals['games']), (1, 1, 8))

    def test_a_damaged_batch_followed_by_another_is_passed_at_once(self):
        good = synthetic_batches(1)[0]
        write_archive(self.path, [('1.1.9.pgn.bz2', b'not bzip2'), good])
        with mock.patch(SETTLE, 30.0):
            aggregate, progress = self.analyse()
        self.assertEqual((progress.members, aggregate.totals['damaged_members'], aggregate.totals['games']), (2, 1, 8))

    def test_a_batch_that_outlasts_its_time_is_abandoned(self):
        write_archive(self.path, synthetic_batches(2))
        clock = iter(range(0, 10_000, 10))
        with mock.patch('OpenBench.games.archive.time.monotonic', side_effect=lambda: next(clock)):
            aggregate, progress = self.analyse(budget=Budget(compressed_bytes=1 << 40, games=1 << 40, seconds=15.0))

        self.assertEqual((progress.members, progress.reached_end), (1, False))
        self.assertEqual(aggregate.totals['damaged_members'], 1)
        self.assertLess(aggregate.totals['games'], 8)

    def test_a_decompression_bomb_stops_at_the_text_limit(self):
        bomb = bz2.compress(b'[White "' + b'x' * (4 << 20) + b'"]\n' * 3)
        self.assertLess(len(bomb), 4096)
        write_archive(self.path, [('1.1.0.pgn.bz2', bomb), *synthetic_batches(1)])
        with mock.patch('OpenBench.games.archive.MAX_MEMBER_CHARS', 1 << 20):
            aggregate, progress = self.analyse()
        self.assertEqual((progress.members, aggregate.totals['damaged_members'], aggregate.totals['games']), (2, 1, 8))

    def test_garbage_in_a_settled_archive_is_reported_as_misaligned(self):
        write_archive(self.path, synthetic_batches(1))
        _, end = self.analyse()
        self.assertFalse(end.misaligned)
        self.assertTrue(on_member_boundary(self.path, end.offset))
        self.assertFalse(on_member_boundary(self.path, end.offset - tarfile.BLOCKSIZE))
        with self.path.open('r+b') as handle:
            handle.seek(end.offset)
            handle.write(os.urandom(2048))

        with mock.patch(SETTLE, 30.0):
            self.assertFalse(self.analyse(end.offset)[1].misaligned)
        self.assertTrue(self.analyse(end.offset)[1].misaligned)

    def test_garbage_after_the_offset_ends_the_pass(self):
        write_archive(self.path, synthetic_batches(1))
        _, end = self.analyse()
        with self.path.open('r+b') as handle:
            handle.seek(end.offset)
            handle.write(os.urandom(2048))

        aggregate, progress = self.analyse(end.offset)
        self.assertEqual((progress.members, progress.offset, aggregate.totals['games']), (0, end.offset, 0))

    def test_an_oversized_batch_is_cut_off(self):
        write_archive(self.path, synthetic_batches(1))
        with mock.patch('OpenBench.games.archive.MAX_MEMBER_CHARS', 4000):
            aggregate, _ = self.analyse()
        self.assertEqual(aggregate.totals['damaged_members'], 1)
        self.assertLess(aggregate.totals['games'], 8)

    def test_directories_in_the_archive_are_skipped(self):
        write_archive(self.path, synthetic_batches(1))
        with tarfile.open(self.path, 'a') as tar:
            folder = tarfile.TarInfo('folder')
            folder.type = tarfile.DIRTYPE
            tar.addfile(folder)
        aggregate, progress = self.analyse()
        self.assertEqual((progress.members, aggregate.totals['games']), (2, 8))


class GameAnalysisTestCase(TestCase):
    def setUp(self):
        use_temporary_media(self)
        self.enterContext(mock.patch(SETTLE, 0.0))
        create_engine_config()
        ensure_book()
        self.author = create_user('author')
        self.test = create_test(
            self.author,
            upload_pgns='COMPACT',
            win_adj='movecount=3 score=400',
            draw_adj='movenumber=40 movecount=8 score=10',
        )

    def archive(self, batches, mode='w'):
        write_archive(archive_path(self.test.id), batches, mode)


class ServiceTests(GameAnalysisTestCase):
    def test_no_archive_means_no_analysis(self):
        with self.assertNumQueries(1):
            self.assertIsNone(refresh(self.test, UNLIMITED))
        self.assertFalse(GameAnalysis.objects.exists())

    def test_analysis_is_stored_and_extended(self):
        batches = synthetic_batches(4)
        self.archive(batches[:2])
        first = present(refresh(self.test, UNLIMITED))
        self.assertEqual((first.games, first.members, first.complete, first.version), (16, 2, True, STATE_VERSION))

        with self.assertNumQueries(1):
            self.assertEqual(present(refresh(self.test, UNLIMITED)).updated, first.updated)

        self.archive(batches[2:], mode='a')
        second = present(refresh(self.test, UNLIMITED))
        self.assertEqual((second.games, second.members), (32, 4))
        self.assertGreater(second.analysed_bytes, first.analysed_bytes)

        self.archive(batches)
        GameAnalysis.objects.all().delete()
        self.assertEqual(present(refresh(self.test, UNLIMITED)).state, second.state)

    def test_a_budget_leaves_the_analysis_incomplete_until_later_passes(self):
        self.archive(synthetic_batches(3))
        seen = []
        for _ in range(4):
            row = present(refresh(self.test, ONE_MEMBER))
            seen.append((row.members, row.complete))
        self.assertEqual(seen, [(1, False), (2, False), (3, True), (3, True)])

    def test_an_older_state_version_is_rebuilt(self):
        self.archive(synthetic_batches(2))
        refresh(self.test, UNLIMITED)
        GameAnalysis.objects.update(version=STATE_VERSION - 1, state={'totals': {'games': 999}}, games=999)

        row = present(refresh(self.test, UNLIMITED))
        self.assertEqual((row.games, row.members, row.version), (16, 2, STATE_VERSION))

    def test_a_replaced_smaller_archive_is_read_from_the_start(self):
        batches = synthetic_batches(3)
        self.archive(batches)
        refresh(self.test, UNLIMITED)
        GameAnalysis.objects.update(analysed_bytes=1 << 30)

        self.archive(batches[:1])
        row = present(refresh(self.test, UNLIMITED))
        self.assertEqual((row.games, row.members), (8, 1))

    def test_a_replaced_larger_archive_is_read_from_the_start(self):
        self.archive(synthetic_batches(1))
        refresh(self.test, UNLIMITED)
        replacement = synthetic_batches(3, seed=11, compact=False)
        self.archive(replacement)
        cursor = GameAnalysis.objects.get().analysed_bytes
        self.assertFalse(on_member_boundary(archive_path(self.test.id), cursor))

        row = present(refresh(self.test, UNLIMITED))
        self.assertEqual((row.games, row.members, row.complete), (24, 3, True))

    def test_a_corrupt_tail_does_not_restart_the_analysis(self):
        self.archive(synthetic_batches(2))
        first = present(refresh(self.test, UNLIMITED))
        with archive_path(self.test.id).open('r+b') as handle:
            handle.seek(first.analysed_bytes)
            handle.write(os.urandom(1024))

        with mock.patch('OpenBench.games.service.analyse_archive', wraps=analyse_archive) as passes:
            row = present(refresh(self.test, UNLIMITED))
        self.assertEqual(passes.call_count, 1)
        self.assertEqual((row.games, row.members, row.updated), (16, 2, first.updated))

    def test_a_concurrent_pass_is_not_overwritten(self):
        self.archive(synthetic_batches(2))
        refresh(self.test, ONE_MEMBER)
        stale = GameAnalysis.objects.get()
        refresh(self.test, UNLIMITED)

        with (
            mock.patch('OpenBench.games.service.analysis_row', return_value=stale),
            self.assertLogs('OpenBench.games.service', 'INFO') as logs,
        ):
            row = present(refresh(self.test, UNLIMITED))
        self.assertIn('Another pass analysed the games', logs.output[0])
        self.assertEqual((row.games, row.members), (16, 2))

    def test_adjudication_rules_come_from_the_workload(self):
        raw = raw_game('1-0', ['+5.00', '-4.00'] * 3)
        self.archive([('1.1.0.pgn.bz2', upload_batch([raw], 1.0, True))])
        self.assertEqual(present(refresh(self.test, UNLIMITED)).state['terminations'], {'win_adjudication': 1})

        other = create_test(self.author, upload_pgns='COMPACT', win_adj='None', draw_adj='None')
        write_archive(archive_path(other.id), [('2.1.0.pgn.bz2', upload_batch([raw], 1.0, True))])
        self.assertEqual(present(refresh(other, UNLIMITED)).state['terminations'], {'unexplained_win': 1})


class WatcherHookTests(GameAnalysisTestCase):
    def upload(self, book_index: int, content: bytes, test_id: int | None = None) -> None:
        pgn = PGN.objects.create(test_id=test_id or self.test.id, result_id=1, book_index=book_index)
        FileSystemStorage().save(pgn.filename(), ContentFile(content))

    def test_archiving_extends_the_analysis(self):
        watcher = PGNWatcher(threading.Event())
        first, second = synthetic_batches(2)

        self.upload(0, first[1])
        self.assertEqual(watcher.process_pending(), 1)
        self.assertEqual(GameAnalysis.objects.get(test=self.test).games, 8)

        self.upload(1, second[1])
        watcher.process_pending()
        row = GameAnalysis.objects.get(test=self.test)
        self.assertEqual((row.games, row.members, row.complete), (16, 2, True))

    def test_a_failing_analysis_never_blocks_archiving(self):
        self.upload(0, synthetic_batches(1)[0][1])
        with (
            mock.patch('OpenBench.games.service.refresh', side_effect=RuntimeError('boom')),
            self.assertLogs('OpenBench.games.service', 'ERROR') as logs,
        ):
            self.assertEqual(PGNWatcher(threading.Event()).process_pending(), 1)

        self.assertIn('Could not analyse the archived games of Workload', logs.output[0])
        self.assertTrue(archive_path(self.test.id).exists())
        self.assertFalse(PGN.objects.filter(processed=False).exists())
        self.assertFalse(GameAnalysis.objects.exists())

    def test_unreadable_uploads_are_archived_and_flagged(self):
        self.upload(0, b'pgn')
        self.assertEqual(PGNWatcher(threading.Event()).process_pending(), 1)
        row = GameAnalysis.objects.get(test=self.test)
        self.assertEqual((row.games, row.members, row.state['totals']['damaged_members']), (0, 1, 1))

    def test_batches_of_an_unknown_workload_are_archived_without_analysis(self):
        self.upload(0, synthetic_batches(1)[0][1], test_id=987)
        self.assertEqual(PGNWatcher(threading.Event()).process_pending(), 1)
        self.assertTrue(archive_path(987).exists())
        self.assertFalse(GameAnalysis.objects.exists())

    def test_hook_swallows_database_errors(self):
        with (
            mock.patch('OpenBench.games.service.Test.objects.filter', side_effect=RuntimeError('locked')),
            self.assertLogs('OpenBench.games.service', 'ERROR'),
        ):
            refresh_after_archiving(self.test.id)


class GamesApiTests(GameAnalysisTestCase):
    def url(self, test=None) -> str:
        return f'/api/workload/{(test or self.test).id}/games/'

    def games(self, test=None):
        return self.client.get(self.url(test)).json()['games']

    def test_anonymous_is_rejected(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {'error': 'API requires authentication for this server'})

    def test_credentials_in_post_are_accepted(self):
        response = self.client.post(self.url(), credentials(self.author))
        self.assertEqual(response.json()['games']['status'], 'empty')

    def test_unknown_workload(self):
        self.client.force_login(self.author)
        response = self.client.get('/api/workload/999/games/')
        self.assertEqual(
            (response.status_code, response.json()), (404, {'error': 'Requested Workload Id does not exist'})
        )

    def test_workloads_without_uploads_are_disabled_and_cost_no_extra_query(self):
        self.client.force_login(self.author)
        plain = create_test(self.author)
        write_archive(archive_path(plain.id), synthetic_batches(1))

        with self.assertNumQueries(4):
            games = self.games(plain)
        self.assertEqual(games, {'status': 'disabled', 'upload_pgns': 'FALSE', 'active': True, 'report': None})
        self.assertFalse(GameAnalysis.objects.exists())

    def test_empty_until_a_batch_is_archived(self):
        self.client.force_login(self.author)
        self.assertEqual(self.games(), {'status': 'empty', 'upload_pgns': 'COMPACT', 'active': True, 'report': None})

    def test_first_view_analyses_lazily_within_its_budget(self):
        self.client.force_login(self.author)
        self.archive(synthetic_batches(3))

        with mock.patch('OpenBench.games.views.VIEW_BUDGET', ONE_MEMBER):
            limits = [self.games()['report']['limits'] for _ in range(4)]

        self.assertEqual([entry['members'] for entry in limits], [1, 2, 3, 3])
        self.assertEqual([entry['complete'] for entry in limits], [False, False, True, True])
        self.assertLess(limits[0]['analysed_bytes'], limits[0]['archive_bytes'])

    def test_ready_shape(self):
        self.client.force_login(self.author)
        self.archive(synthetic_batches(3, compact=False))
        games = self.games()
        report = games['report']

        self.assertEqual((games['status'], games['upload_pgns'], games['active']), ('ready', 'COMPACT', True))
        self.assertEqual(
            sorted(report),
            ['colour', 'evals', 'games', 'lengths', 'limits', 'openings', 'pairs', 'terminations', 'updated_at'],
        )
        self.assertEqual(report['games'], 24)
        self.assertEqual(report['pairs']['total'], 12)
        self.assertEqual(sum(report['pairs']['pentanomial']), 12)
        self.assertEqual(
            sorted(report['limits']),
            [
                'analysed_bytes',
                'archive_bytes',
                'complete',
                'damaged_members',
                'malformed_games',
                'members',
                'unfinished_games',
                'unpaired_games',
                'untracked_opening_pairs',
            ],
        )
        self.assertEqual(sum(row['games'] for row in report['terminations']['rows']), 24)
        self.assertAlmostEqual(sum(row['share'] for row in report['terminations']['rows']), 1.0)
        self.assertEqual(report['lengths']['all']['games'], 24)
        self.assertEqual(sum(row['decisive'] + row['drawn'] for row in report['lengths']['histogram']), 24)
        self.assertTrue(report['evals']['has_timing'])
        self.assertEqual(len(report['evals']['advantage']), 6)
        self.assertEqual([row['key'] for row in report['evals']['phases']], ['early', 'middle', 'late'])
        self.assertEqual(
            sorted(report['openings']), ['always_drawn', 'colour_bound', 'drawn', 'lopsided', 'repeated', 'tracked']
        )

    def test_a_settled_analysis_is_served_with_a_constant_number_of_queries(self):
        self.client.force_login(self.author)
        for batches in (1, 6):
            self.archive(synthetic_batches(batches))
            self.games()
            with self.assertNumQueries(5):
                self.assertEqual(self.games()['report']['limits']['members'], batches)

    def test_a_removed_archive_keeps_its_analysis(self):
        self.client.force_login(self.author)
        self.archive(synthetic_batches(2))
        before = self.games()['report']
        archive_path(self.test.id).unlink()

        after = self.games()['report']
        self.assertEqual(after['games'], 16)
        self.assertEqual(after['pairs'], before['pairs'])
        self.assertEqual(after['limits']['archive_bytes'], after['limits']['analysed_bytes'])

    def test_finished_workloads_are_not_active(self):
        self.client.force_login(self.author)
        type(self.test).objects.filter(id=self.test.id).update(finished=True)
        self.assertFalse(self.games()['active'])


class GamesPageTests(GameAnalysisTestCase):
    HINT = 'Per-game insights need PGN uploads (set Upload PGNs to COMPACT when creating the test)'

    def page(self, test, kind='test') -> str:
        self.client.force_login(self.author)
        return self.client.get(f'/{kind}/{test.id}/').content.decode()

    def test_without_uploads_only_the_hint_is_rendered(self):
        content = self.page(create_test(self.author))
        self.assertEqual(content.count(self.HINT), 1)
        self.assertNotIn('data-games-insights', content)
        self.assertNotIn('games.js', content)

    def test_with_uploads_the_container_and_script_are_rendered(self):
        content = self.page(self.test)
        self.assertNotIn(self.HINT, content)
        self.assertIn(f'data-games-insights data-workload-id="{self.test.id}" hidden', content)
        self.assertIn('games.js?', content)

    def test_the_hint_is_for_tests_only(self):
        self.assertNotIn(self.HINT, self.page(create_test(self.author, test_mode='DATAGEN'), 'datagen'))

    def test_page_queries_do_not_depend_on_uploads_or_archives(self):
        plain = create_test(self.author)
        self.archive(synthetic_batches(2))
        refresh(self.test, VIEW_BUDGET)
        self.client.force_login(self.author)
        self.client.get(f'/test/{plain.id}/')

        for test in (plain, self.test):
            with self.assertNumQueries(17):
                self.assertEqual(self.client.get(f'/test/{test.id}/').status_code, 200)
