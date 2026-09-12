"""LangChain tool bindings for the workflow layer.

These functions exist so that ``langgraph.prebuilt.ToolNode`` can invoke the
tool layer with plain JSON arguments: the JSON schema is inferred from the
signatures below, and the session objects are supplied through
:func:`tools.context.current_context` instead of being part of the schema.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from .context import current_context
from .dashboard_tools import get_dashboard_summary as _get_dashboard_summary
from .hazard_tools import create_hazard as _create_hazard
from .hazard_tools import update_hazard as _update_hazard
from .jsa_tools import calculate_risk as _calculate_risk
from .jsa_tools import draft_jsa as _draft_jsa
from .sds_tools import search_sds as _search_sds


@tool
def calculate_risk(likelihood: int, severity: int) -> dict:
    """计算风险值 R = L × S。likelihood 为可能性 L（1-5），severity 为严重度 S（1-5）。"""
    return _calculate_risk(current_context(), likelihood=likelihood, severity=severity)


@tool
def search_sds(question: str) -> dict:
    """在已构建的 SDS 知识库中检索危险性、PPE、储存、急救、消防、泄漏等信息，返回答案与证据来源。"""
    return _search_sds(current_context(), question=question)


@tool
def draft_jsa(
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
) -> dict:
    """起草一条 JSA 作业安全分析记录并加入当前会话的 JSA 表格。

    job_name 为作业名称；job_step 为作业步骤；hazard 为危害因素；consequence 为可能后果；
    likelihood/severity 为初始可能性 L 与严重度 S（1-5）；existing_controls/suggested_controls 为现有与建议控制措施；
    residual_likelihood/residual_severity 为控制后 L/S，传 0 表示自动推断。
    """
    return _draft_jsa(
        current_context(),
        job_name=job_name,
        job_step=job_step,
        hazard=hazard,
        consequence=consequence,
        likelihood=likelihood,
        severity=severity,
        existing_controls=existing_controls,
        suggested_controls=suggested_controls,
        residual_likelihood=residual_likelihood,
        residual_severity=residual_severity,
    )


@tool
def create_hazard(
    description: str,
    hazard_type: str = "其他",
    risk_level: str = "中",
    owner: str = "待分配（AI工作流）",
    found_on: str = "",
    due_on: str = "",
    corrective_action: str = "",
    status: str = "待整改",
    hazard_id: str = "",
) -> dict:
    """新增一条隐患整改记录并加入当前会话的隐患台账。

    description 为隐患描述；hazard_type 为隐患类型（危化品管理/PPE/消防/电气安全/设备安全/作业现场/环境管理/其他）；
    risk_level 为风险等级（低/中/高/重大）；owner 为责任人；found_on/due_on 为发现日期与整改期限（YYYY-MM-DD）；
    corrective_action 为整改措施；status 为状态（待整改/整改中/已关闭）；hazard_id 留空则自动生成。
    """
    return _create_hazard(
        current_context(),
        description=description,
        hazard_type=hazard_type,
        risk_level=risk_level,
        owner=owner,
        found_on=found_on or None,
        due_on=due_on or None,
        corrective_action=corrective_action,
        status=status,
        hazard_id=hazard_id,
    )


@tool
def update_hazard(
    hazard_id: str,
    status: str = "",
    risk_level: str = "",
    owner: str = "",
    description: str = "",
    hazard_type: str = "",
    corrective_action: str = "",
    due_on: str = "",
) -> dict:
    """更新一条已有隐患记录（按隐患编号定位）。只填写需要变更的字段。

    hazard_id 为隐患编号（如 HZ-003）；status 为新的整改状态；risk_level 为新的风险等级；
    owner/description/hazard_type/corrective_action/due_on 分别对应责任人、隐患描述、隐患类型、整改措施、整改期限。
    """
    return _update_hazard(
        current_context(),
        hazard_id=hazard_id,
        status=status,
        risk_level=risk_level,
        owner=owner,
        description=description,
        hazard_type=hazard_type,
        corrective_action=corrective_action,
        due_on=due_on or None,
    )


@tool
def get_dashboard_summary() -> dict:
    """汇总当前会话的 EHS 仪表盘数据：JSA 与隐患总数、待整改/已关闭数量、整改完成率、风险分布与重点关注事项。"""
    return _get_dashboard_summary(current_context())


LANGCHAIN_TOOLS: tuple[BaseTool, ...] = (
    search_sds,
    calculate_risk,
    draft_jsa,
    create_hazard,
    update_hazard,
    get_dashboard_summary,
)

TOOL_BY_NAME: dict[str, BaseTool] = {item.name: item for item in LANGCHAIN_TOOLS}
