from datetime import datetime
from pathlib import Path

from django.conf import settings

from OpenBench.models import LogEvent, Test
from OpenBench.triage.logs import log_name

BUILD_LOG = """\
zig build -Doptimize=ReleaseFast -Dcpu=native --prefix /tmp/openbench-build
install
+- install Avalanche
   +- compile exe Avalanche ReleaseFast native 2 errors
src/nnue/accumulator.zig:214:38: error: expected type '@Vector(32, i16)', found '@Vector(16, i16)'
        const clipped = @min(@max(acc[i], zero), qa);
                                  ~~~^~~
src/nnue/accumulator.zig:231:20: error: use of undeclared identifier 'maddubs512'
            sum += maddubs512(clipped, weights[i]);
                   ^~~~~~~~~~
referenced by:
    refresh: src/nnue/accumulator.zig:188:25
    evaluate: src/eval.zig:41:32
    search: src/search.zig:512:27
error: the following command failed with 2 compilation errors:
/usr/local/zig/zig build-exe -OReleaseFast -mcpu native -Mroot=/tmp/openbench-build/src/main.zig --name Avalanche
Build Summary: 0/3 steps succeeded; 1 failed
install transitive failure
+- install Avalanche transitive failure
   +- compile exe Avalanche ReleaseFast native 2 errors
error: the following build command failed with exit code 1:
/tmp/openbench-build/.zig-cache/o/5d1c0e1b7a36/build /usr/local/zig/zig /tmp/openbench-build install
make: *** [Makefile:14: all] Error 1
"""

CRASH_PGN = """\
[Event "Fastchess Tournament"]
[Site "?"]
[Date "2026.09.30"]
[Round "118"]
[White "Avalanche-dev"]
[Black "Avalanche-base"]
[Result "0-1"]
[FEN "r1bq1rk1/pp2bppp/2n1pn2/2pp4/3P1B2/2P1PN2/PP1NBPPP/R2QK2R w KQ - 0 8"]
[GameDuration "00:00:14"]
[PlyCount "47"]
[Termination "abandoned"]
[TimeControl "8+0.08"]

8. O-O {+0.31/18 0.41s} b6 {-0.22/17 0.38s} 9. Ne5 {+0.35/17 0.36s} Bb7 {-0.28/18 0.40s}
10. Qa4 {+0.44/16 0.33s} Nxe5 {-0.40/17 0.35s} 11. Bxe5 {+0.38/18 0.37s} a6 {-0.31/17 0.31s}
12. dxc5 {+0.52/17 0.29s} bxc5 {-0.47/18 0.34s} 13. Rfd1 {+0.49/16 0.30s} Qb6 {-0.44/17 0.28s}
14. Nb3 {White disconnects} 0-1
"""

ILLEGAL_PGN = """\
[Event "Fastchess Tournament"]
[Site "?"]
[Date "2026.09.30"]
[Round "342"]
[White "Avalanche-base"]
[Black "Avalanche-dev"]
[Result "1-0"]
[FEN "8/5pk1/6p1/3pP2p/3P1P1P/6P1/5K2/8 b - - 0 41"]
[PlyCount "9"]
[Termination "illegal move"]
[TimeControl "8+0.08"]

41... Kf8 {-0.02/31 0.22s} 42. Ke3 {+0.00/33 0.19s} Ke7 {-0.01/30 0.18s}
43. Kd3 {+0.00/34 0.17s} Kd7 {+0.00/32 0.16s} 44. Kc3 {+0.00/35 0.15s}
Kc6 {+0.00/31 0.14s} 45. Kb4 {+0.00/36 0.15s} Kb6e7 {Black makes an illegal move: b6e7} 1-0
"""


def build_failure_summary(test: Test) -> str:
    return f'[{test.dev_engine}] {test.dev.name} build failed'


def wrong_bench_summary(test: Test, reported: int) -> str:
    return f'[{test.dev_engine}-{test.dev.sha.upper()[:8]}] Wrong Bench: {reported}'


def record_error(test: Test, machine_id: int, author: str, summary: str, at: datetime, log: str | None) -> LogEvent:

    event = LogEvent.objects.create(author=author, summary=summary, log_file='', machine_id=machine_id, test_id=test.id)
    LogEvent.objects.filter(id=event.id).update(created=at, log_file=log_name(event.id) if log else '')

    if log:
        media = Path(settings.MEDIA_ROOT)
        media.mkdir(parents=True, exist_ok=True)
        (media / log_name(event.id)).write_text(log, encoding='utf-8')

    event.refresh_from_db()
    return event
