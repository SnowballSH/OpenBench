import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

RESULTS = frozenset({'1-0', '0-1', '1/2-1/2', '*'})
REQUIRED_HEADERS = ('White', 'Black', 'Result')
MATE_CP = 100_000
MAX_GAME_CHARS = 1 << 20

HEADER = re.compile(r'^\[(\w+)\s+"(.*)"\]$')
TOKEN = re.compile(r'\{([^}]*)\}|([^\s{}]+)')
MOVE_NUMBER = re.compile(r'^\d+\.+')
MOVE = re.compile(
    r'^(?:[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=?[QRBN])?|O-O(?:-O)?|0-0(?:-0)?|[a-h][1-8][a-h][1-8][qrbn]?|--|0000)'
    r'[+#]?[!?]*$'
)
COMMENT = re.compile(
    r'\s*(?:([+-]?)(M?)(\d+(?:\.\d+)?)/(\d+))?'
    r'(?:\s*(\d+(?:\.\d+)?)s\b)?'
    r'(?:,?\s*n=(\d+))?'
    r'(?:,?\s*sd=(\d+))?'
)
ENGINE_LINE = re.compile(r',?\s*line=')
BOOK_COMMENT = 'book'


@dataclass(frozen=True, slots=True)
class Move:
    san: str
    book: bool = False
    score_cp: int | None = None
    depth: int | None = None
    time_ms: int | None = None
    nodes: int | None = None
    seldepth: int | None = None

    @property
    def mates(self) -> bool:
        return self.san.rstrip('!?').endswith('#')


@dataclass(frozen=True, slots=True)
class Game:
    headers: dict[str, str]
    moves: tuple[Move, ...]
    result: str
    final_comment: str


def score_cp(sign: str, mate: str, magnitude: str) -> int:
    direction = -1 if sign == '-' else 1
    if mate:
        return direction * MATE_CP
    return direction * min(MATE_CP - 1, round(100 * float(magnitude)))


def annotated(san: str, comment: str) -> Move:

    if comment.strip() == BOOK_COMMENT:
        return Move(san, book=True)

    fields = COMMENT.match(comment)
    if fields is None or fields.end() == 0:
        return Move(san)

    sign, mate, magnitude, depth, seconds, nodes, seldepth = fields.groups()
    return Move(
        san,
        score_cp=score_cp(sign, mate, magnitude) if magnitude else None,
        depth=int(depth) if depth else None,
        time_ms=round(1000 * float(seconds)) if seconds else None,
        nodes=int(nodes) if nodes else None,
        seldepth=int(seldepth) if seldepth else None,
    )


def without_engine_line(comment: str) -> str:
    return ENGINE_LINE.split(comment, maxsplit=1)[0].strip()


def parse_movetext(text: str) -> tuple[tuple[Move, ...], str, str] | None:

    moves: list[Move] = []
    bare: str | None = None
    result: str | None = None
    last_comment = ''

    for comment, word in TOKEN.findall(text):
        if result is not None:
            return None
        if not word:
            if bare is not None:
                moves.append(annotated(bare, comment))
                bare = None
            last_comment = comment
            continue
        if bare is not None:
            moves.append(Move(bare))
            bare = None
        if word in RESULTS:
            result = word
        elif san := MOVE_NUMBER.sub('', word):
            if not MOVE.match(san):
                return None
            bare = san

    return None if result is None else (tuple(moves), result, without_engine_line(last_comment))


def parse_game(header_lines: list[str], move_lines: list[str]) -> Game | None:

    headers: dict[str, str] = {}
    for line in header_lines:
        if not (match := HEADER.match(line)):
            return None
        headers[match[1]] = match[2]

    if any(not headers.get(name) for name in REQUIRED_HEADERS) or headers['Result'] not in RESULTS:
        return None

    if (parsed := parse_movetext(' '.join(move_lines))) is None or parsed[1] != headers['Result']:
        return None

    return Game(headers, *parsed)


def parse_games(lines: Iterable[str]) -> Iterator[Game | None]:
    """Yields each game of the Client's upload dialect, or None for a block that is not one."""

    header_lines: list[str] = []
    move_lines: list[str] = []
    chars = 0
    oversized = False

    def flush() -> Iterator[Game | None]:
        nonlocal chars, oversized
        if header_lines or move_lines:
            yield None if oversized else parse_game(header_lines, move_lines)
        header_lines.clear()
        move_lines.clear()
        chars, oversized = 0, False

    for raw in lines:
        line = raw.strip()
        if not line:
            if move_lines:
                yield from flush()
            continue

        if line.startswith('[') and move_lines:
            yield from flush()

        chars += len(line)
        if chars > MAX_GAME_CHARS:
            oversized = True
        elif line.startswith('[') and not move_lines:
            header_lines.append(line)
        else:
            move_lines.append(line)

    yield from flush()
