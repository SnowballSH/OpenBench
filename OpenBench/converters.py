from django.urls import register_converter


class IdConverter:
    # Eighteen digits stay below SQLite's 64-bit integer limit
    regex = r'\d{1,18}'

    def to_python(self, value: str) -> int:
        return int(value)

    def to_url(self, value: int) -> str:
        return str(value)


register_converter(IdConverter, 'id')
