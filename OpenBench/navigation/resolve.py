from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from OpenBench.navigation.query import (
    MACHINE_ID,
    MACHINES_PATH,
    MAX_QUERY_LENGTH,
    SEARCH_PATH,
    USER_PREFIX,
    USERNAME,
    USERS_PATH,
    WORKLOAD_ID,
    is_commit_prefix,
    machine_path,
    normalise,
    search_path,
    user_path,
)
from OpenBench.progress.domain import DEFAULT_WINDOW
from OpenBench.progress.present import progress_url

TOO_LONG = f'Jump to at most {MAX_QUERY_LENGTH} characters'
AMBIGUOUS_LIMIT = 2


@dataclass(frozen=True, slots=True)
class WorkloadRef:
    id: int
    kind: str
    title: str
    detail: str

    @property
    def path(self) -> str:
        return f'/{self.kind}/{self.id}/'


@dataclass(frozen=True, slots=True)
class Jump:
    path: str
    notice: str | None = None


class Catalogue(Protocol):
    def workload(self, workload_id: int) -> WorkloadRef | None: ...

    def commit_workloads(self, prefix: str, limit: int) -> Sequence[WorkloadRef]: ...

    def text_workloads(self, text: str, limit: int) -> Sequence[WorkloadRef]: ...

    def username(self, name: str) -> str | None: ...

    def engine(self, name: str) -> str | None: ...

    def machine_exists(self, machine_id: int) -> bool: ...


type Rule = Callable[[str, Catalogue], Jump | None]


def workload_rule(text: str, catalogue: Catalogue) -> Jump | None:
    if (match := WORKLOAD_ID.fullmatch(text)) is None:
        return None

    workload_id = int(match[2])
    if (found := catalogue.workload(workload_id)) is not None:
        return Jump(found.path)

    # A bare run of seven or more digits may still abbreviate a commit
    if not match[1] and is_commit_prefix(text):
        return None

    return Jump(SEARCH_PATH, notice=f'No workload #{workload_id}')


def commit_rule(text: str, catalogue: Catalogue) -> Jump | None:
    if not is_commit_prefix(text):
        return None

    found = catalogue.commit_workloads(text.lower(), AMBIGUOUS_LIMIT)
    if not found:
        return None

    return Jump(found[0].path) if len(found) == 1 else Jump(search_path(text))


def user_rule(text: str, catalogue: Catalogue) -> Jump | None:
    if (match := USER_PREFIX.fullmatch(text)) is not None:
        name = match[1]
        known = catalogue.username(name) if USERNAME.fullmatch(name) else None
        return Jump(user_path(known)) if known else Jump(USERS_PATH, notice=f'No user named {name}')

    known = catalogue.username(text) if USERNAME.fullmatch(text) else None
    return Jump(user_path(known)) if known else None


def engine_rule(text: str, catalogue: Catalogue) -> Jump | None:
    known = catalogue.engine(text)
    return Jump(progress_url(known, DEFAULT_WINDOW)) if known else None


def machine_rule(text: str, catalogue: Catalogue) -> Jump | None:
    if (match := MACHINE_ID.fullmatch(text)) is None:
        return None

    machine_id = int(match[1])
    if catalogue.machine_exists(machine_id):
        return Jump(machine_path(machine_id))

    return Jump(MACHINES_PATH, notice=f'No machine {machine_id}')


RULES: tuple[Rule, ...] = (workload_rule, commit_rule, user_rule, engine_rule, machine_rule)


def resolve(raw: str, catalogue: Catalogue) -> Jump:
    text = normalise(raw)
    if not text:
        return Jump(SEARCH_PATH)

    if len(text) > MAX_QUERY_LENGTH:
        return Jump(SEARCH_PATH, notice=TOO_LONG)

    for rule in RULES:
        if (jump := rule(text, catalogue)) is not None:
            return jump

    return Jump(search_path(text))
