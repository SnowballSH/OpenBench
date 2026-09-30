from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

type Trinomial   = tuple[int, int, int]
type Pentanomial = tuple[int, int, int, int, int]

class WorkloadMode(StrEnum):
    SPRT    = 'SPRT'
    GAMES   = 'GAMES'
    SPSA    = 'SPSA'
    DATAGEN = 'DATAGEN'

class WorkloadStatus(StrEnum):
    PENDING   = 'pending'
    ACTIVE    = 'active'
    PASSED    = 'passed'
    FAILED    = 'failed'
    COMPLETED = 'completed'
    STOPPED   = 'stopped'
    DELETED   = 'deleted'

@dataclass(frozen=True, slots=True)
class Outcomes:
    trinomial   : Trinomial
    pentanomial : Pentanomial
    use_penta   : bool

    @property
    def games(self) -> int:
        return sum(self.trinomial)

    @property
    def pairs(self) -> int:
        return sum(self.pentanomial)

    @property
    def draws(self) -> int:
        return self.trinomial[1]

    def primary(self) -> tuple[int, ...]:
        return self.pentanomial if self.use_penta else self.trinomial

@dataclass(frozen=True, slots=True)
class ProgressPoint:
    timestamp : datetime
    games     : int
    outcomes  : Outcomes
    llr       : float

def spsa_target_games(pairs_per: int, iterations: int) -> int:
    return 2 * pairs_per * iterations

def tune_completed(mode: WorkloadMode, games: int, target: int | None) -> bool:
    return mode == WorkloadMode.SPSA and target is not None and target > 0 and games >= target

@dataclass(frozen=True, slots=True)
class SprtBounds:
    elo0      : float
    elo1      : float
    lower_llr : float
    upper_llr : float

@dataclass(frozen=True, slots=True)
class WorkloadFacts:
    id           : int
    mode         : WorkloadMode
    status       : WorkloadStatus
    created_at   : datetime
    updated_at   : datetime
    finished     : bool
    outcomes     : Outcomes
    llr          : float
    sprt         : SprtBounds | None
    target_games : int | None
