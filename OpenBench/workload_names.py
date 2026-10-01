import re

from OpenBench.models import Test

SHORT_SHA_LENGTH = 8

# A full 40-digit SHA, as prettyName recognises it, or an abbreviation of seven or more hex digits with
# at least one decimal digit, so "deadbeef" and "defaced" stay branch names
COMMIT_NAME = re.compile(r'[0-9a-f]{40}|(?=[a-f]*[0-9])[0-9a-f]{7,39}', re.IGNORECASE)


def is_commit_name(name: str) -> bool:
    return COMMIT_NAME.fullmatch(name) is not None


def short_name(name: str) -> str:
    return name[:SHORT_SHA_LENGTH].lower() if is_commit_name(name) else name


def split_info(info: str) -> tuple[str, str]:
    subject, _, rest = info.strip().partition('\n')
    return subject.strip(), rest.strip()


def commit_pair(test: Test) -> str:
    dev, base = test.dev.name, test.base.name
    return short_name(dev) if dev == base else f'{short_name(dev)} vs {short_name(base)}'
