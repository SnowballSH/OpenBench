from dataclasses import asdict, dataclass

from OpenBench.navigation.query import (
    MACHINE_ID,
    MAX_QUERY_LENGTH,
    USER_PREFIX,
    USERNAME,
    WORKLOAD_ID,
    machine_path,
    normalise,
    search_path,
    user_path,
)
from OpenBench.navigation.resolve import Catalogue, WorkloadRef
from OpenBench.progress.domain import DEFAULT_WINDOW
from OpenBench.progress.present import progress_url

MAX_SUGGESTIONS = 8
MIN_TEXT_LENGTH = 2


@dataclass(frozen=True, slots=True)
class Suggestion:
    label: str
    detail: str
    url: str


def workload_suggestion(workload: WorkloadRef) -> Suggestion:
    return Suggestion(label=f'#{workload.id} {workload.title}', detail=workload.detail, url=workload.path)


def direct_hits(text: str, catalogue: Catalogue) -> list[Suggestion]:
    hits: list[Suggestion] = []

    workload = catalogue.workload(int(match[2])) if (match := WORKLOAD_ID.fullmatch(text)) else None
    if workload and not workload.deleted:
        hits.append(workload_suggestion(workload))

    name = match[1] if (match := USER_PREFIX.fullmatch(text)) else text
    if USERNAME.fullmatch(name) and (username := catalogue.username(name)):
        hits.append(Suggestion(label=username, detail='User', url=user_path(username)))

    if engine := catalogue.engine(text):
        hits.append(Suggestion(label=engine, detail='Engine progress', url=progress_url(engine, DEFAULT_WINDOW)))

    if (match := MACHINE_ID.fullmatch(text)) and catalogue.machine_exists(machine_id := int(match[1])):
        hits.append(Suggestion(label=f'Machine {machine_id}', detail='Machine', url=machine_path(machine_id)))

    return hits


def suggestions(raw: str, catalogue: Catalogue) -> list[Suggestion]:
    text = normalise(raw)
    if not text or len(text) > MAX_QUERY_LENGTH:
        return []

    found = direct_hits(text, catalogue)
    if len(text) >= MIN_TEXT_LENGTH:
        room = MAX_SUGGESTIONS - len(found) - 1
        found += [workload_suggestion(workload) for workload in catalogue.text_workloads(text, room)]

    found.append(Suggestion(label=f'Search for “{text}”', detail='Search', url=search_path(text)))
    unique: dict[str, Suggestion] = {}
    for suggestion in found:
        unique.setdefault(suggestion.url, suggestion)
    return list(unique.values())


def to_json(found: list[Suggestion]) -> list[dict[str, str]]:
    return [asdict(suggestion) for suggestion in found]
