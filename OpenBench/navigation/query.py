import re
from urllib.parse import quote, urlencode

MAX_QUERY_LENGTH = 100
MIN_COMMIT_PREFIX = 7

WORKLOAD_ID = re.compile(r'(#?)([0-9]{1,18})')
COMMIT_PREFIX = re.compile(rf'[0-9a-f]{{{MIN_COMMIT_PREFIX},40}}', re.IGNORECASE)
MACHINE_ID = re.compile(r'(?:machine[ :#]*|m[:#])([0-9]{1,18})', re.IGNORECASE)
USER_PREFIX = re.compile(r'user:\s*(\S+)', re.IGNORECASE)
USERNAME = re.compile(r'(?!\.+$)[\w.@+-]{1,150}', re.ASCII)

SEARCH_PATH = '/search/'
USERS_PATH = '/users/'
MACHINES_PATH = '/machines/'


def normalise(raw: str) -> str:
    return ' '.join(raw.split())


def is_commit_prefix(text: str) -> bool:
    return COMMIT_PREFIX.fullmatch(text) is not None


def search_path(text: str) -> str:
    return f'{SEARCH_PATH}?{urlencode({"q": text})}'


def user_path(username: str) -> str:
    return f'/user/{quote(username, safe="")}/'


def machine_path(machine_id: int) -> str:
    return f'{MACHINES_PATH}{machine_id}/'
