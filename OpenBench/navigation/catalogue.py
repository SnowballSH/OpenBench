from django.contrib.auth.models import User
from django.db.models import Q, QuerySet

from OpenBench.listing_rows import commit_pair, split_info
from OpenBench.models import EngineConfig, Machine, Test
from OpenBench.navigation.resolve import WorkloadRef

TEXT_FIELDS = ('info__icontains', 'dev__name__icontains', 'base__name__icontains')
COMMIT_FIELDS = ('dev__sha__istartswith', 'base__sha__istartswith')
COMMIT_NAME_FIELDS = ('dev__name__istartswith', 'base__name__istartswith')


def any_field(fields: tuple[str, ...], value: str) -> Q:
    query = Q()
    for field in fields:
        query |= Q(**{field: value})
    return query


def text_filter(text: str) -> Q:
    query = Q()
    for term in text.split():
        query &= any_field(TEXT_FIELDS + COMMIT_FIELDS, term)
    return query


def commit_filter(prefix: str) -> Q:
    return any_field(COMMIT_FIELDS + COMMIT_NAME_FIELDS, prefix)


def workload_ref(test: Test) -> WorkloadRef:
    subject, _ = split_info(test.info)
    commits = commit_pair(test)
    detail = f'{commits} · {test.dev_time_control}' if subject else test.dev_time_control
    return WorkloadRef(id=test.id, kind=test.workload_type_str(), title=subject or commits, detail=detail)


def labelled_tests() -> QuerySet[Test]:
    fields = ('id', 'test_mode', 'info', 'dev_time_control', 'dev__name', 'base__name')
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
