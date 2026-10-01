from dataclasses import fields

from OpenBench.digest.domain import DigestReport, ErrorDigest
from OpenBench.insights.serialize import Json, to_json
from OpenBench.triage.serialize import group_json


def errors_json(errors: ErrorDigest) -> dict[str, Json]:
    return {
        'total': errors.total,
        'new': errors.new,
        'unresolved': errors.unresolved,
        'omitted': errors.omitted,
        'truncated': errors.truncated,
        'groups': [{**group_json(line.row), 'new': line.new} for line in errors.lines],
    }


def digest_json(report: DigestReport) -> dict[str, Json]:
    plain = {field.name: to_json(getattr(report, field.name)) for field in fields(report) if field.name != 'errors'}
    return {**plain, 'errors': errors_json(report.errors)}
