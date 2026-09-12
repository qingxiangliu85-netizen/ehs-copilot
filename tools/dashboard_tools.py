"""Dashboard summary tool.

Thin wrapper over :mod:`dashboard` and :mod:`hazards`.  All aggregation stays in
those modules; this one only bundles the metrics into one payload.
"""

from __future__ import annotations

from dashboard import (
    calculate_dashboard_metrics,
    get_hazard_distributions,
    get_jsa_risk_distribution,
    get_priority_items,
)

from .context import ToolContext


TOOL_NAME = "get_dashboard_summary"


def get_dashboard_summary(context: ToolContext) -> dict[str, object]:
    """Return the current session's headline EHS metrics and priority items."""
    jsa_records = context.jsa_records
    hazard_records = context.hazard_records

    metrics = calculate_dashboard_metrics(jsa_records, hazard_records)
    hazard_distributions = get_hazard_distributions(hazard_records)
    priority = get_priority_items(jsa_records, hazard_records)

    return {
        "tool": TOOL_NAME,
        "status": "ok",
        "metrics": {key: value for key, value in metrics.items()},
        "jsa_risk_distribution": get_jsa_risk_distribution(jsa_records),
        "hazard_risk_distribution": hazard_distributions["risk"],
        "hazard_status_distribution": hazard_distributions["status"],
        "hazard_type_distribution": hazard_distributions["type"],
        "priority_jsa": priority["jsa"],
        "priority_hazards": priority["hazards"],
    }
