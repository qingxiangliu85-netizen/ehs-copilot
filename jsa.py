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
