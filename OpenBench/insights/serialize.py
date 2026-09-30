import math
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]

def to_json(value: object) -> Json:

    match value:
        case Enum():
            return to_json(value.value)
        case None | bool() | int() | str():
            return value
        case float():
            return value if math.isfinite(value) else None
        case datetime() | date():
            return value.isoformat()
        case list() | tuple():
            return [to_json(item) for item in value]
        case dict():
            return { str(key) : to_json(item) for key, item in value.items() }
        case _ if is_dataclass(value) and not isinstance(value, type):
            return { field.name : to_json(getattr(value, field.name)) for field in fields(value) }

    raise TypeError('Cannot serialize %r' % (value,))
