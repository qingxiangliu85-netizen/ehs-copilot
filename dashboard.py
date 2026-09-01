"""Dashboard calculations and Streamlit rendering for EHS Copilot."""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping

import pandas as pd
import streamlit as st

from hazards import HAZARD_STATUSES, HAZARD_TYPES, RISK_LEVELS, calculate_hazard_summary


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
    """Prefer residual risk and fall back to the initial JSA risk level."""
    residual = _normalise_risk_level(record.get("残余风险等级", ""))
    if residual:
        return residual
    return _normalise_risk_level(record.get("风险等级", ""))


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


def calculate_dashboard_metrics(
    jsa_records: Iterable[Mapping[str, object]],
    hazard_records: Iterable[Mapping[str, object]],
) -> dict[str, int | float]:
    """Calculate headline metrics from the live JSA and hazard record lists."""
    jsa_rows = list(jsa_records)
    hazard_rows = list(hazard_records)
    jsa_distribution = get_jsa_risk_distribution(jsa_rows)
    hazard_summary = calculate_hazard_summary(hazard_rows)
    return {
        "jsa_high_major": jsa_distribution["高"] + jsa_distribution["重大"],
        "hazard_pending": int(hazard_summary["pending"]),
        "hazard_closed": int(hazard_summary["closed"]),
        "completion_rate": float(hazard_summary["completion_rate"]),
        "jsa_total": len(jsa_rows),
        "hazard_total": len(hazard_rows),
    }


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
                    "最终风险值R": record.get(
                        "残余风险R", record.get("风险值R", "")
                    ),
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
) -> None:
    """Render a lightweight dashboard backed directly by session-state lists."""
    metrics = calculate_dashboard_metrics(jsa_records, hazard_records)
    jsa_distribution = get_jsa_risk_distribution(jsa_records)
    hazard_distributions = get_hazard_distributions(hazard_records)
    priority = get_priority_items(jsa_records, hazard_records)

    st.title("EHS Dashboard")
    st.caption("汇总JSA风险评估与隐患整改数据，用于演示EHS风险识别和整改闭环管理。")
    st.info(
        "本页面数据来自JSA及隐患整改模块的模拟/演示记录，"
        "仅用于EHS数字化原型展示，不代表真实企业数据。"
    )
    st.warning("本工具不替代企业制度、现场风险评估及专业人员判断。")

    metric_columns = st.columns(6)
    metric_columns[0].metric("JSA高/重大风险", metrics["jsa_high_major"])
    metric_columns[1].metric("待整改隐患", metrics["hazard_pending"])
    metric_columns[2].metric("已关闭隐患", metrics["hazard_closed"])
    metric_columns[3].metric("整改完成率", f"{metrics['completion_rate']:.1f}%")
    metric_columns[4].metric("JSA记录总数", metrics["jsa_total"])
    metric_columns[5].metric("隐患总数", metrics["hazard_total"])

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

