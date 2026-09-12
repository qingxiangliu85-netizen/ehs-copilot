"""Dashboard calculations and Streamlit rendering for EHS Copilot."""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Iterable, Mapping

import pandas as pd
import streamlit as st

from hazards import HAZARD_STATUSES, HAZARD_TYPES, RISK_LEVELS, calculate_hazard_summary
from jobs import JOB_STATUS_EXECUTING, job_summary
from jsa import calculate_jsa_record_risks


DASHBOARD_RISK_LEVELS = ("低", "中", "高", "重大")
_RISK_ALIASES = {
    "低": "低",
    "低风险": "低",
    "中": "中",
    "中风险": "中",
    "高": "高",
    "高风险": "高",
    "重大": "重大",
    "重大风险": "重大",
}


def _normalise_risk_level(value: object) -> str:
    return _RISK_ALIASES.get(str(value).strip(), "")


def _jsa_final_risk_level(record: Mapping[str, object]) -> str:
    """Recalculate and return the record's residual/final risk level."""
    try:
        risks = calculate_jsa_record_risks(record)
        return _normalise_risk_level(risks["残余风险等级"])
    except (KeyError, TypeError, ValueError):
        residual = _normalise_risk_level(record.get("残余风险等级", ""))
        if residual:
            return residual
        return _normalise_risk_level(record.get("风险等级", ""))


def _jsa_final_risk_score(record: Mapping[str, object]) -> object:
    """Recalculate the residual score, with fallback for legacy records."""
    try:
        return calculate_jsa_record_risks(record)["残余风险R"]
    except (KeyError, TypeError, ValueError):
        return record.get("残余风险R", record.get("风险值R", ""))


def get_jsa_risk_distribution(
    records: Iterable[Mapping[str, object]],
) -> dict[str, int]:
    """Return the final/residual JSA risk distribution in display order."""
    counts = Counter(_jsa_final_risk_level(record) for record in records)
    return {level: counts.get(level, 0) for level in DASHBOARD_RISK_LEVELS}


def get_hazard_distributions(
    records: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, int]]:
    """Return dashboard-ready hazard risk, status and type distributions."""
    summary = calculate_hazard_summary(records)
    return {
        "risk": {
            level: int(summary["risk_distribution"].get(level, 0))
            for level in RISK_LEVELS
        },
        "status": {
            status: int(summary["status_distribution"].get(status, 0))
            for status in HAZARD_STATUSES
        },
        "type": {
            hazard_type: int(summary["type_distribution"].get(hazard_type, 0))
            for hazard_type in HAZARD_TYPES
        },
    }


def count_overdue_hazards(
    hazard_records: Iterable[Mapping[str, object]],
    *,
    today: date | None = None,
) -> int:
    """Return the number of open hazards whose rectification due date passed."""
    base = today or date.today()
    count = 0
    for record in hazard_records:
        if str(record.get("状态", "")) == "已关闭":
            continue
        raw = str(record.get("整改期限", "")).strip()
        if not raw:
            continue
        try:
            due = date.fromisoformat(raw)
        except ValueError:
            continue
        if due < base:
            count += 1
    return count


def _job_final_risk_level(job: Mapping[str, object]) -> str:
    """Return the EHS-confirmed residual risk level of one job, if any."""
    confirmation = dict(job.get("jsa_confirmation") or {})
    final = dict(confirmation.get("final") or {})
    return _normalise_risk_level(final.get("残余风险等级", "")) or (
        _normalise_risk_level(final.get("风险等级", ""))
    )


def get_job_metrics(
    job_records: Iterable[Mapping[str, object]],
) -> dict[str, int | float]:
    """Return the job-lifecycle metrics required by the dashboard."""
    rows = list(job_records)
    summary = job_summary(rows)
    distribution = dict(summary["status_distribution"])
    high_risk = sum(
        1 for job in rows if _job_final_risk_level(job) in {"高", "重大"}
    )
    return {
        "job_total": int(summary["total"]),
        "job_active": int(summary["active"]),
        "job_awaiting_approval": int(summary["awaiting_approval"]),
        "job_executing": int(distribution.get(JOB_STATUS_EXECUTING, 0)),
        "job_awaiting_review": int(summary["awaiting_review"]),
        "job_closed": int(summary["closed"]),
        "job_rejected": int(summary["rejected"]),
        "job_high_risk": high_risk,
        "job_completion_rate": float(summary["completion_rate"]),
    }


def calculate_dashboard_metrics(
    jsa_records: Iterable[Mapping[str, object]],
    hazard_records: Iterable[Mapping[str, object]],
    job_records: Iterable[Mapping[str, object]] = (),
    *,
    today: date | None = None,
) -> dict[str, int | float]:
    """Calculate headline metrics from the live JSA / hazard / job lists.

    ``job_records`` is optional so existing callers keep working; when it is
    omitted the job metrics are reported as zero.
    """
    jsa_rows = list(jsa_records)
    hazard_rows = list(hazard_records)
    jsa_distribution = get_jsa_risk_distribution(jsa_rows)
    hazard_summary = calculate_hazard_summary(hazard_rows)
    metrics: dict[str, int | float] = {
        "jsa_high_major": jsa_distribution["高"] + jsa_distribution["重大"],
        "hazard_pending": int(hazard_summary["pending"]),
        "hazard_closed": int(hazard_summary["closed"]),
        "completion_rate": float(hazard_summary["completion_rate"]),
        "jsa_total": len(jsa_rows),
        "hazard_total": len(hazard_rows),
        "hazard_overdue": count_overdue_hazards(hazard_rows, today=today),
    }
    metrics.update(get_job_metrics(job_records))
    return metrics


