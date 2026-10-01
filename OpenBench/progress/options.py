import re

THREADS_OPTION = re.compile(r'(?:^|\s)Threads=(\d+)(?=\s|$)')


def thread_count(options: str) -> int | None:
    found = THREADS_OPTION.search(options)
    return int(found.group(1)) if found else None
