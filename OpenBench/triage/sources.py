from OpenBench.models import LogEvent


def worker_error_count(test_id: int) -> int:
    return LogEvent.objects.filter(test_id=test_id, machine_id__gt=0).count()