def get_priority_items(
    jsa_records: Iterable[Mapping[str, object]],
    hazard_records: Iterable[Mapping[str, object]],
    limit: int = 5,
) -> dict[str, list[dict[str, object]]]:
    """Return a small set of high/major records for management attention."""
    jsa_items: list[dict[str, object]] = []
    for record in jsa_records:
        final_level = _jsa_final_risk_level(record)
        if final_level in {"高", "重大"}:
            jsa_items.append(
                {
                    "作业名称": record.get("作业名称", ""),
                    "作业步骤": record.get("作业步骤", ""),
                    "最终风险等级": final_level,
                    "最终风险值R": _jsa_final_risk_score(record),
                }
            )

    hazard_items = [
        {
            "隐患编号": record.get("隐患编号", ""),
            "隐患描述": record.get("隐患描述", ""),
            "风险等级": record.get("风险等级", ""),
            "状态": record.get("状态", ""),
            "整改期限": record.get("整改期限", ""),
        }
        for record in hazard_records
        if _normalise_risk_level(record.get("风险等级", "")) in {"高", "重大"}
        and record.get("状态") != "已关闭"
    ]
    return {"jsa": jsa_items[:limit], "hazards": hazard_items[:limit]}


def _distribution_frame(distribution: Mapping[str, int]) -> pd.DataFrame:
    return pd.DataFrame(
        {"类别": list(distribution.keys()), "数量": list(distribution.values())}
    ).set_index("类别")


def render_dashboard_page(
    jsa_records: list[dict[str, object]],
    hazard_records: list[dict[str, object]],
    job_records: list[dict[str, object]] | None = None,
) -> None:
    """Render the business-first EHS dashboard backed by session-state lists."""
    job_rows = list(job_records or [])
    metrics = calculate_dashboard_metrics(
        jsa_records, hazard_records, job_rows
    )
    jsa_distribution = get_jsa_risk_distribution(jsa_records)
    hazard_distributions = get_hazard_distributions(hazard_records)
    priority = get_priority_items(jsa_records, hazard_records)

    st.title("EHS驾驶舱")
    st.caption("作业闭环、隐患整改与风险状态的业务总览。")
    st.info(
        "本页面数据来自作业闭环、JSA及隐患整改模块的会话记录，"
        "仅用于EHS数字化原型展示，不代表真实企业数据。"
    )
    st.warning("本工具不替代企业制度、现场风险评估及专业人员判断。")

    st.subheader("作业闭环")
    job_columns = st.columns(6)
    job_columns[0].metric("在办作业", metrics["job_active"])
    job_columns[1].metric("待审批", metrics["job_awaiting_approval"])
    job_columns[2].metric("执行中", metrics["job_executing"])
    job_columns[3].metric("待复查", metrics["job_awaiting_review"])
    job_columns[4].metric("高风险作业", metrics["job_high_risk"])
    job_columns[5].metric("作业闭环率", f"{metrics['job_completion_rate']:.1f}%")

    st.subheader("隐患整改")
    metric_columns = st.columns(6)
    metric_columns[0].metric("待整改隐患", metrics["hazard_pending"])
    metric_columns[1].metric("逾期隐患", metrics["hazard_overdue"])
    metric_columns[2].metric("已关闭隐患", metrics["hazard_closed"])
    metric_columns[3].metric("整改完成率", f"{metrics['completion_rate']:.1f}%")
    metric_columns[4].metric("JSA高/重大风险", metrics["jsa_high_major"])
    metric_columns[5].metric("隐患总数", metrics["hazard_total"])
    st.caption(
        f"作业总数：{metrics['job_total']}（已关闭 {metrics['job_closed']}）｜"
        f"JSA记录总数：{metrics['jsa_total']}"
    )

    st.subheader("风险分布")
    risk_columns = st.columns(2)
    with risk_columns[0]:
        st.markdown("#### JSA最终/残余风险等级")
        st.bar_chart(_distribution_frame(jsa_distribution), y="数量")
    with risk_columns[1]:
        st.markdown("#### 隐患风险等级")
        st.bar_chart(
            _distribution_frame(hazard_distributions["risk"]), y="数量"
        )

    st.subheader("隐患整改分布")
    hazard_columns = st.columns(2)
    with hazard_columns[0]:
        st.markdown("#### 整改状态")
        st.bar_chart(
            _distribution_frame(hazard_distributions["status"]), y="数量"
        )
    with hazard_columns[1]:
        st.markdown("#### 隐患类型")
        st.bar_chart(
            _distribution_frame(hazard_distributions["type"]), y="数量"
        )

    st.subheader("重点关注事项")
    priority_columns = st.columns(2)
    with priority_columns[0]:
        st.markdown("#### 高/重大风险JSA")
        if priority["jsa"]:
            st.dataframe(priority["jsa"], hide_index=True, use_container_width=True)
        else:
            st.caption("当前无高/重大风险JSA记录。")
    with priority_columns[1]:
        st.markdown("#### 高/重大且未关闭隐患")
        if priority["hazards"]:
            st.dataframe(
                priority["hazards"], hide_index=True, use_container_width=True
            )
        else:
            st.caption("当前无高/重大且未关闭隐患。")
