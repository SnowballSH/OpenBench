import csv
import io
import math
from collections.abc import Sequence
from datetime import UTC, datetime

from OpenBench.insights.series import SeriesPoint

HISTORY_COLUMNS = ('timestamp', 'games', 'llr', 'elo', 'elo_lower', 'elo_upper')

type Cell = datetime | int | float | None


def cell_text(value: object) -> str:
    # Every cell is a number or an ISO timestamp, so none can start a spreadsheet formula
    match value:
        case None:
            return ''
        case datetime():
            return value.astimezone(UTC).isoformat()
        case bool():
            raise TypeError(f'Refusing to export a boolean cell: {value!r}')
        case int():
            return str(value)
        case float():
            return repr(value) if math.isfinite(value) else ''
    raise TypeError(f'Refusing to export a non-numeric cell: {value!r}')


def history_row(point: SeriesPoint) -> list[str]:
    cells: tuple[Cell, ...] = (point.timestamp, point.games, point.llr, point.elo, point.elo_lower, point.elo_upper)
    return [cell_text(cell) for cell in cells]


def history_csv(points: Sequence[SeriesPoint]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow(HISTORY_COLUMNS)
    writer.writerows(history_row(point) for point in points)
    return buffer.getvalue()


def history_filename(workload_id: int) -> str:
    return f'workload-{workload_id}-history.csv'
