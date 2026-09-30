import json
from collections.abc import Mapping
from typing import Any

type SystemInfo = dict[str, Any]

REQUIRED_TYPES: Mapping[str, type] = {
    'concurrency': int,
    'physical_cores': int,
    'logical_cores': int,
    'ram_total_mb': int,
    'sockets': int,
    'syzygy_max': int,
    'noisy': bool,
    'cpu_flags': list,
    'os_name': str,
    'compilers': dict,
    'tokens': dict,
}

OPTIONAL_TYPES: Mapping[str, type] = {
    'cpu_name': str,
    'isa_name': str,
    'os_ver': str,
    'machine_name': str,
    'focus': list,
    'only': list,
}

STRING_LISTS = frozenset({'cpu_flags', 'focus', 'only'})

NULLABLE = frozenset({'physical_cores', 'logical_cores'})


def decode_system_info(raw: str | None) -> SystemInfo | None:
    try:
        info = json.loads(raw or '')
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def well_typed(info: SystemInfo, key: str, kind: type) -> bool:
    # Exact types, as JSON decodes them: a bool is never accepted as an int
    value = info[key]
    if value is None and key in NULLABLE:
        return True
    if type(value) is not kind:
        return False
    return key not in STRING_LISTS or (isinstance(value, list) and all(type(item) is str for item in value))


def malformed_fields(info: SystemInfo) -> list[str]:
    missing = [key for key in REQUIRED_TYPES if key not in info]
    present = {**REQUIRED_TYPES, **{key: kind for key, kind in OPTIONAL_TYPES.items() if key in info}}
    return missing + [key for key, kind in present.items() if key in info and not well_typed(info, key, kind)]


def known_text(value: object) -> str | None:
    return str(value) if value not in (None, '', 'None') else None


def text_of(info: object, key: str) -> str | None:
    return known_text(info.get(key)) if isinstance(info, dict) else None


def int_of(info: object, key: str) -> int:
    if not isinstance(info, dict):
        return 0
    try:
        return int(info.get(key) or 0)
    except TypeError, ValueError, OverflowError:
        return 0
