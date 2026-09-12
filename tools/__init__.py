"""Tool layer for the EHS Copilot V3 workflow.

Every tool is a thin wrapper over an existing module:

===============  ==========================  ==================================
Tool             Reuses                      What it adds
===============  ==========================  ==================================
search_sds       ``rag``                     JSON payload + evidence list
calculate_risk   ``jsa.calculate_risk``      argument coercion, error payload
draft_jsa        ``jsa.create_jsa_record``   session append + response shape
create_hazard    ``hazards.create_hazard_record`` / ``next_hazard_id``
update_hazard    ``hazards.update_hazard_record``
get_dashboard_summary  ``dashboard.*`` / ``hazards.calculate_hazard_summary``
===============  ==========================  ==================================

No business logic is duplicated here — the arithmetic, validation and
aggregation all live in the original modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .context import ToolContext, ToolContextError, current_context, use_context
from . import dashboard_tools, hazard_tools, jsa_tools, sds_tools


@dataclass(frozen=True)
class ToolSpec:
    """Static description of one workflow tool."""

    name: str
    label: str
    description: str
    reuses: str
    function: Callable[..., dict]


TOOL_SPECS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="search_sds",
            label="SDS 检索",
            description="在已构建的 SDS 知识库中检索危险性、PPE、储存、急救、消防、泄漏等信息。",
            reuses="rag.retrieve_documents / rag.answer_question / rag.collect_sources",
            function=sds_tools.search_sds,
        ),
        ToolSpec(
            name="calculate_risk",
            label="风险值计算",
            description="按 R = L × S 计算风险值与风险等级。",
            reuses="jsa.calculate_risk",
            function=jsa_tools.calculate_risk,
        ),
        ToolSpec(
            name="draft_jsa",
            label="起草 JSA 记录",
            description="生成一条 JSA 记录并写入当前会话的 JSA 表格。",
            reuses="jsa.create_jsa_record / jsa.calculate_jsa_record_risks",
            function=jsa_tools.draft_jsa,
        ),
        ToolSpec(
            name="create_hazard",
            label="新增隐患记录",
            description="新增一条隐患整改记录并写入当前会话的隐患台账。",
            reuses="hazards.create_hazard_record / hazards.next_hazard_id",
            function=hazard_tools.create_hazard,
        ),
        ToolSpec(
            name="update_hazard",
            label="更新隐患记录",
            description="按隐患编号更新整改状态、风险等级、责任人等字段。",
            reuses="hazards.update_hazard_record",
            function=hazard_tools.update_hazard,
        ),
        ToolSpec(
            name="get_dashboard_summary",
            label="仪表盘汇总",
            description="汇总 JSA 与隐患的关键指标、风险分布与重点关注事项。",
            reuses="dashboard.calculate_dashboard_metrics / get_priority_items / get_jsa_risk_distribution",
            function=dashboard_tools.get_dashboard_summary,
        ),
    )
}

TOOL_NAMES: tuple[str, ...] = tuple(TOOL_SPECS)


def get_spec(name: str) -> ToolSpec | None:
    """Return the spec for ``name``, or ``None`` when it is unknown."""
    return TOOL_SPECS.get(name)


def describe_tools() -> list[dict[str, str]]:
    """Return a display-ready catalogue of the available tools."""
    return [
        {
            "name": spec.name,
            "label": spec.label,
            "description": spec.description,
            "reuses": spec.reuses,
        }
        for spec in TOOL_SPECS.values()
    ]


def run_tool(
    name: str,
    arguments: dict[str, object] | None = None,
    context: ToolContext | None = None,
) -> dict[str, object]:
    """Invoke one tool by name, binding ``context`` for the call.

    Unknown tools return an ``error`` payload rather than raising, so a single
    bad plan entry cannot break an entire workflow run.
    """
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return {
            "tool": name,
            "status": "error",
            "message": f"未知工具：{name}",
        }

    payload = dict(arguments or {})
    active = context or current_context()
    try:
        with use_context(active):
            return spec.function(active, **payload)
    except ToolContextError as exc:
        return {"tool": name, "status": "error", "message": str(exc)}
    except TypeError as exc:
        return {"tool": name, "status": "error", "message": f"工具参数不正确：{exc}"}
    except Exception as exc:  # noqa: BLE001 - tools must never crash the graph
        return {"tool": name, "status": "error", "message": f"{type(exc).__name__}: {exc}"}


# Imported last: the binding module imports the submodules above.
from .langchain_tools import LANGCHAIN_TOOLS, TOOL_BY_NAME  # noqa: E402


__all__ = [
    "LANGCHAIN_TOOLS",
    "TOOL_BY_NAME",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "ToolContext",
    "ToolContextError",
    "ToolSpec",
    "current_context",
    "describe_tools",
    "get_spec",
    "run_tool",
    "use_context",
]
