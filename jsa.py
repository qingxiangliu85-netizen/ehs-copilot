"""Small, session-only helpers for JSA risk assessment."""

from __future__ import annotations

import csv
from io import StringIO
from typing import Iterable, Mapping


RISK_LEVELS = (
    (4, "低风险"),
    (9, "中风险"),
    (16, "高风险"),
    (25, "重大风险"),
)


def calculate_risk(likelihood: int, severity: int) -> tuple[int, str]:
    """Return R=L×S and the configured risk level for 1–5 inputs."""
    if likelihood not in range(1, 6) or severity not in range(1, 6):
        raise ValueError("可能性 L 和严重度 S 必须为 1–5 的整数。")

    score = likelihood * severity
    for upper_bound, level in RISK_LEVELS:
        if score <= upper_bound:
            return score, level
    raise ValueError("风险值超出允许范围。")


def calculate_jsa_record_risks(
    record: Mapping[str, object],
) -> dict[str, int | str]:
    """Recalculate initial and residual risk from a JSA record's L/S inputs."""
    initial_score, initial_level = calculate_risk(
        int(record["可能性L"]), int(record["严重度S"])
    )
    residual_score, residual_level = calculate_risk(
        int(record["控制后可能性L"]), int(record["控制后严重度S"])
    )
    return {
        "风险值R": initial_score,
        "风险等级": initial_level,
        "残余风险R": residual_score,
        "残余风险等级": residual_level,
    }


def create_jsa_record(
    *,
    job_name: str,
    job_step: str,
    hazard: str,
    consequence: str,
    likelihood: int,
    severity: int,
    existing_controls: str,
    suggested_controls: str,
    residual_likelihood: int,
    residual_severity: int,
) -> dict[str, object]:
    """Create one consistent JSA record using the shared risk calculation."""
    record: dict[str, object] = {
        "作业名称": job_name.strip(),
        "作业步骤": job_step.strip(),
        "危害因素": hazard.strip(),
        "可能后果": consequence.strip(),
        "可能性L": likelihood,
        "严重度S": severity,
        "现有控制措施": existing_controls.strip(),
        "建议控制措施": suggested_controls.strip(),
        "控制后可能性L": residual_likelihood,
        "控制后严重度S": residual_severity,
    }
    record.update(calculate_jsa_record_risks(record))
    return record


def records_to_csv(records: Iterable[Mapping[str, object]]) -> bytes:
    """Serialize current-session JSA records as an Excel-friendly UTF-8 CSV."""
    rows = list(records)
    if not rows:
        return b""

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")
