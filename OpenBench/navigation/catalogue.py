from django.contrib.auth.models import User
from django.db.models import Q, QuerySet

from OpenBench.listing_rows import COMMIT_NAME, commit_pair, split_info
from OpenBench.models import EngineConfig, Machine, Test
from OpenBench.navigation.resolve import WorkloadRef

NAME_FIELDS = ('dev__name', 'base__name')
COMMIT_NAME_PATTERN = f'^(?:{COMMIT_NAME.pattern})$'
COMMIT_FIELDS = ('dev__sha__istartswith', 'base__sha__istartswith')
COMMIT_NAME_FIELDS = ('dev__name__istartswith', 'base__name__istartswith')


def any_field(fields: tuple[str, ...], value: str) -> Q:
    query = Q()
    for field in fields:
        query |= Q(**{field: value})
    return query


def branch_name_contains(term: str) -> Q:
    # A commit-pinned name is forty hex digits, where any short term would match by accident
    query = Q()
    for field in NAME_FIELDS:
        query |= Q(**{f'{field}__icontains': term}) & ~Q(**{f'{field}__iregex': COMMIT_NAME_PATTERN})
    return query


def text_filter(text: str) -> Q:
    query = Q()
    for term in text.split():
        query &= Q(info__icontains=term) | any_field(COMMIT_FIELDS, term) | branch_name_contains(term)
    return query


def commit_filter(prefix: str) -> Q:
    return any_field(COMMIT_FIELDS + COMMIT_NAME_FIELDS, prefix)


def workload_ref(test: Test) -> WorkloadRef:
    subject, _ = split_info(test.info)
    commits = commit_pair(test)
    detail = f'{commits} · {test.dev_time_control}' if subject else test.dev_time_control
    return WorkloadRef(
        id=test.id, kind=test.workload_type_str(), title=subject or commits, detail=detail, deleted=test.deleted
    )


def labelled_tests() -> QuerySet[Test]:
    fields = ('id', 'test_mode', 'info', 'dev_time_control', 'deleted', 'dev__name', 'base__name')
    return Test.objects.select_related('dev', 'base').only(*fields)


def newest_listed(matching: Q, limit: int) -> list[WorkloadRef]:
    tests = labelled_tests().filter(matching).exclude(deleted=True).order_by('-id')
    return [workload_ref(test) for test in tests[:limit]]


class DatabaseCatalogue:
    def workload(self, workload_id: int) -> WorkloadRef | None:
        test = labelled_tests().filter(id=workload_id).first()
        return workload_ref(test) if test else None

    def commit_workloads(self, prefix: str, limit: int) -> list[WorkloadRef]:
        return newest_listed(commit_filter(prefix), limit)

    def text_workloads(self, text: str, limit: int) -> list[WorkloadRef]:
        return newest_listed(text_filter(text), limit)

    def username(self, name: str) -> str | None:
        return User.objects.filter(username__iexact=name).values_list('username', flat=True).first()

    def engine(self, name: str) -> str | None:
        return EngineConfig.objects.filter(name__iexact=name).values_list('name', flat=True).first()

    def machine_exists(self, machine_id: int) -> bool:
        return Machine.objects.filter(id=machine_id).exists()
