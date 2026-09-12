"""JSA tools.

Thin wrappers over :mod:`jsa`.  Risk scoring and record construction stay in
:mod:`jsa`; this module only validates arguments, stores the record into the
session list and shapes the response.
"""

from __future__ import annotations

import jsa

from .context import ToolContext


CALCULATE_RISK_TOOL = "calculate_risk"
DRAFT_JSA_TOOL = "draft_jsa"


def calculate_risk(
    context: ToolContext,
    likelihood: int = 0,
    severity: int = 0,
) -> dict[str, object]:
    """Calculate R = L × S and its risk level for one likelihood/severity pair."""
    try:
        score, level = jsa.calculate_risk(int(likelihood), int(severity))
    except (TypeError, ValueError) as exc:
        return {
            "tool": CALCULATE_RISK_TOOL,
            "status": "error",
            "message": str(exc),
            "likelihood": likelihood,
            "severity": severity,
        }
    return {
        "tool": CALCULATE_RISK_TOOL,
        "status": "ok",
        "likelihood": int(likelihood),
        "severity": int(severity),
        "risk_score": score,
        "risk_level": level,
        "formula": f"R = {int(likelihood)} × {int(severity)} = {score}",
    }


def draft_jsa(
    context: ToolContext,
    *,
    job_name: str,
    job_step: str = "",
    hazard: str = "",
    consequence: str = "",
    likelihood: int = 3,
    severity: int = 3,
    existing_controls: str = "",
    suggested_controls: str = "",
    residual_likelihood: int = 0,
    residual_severity: int = 0,
    store: bool = True,
) -> dict[str, object]:
    """Draft one JSA record and (by default) append it to the session table.

    ``residual_likelihood`` / ``residual_severity`` of 0 mean "derive
    automatically" — one step down on likelihood, same severity.
    """
    name = str(job_name or "").strip()
    step = str(job_step or "").strip() or name
    hazard_text = str(hazard or "").strip() or "待补充危害因素（AI 工作流草稿）"
    consequence_text = str(consequence or "").strip() or "待补充可能后果（AI 工作流草稿）"

    residual_l = int(residual_likelihood) or max(1, int(likelihood) - 1)
    residual_s = int(residual_severity) or int(severity)

    try:
        record = jsa.create_jsa_record(
            job_name=name,
            job_step=step,
            hazard=hazard_text,
            consequence=consequence_text,
            likelihood=int(likelihood),
            severity=int(severity),
            existing_controls=str(existing_controls or "").strip()
            or "待补充现有控制措施（AI 工作流草稿）",
            suggested_controls=str(suggested_controls or "").strip()
            or "待补充建议控制措施（AI 工作流草稿）",
            residual_likelihood=residual_l,
            residual_severity=residual_s,
        )
    except (TypeError, ValueError) as exc:
        return {
            "tool": DRAFT_JSA_TOOL,
            "status": "error",
            "message": str(exc),
            "job_name": name,
        }

    if store:
        context.jsa_records.append(record)

    return {
        "tool": DRAFT_JSA_TOOL,
        "status": "ok",
        "job_name": record["作业名称"],
        "job_step": record["作业步骤"],
        "hazard": record["危害因素"],
        "record": record,
        "likelihood": int(likelihood),
        "severity": int(severity),
        "risk_score": record["风险值R"],
        "risk_level": record["风险等级"],
        "residual_likelihood": residual_l,
        "residual_severity": residual_s,
        "residual_risk_score": record["残余风险R"],
        "residual_risk_level": record["残余风险等级"],
        "stored": bool(store),
        "jsa_record_count": len(context.jsa_records),
    }
