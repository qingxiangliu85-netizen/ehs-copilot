"""Deterministic router for the EHS Copilot workflow.

The router turns one natural-language sentence into

* the recognised task type(s)  — SDS 查询 / JSA 风险评估 / 隐患管理 / 组合任务,
* the parameters the sentence carries (L/S, hazard id, dates, ...),
* an explicit, ordered tool-call plan.

It is deliberately rule-based: it needs no API key, it is reproducible, and its
decisions can be asserted in unit tests.  An LLM can later be layered on top
without changing this contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from hazards import HAZARD_TYPES

from .state import (
    TASK_DASHBOARD_SUMMARY,
    TASK_HAZARD_MANAGEMENT,
    TASK_JSA_RISK,
    TASK_ORDER,
    TASK_SDS_QUERY,
    task_label,
)


# --------------------------------------------------------------------------- #
# Intent patterns
# --------------------------------------------------------------------------- #

_INTENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        TASK_SDS_QUERY,
        (
            r"\bsds\b", r"\bmsds\b", r"安全数据表", r"安全技术说明",
            r"危险性", r"危害概述", r"主要危害", r"\bppe\b", r"个体防护",
            r"防护用品", r"劳保", r"储存", r"贮存", r"急救", r"皮肤接触",
            r"眼睛接触", r"吸入", r"泄漏", r"消防", r"灭火", r"闪点",
            r"沸点", r"理化", r"成分", r"运输", r"联合国编号", r"废弃处置",
            r"毒理", r"生态", r"暴露控制", r"稳定性", r"反应性", r"兼容性",
            r"禁配物", r"应急处置",
        ),
    ),
    (
        TASK_JSA_RISK,
        (
            r"\bjsa\b", r"作业安全分析", r"风险评估", r"风险分析", r"危害辨识",
            r"危害识别", r"危害因素", r"作业步骤", r"残余风险", r"风险值",
            r"\bl\s*[×x*]\s*s\b", r"可能性", r"严重度", r"\blikelihood\b",
            r"\bseverity\b", r"\br\s*=\s*l",
        ),
    ),
    (
        TASK_HAZARD_MANAGEMENT,
        (
            r"隐患", r"整改", r"闭环", r"纠错", r"纠正措施", r"整改期限",
            r"责任人", r"\bhazard\b", r"隐患排查",
        ),
    ),
    (
        TASK_DASHBOARD_SUMMARY,
        (
            r"仪表盘", r"看板", r"概览", r"总览", r"整体情况", r"完成率",
            r"风险分布", r"汇总", r"统计", r"\bdashboard\b",
        ),
    ),
)

_CREATE_HINTS = (
    "新增", "创建", "录入", "登记", "添加", "上报", "报告", "补录", "加一条",
    "记一条", "记录一条", "登记一条", "新增一条", "建一条",
)
_UPDATE_HINTS = (
    "更新", "修改", "调整为", "调整", "改为", "改成", "变更为", "关闭",
    "关掉", "标记为", "标记", "已完成", "完成整改", "已关闭",
)
_READ_ONLY_HINTS = (
    "查看", "看看", "有哪些", "有多少", "多少条", "统计", "列出", "清单",
    "情况", "概览", "汇总", "查询", "几项", "几条",
)
_DRAFT_HINTS = (
    "jsa", "作业安全分析", "风险评估", "风险分析", "起草", "生成", "创建",
    "添加", "记录", "新增", "做一", "做一个", "列一份", "写一份",
)

_STATUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"已关闭|关闭|关掉|闭环|已完成|完成整改|标记为完成", "已关闭"),
    (r"整改中|进行中|处理中", "整改中"),
    (r"待整改|未整改|待处理|未关闭", "待整改"),
)

_RISK_LEVEL_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"重大风险|风险等级\s*(?:为|=|:|：)?\s*重大", "重大"),
    (r"高风险|风险等级\s*(?:为|=|:|：)?\s*高", "高"),
    (r"中风险|风险等级\s*(?:为|=|:|：)?\s*中", "中"),
    (r"低风险|风险等级\s*(?:为|=|:|：)?\s*低", "低"),
)

_TYPE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("危化品管理", ("危化品", "化学品", "试剂", "酸碱", "hf", "氢氟酸")),
    ("PPE", ("ppe", "防护用品", "个体防护", "劳保", "护目镜", "手套")),
    ("消防", ("消防", "灭火", "火灾", "疏散")),
    ("电气安全", ("电气", "配电", "用电", "漏电", "插座")),
    ("设备安全", ("设备", "机械", "点检", "管线")),
    ("作业现场", ("作业现场", "现场", "通道", "标识", "物料")),
    ("环境管理", ("环境", "废液", "固废", "废气", "排放", "危废")),
)

_SHORT_ASCII_KEYWORDS = {"hf", "ppe"}


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolCallPlan:
    """One planned tool invocation."""

    tool: str
    arguments: dict[str, Any]
    reason: str
    call_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "reason": self.reason,
            "call_id": self.call_id,
        }


@dataclass(frozen=True)
class RouteDecision:
    """Full routing outcome for one user sentence."""

    task_types: tuple[str, ...] = ()
    label: str = ""
    matched_keywords: dict[str, list[str]] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    plan: tuple[ToolCallPlan, ...] = ()

    @property
    def is_combined(self) -> bool:
        return len(self.task_types) > 1


# --------------------------------------------------------------------------- #
# Small extraction helpers
# --------------------------------------------------------------------------- #


def _matched_terms(text: str, patterns: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for pattern in patterns:
        for hit in re.findall(pattern, text, flags=re.IGNORECASE):
            value = hit if isinstance(hit, str) else "".join(hit)
            value = value.strip()
            if value and value not in found:
                found.append(value)
    return found


def _labeled(text: str, names: tuple[str, ...]) -> str:
    for name in names:
        match = re.search(rf"{name}\s*[:：]\s*([^\n；;。]+)", text)
        if match:
            return match.group(1).strip()
    return ""


def _first_number(text: str, patterns: tuple[str, ...]) -> int | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def extract_risk_pair(text: str) -> tuple[int, int] | None:
    """Extract (L, S) from phrasings such as ``L=4 S=5``, ``4×5``, ``可能性4``."""
    likelihood = _first_number(
        text,
        (
            r"可能性\s*(?:L|为|=|:|：)?\s*([1-5])(?![0-9])",
            r"(?<![0-9A-Za-z])L\s*[=:：]?\s*([1-5])(?![0-9])",
        ),
    )
    severity = _first_number(
        text,
        (
            r"严重(?:度|性)\s*(?:S|为|=|:|：)?\s*([1-5])(?![0-9])",
            r"(?<![0-9A-Za-z])S\s*[=:：]?\s*([1-5])(?![0-9])",
        ),
    )
    if likelihood is not None and severity is not None:
        return likelihood, severity

    match = re.search(r"(?<![0-9])([1-5])\s*[×x*]\s*([1-5])(?![0-9])", text)
    if match:
        return int(match.group(1)), int(match.group(2))

    if likelihood is not None and severity is None:
        return likelihood, likelihood
    return None


def extract_hazard_id(text: str) -> str:
    """Extract a hazard identifier such as ``HZ-003`` or ``DEMO-HZ-002``."""
    match = re.search(r"((?:DEMO-)?HZ-\d+)", text, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def extract_status(text: str) -> str:
    for pattern, status in _STATUS_PATTERNS:
        if re.search(pattern, text):
            return status
    return ""


def extract_risk_level(text: str) -> str:
    for pattern, level in _RISK_LEVEL_PATTERNS:
        if re.search(pattern, text):
            return level
    return ""


def _keyword_hit(keyword: str, lowered_text: str) -> bool:
    if keyword in _SHORT_ASCII_KEYWORDS:
        return bool(
            re.search(
                rf"(?<![0-9a-z]){re.escape(keyword)}(?![0-9a-z])", lowered_text
            )
        )
    return keyword in lowered_text


def extract_hazard_type(text: str, focus: str = "") -> str:
    """Map a sentence (preferably its hazard clause) onto a hazard type.

    ``focus`` is checked first so a hazard described as "配电箱前堆放杂物"
    is classified by its own wording rather than by another clause of the
    same sentence.
    """
    for candidate in (focus, text):
        lowered = str(candidate or "").casefold()
        if not lowered:
            continue
        for hazard_type, keywords in _TYPE_PATTERNS:
            if any(_keyword_hit(keyword, lowered) for keyword in keywords):
                return hazard_type
    return ""


def extract_date(text: str) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else ""


def _strip_command_prefix(text: str) -> str:
    cleaned = text.strip()
    prefix = re.compile(
        r"^(?:请|帮我|麻烦|我想|我要|需要)?\s*"
        r"(?:查一下|查询|检索一下|检索|查找|搜索|看看|看一下|问一下|请问|告诉我|了解一下)"
        r"\s*[:：]?\s*"
    )
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = prefix.sub("", cleaned).strip()
    return cleaned or text.strip()


def _split_clauses(text: str) -> list[str]:
    return [
        clause.strip()
        for clause in re.split(r"[，,。;；\n]|并且|同时|以及|然后|另外|并", text)
        if clause.strip()
    ]


def _sds_question(text: str) -> str:
    """Pick the clause that looks most like an SDS question."""
    clauses = _split_clauses(text)
    if not clauses:
        return _strip_command_prefix(text)

    def score(clause: str) -> int:
        return sum(
            len(_matched_terms(clause, patterns))
            for task, patterns in _INTENT_PATTERNS
            if task == TASK_SDS_QUERY
        )

    best = max(clauses, key=score)
    if score(best) == 0:
        return _strip_command_prefix(text)
    return _strip_command_prefix(best)


def _owner(text: str) -> str:
    """Extract the responsible person, with or without a colon."""
    match = re.search(r"(?:责任人|负责人)\s*[:：]?\s*([^\n；;。，,]+)", text)
    return match.group(1).strip() if match else ""


def _job_name(text: str) -> str:
    labeled = _labeled(text, ("作业名称", "作业", "任务名称"))
    if labeled:
        return labeled
    quoted = re.search(r"[“\"'「『《]([^”\"'」』》]{2,40})[”\"'」』》]", text)
    if quoted:
        return quoted.group(1).strip()
    for clause in _split_clauses(text):
        if re.search(r"作业|酸洗|检修|动火|受限空间|高处|吊装|清洗", clause):
            cleaned = re.sub(r"^(?:为|给|对|针对)\s*", "", clause).strip()
            cleaned = re.split(r"做|进行|开展|安排|执行", cleaned)[0].strip()
            return cleaned or clause
    return "AI 工作流识别作业"


def _hazard_description(text: str) -> str:
    labeled = _labeled(text, ("隐患描述", "隐患", "描述", "问题"))
    if labeled:
        return labeled
    cleaned = _strip_command_prefix(text)
    for hint in _CREATE_HINTS + _UPDATE_HINTS:
        cleaned = cleaned.replace(hint, " ")
    cleaned = re.sub(r"隐患|一条|记录|台账|请|帮我|麻烦", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ：:，,。.；;")
    return cleaned or text.strip()


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def classify(user_input: str) -> tuple[str, ...]:
    """Return the recognised task types in canonical order."""
    text = str(user_input or "")
    if not text.strip():
        return ()
    lowered = text.casefold()
    matched = [
        task
        for task, patterns in _INTENT_PATTERNS
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)
    ]
    return tuple(task for task in TASK_ORDER if task in matched)


def extract_parameters(text: str, task_types: tuple[str, ...]) -> dict[str, Any]:
    """Pull the arguments the recognised tasks need out of one sentence."""
    description = _hazard_description(text)
    return {
        "risk_pair": extract_risk_pair(text),
        "hazard_id": extract_hazard_id(text),
        "hazard_status": extract_status(text),
        "hazard_level": extract_risk_level(text),
        "hazard_type": extract_hazard_type(text, description),
        "hazard_owner": _owner(text),
        "due_on": extract_date(text),
        "hazard_description": description,
        "job_name": _job_name(text),
        "job_step": _labeled(text, ("作业步骤", "步骤")),
        "hazard_factor": _labeled(text, ("危害因素", "危害")),
        "consequence": _labeled(text, ("可能后果", "后果")),
        "existing_controls": _labeled(text, ("现有控制措施", "现有措施")),
        "suggested_controls": _labeled(text, ("建议控制措施", "建议措施")),
        "sds_question": _sds_question(text) if TASK_SDS_QUERY in task_types else "",
        "wants_draft": bool(
            re.search("|".join(re.escape(item) for item in _DRAFT_HINTS), text, re.I)
        ),
    }


def _plan_sds(parameters: dict[str, Any]) -> ToolCallPlan:
    return ToolCallPlan(
        tool="search_sds",
        arguments={"question": parameters["sds_question"]},
        reason="需要从 SDS 知识库检索对应章节的证据",
        call_id="call_1_search_sds",
    )


def _plan_jsa(parameters: dict[str, Any], counter: list[int]) -> list[ToolCallPlan]:
    plans: list[ToolCallPlan] = []
    pair = parameters.get("risk_pair")
    wants_draft = bool(parameters.get("wants_draft"))

    if pair:
        counter[0] += 1
        plans.append(
            ToolCallPlan(
                tool="calculate_risk",
                arguments={"likelihood": pair[0], "severity": pair[1]},
                reason=f"输入中给出了 L={pair[0]}、S={pair[1]}，先计算风险值",
                call_id=f"call_{counter[0]}_calculate_risk",
            )
        )
        if not wants_draft:
            return plans

    counter[0] += 1
    plans.append(
        ToolCallPlan(
            tool="draft_jsa",
            arguments={
                "job_name": parameters["job_name"],
                "job_step": parameters["job_step"],
                "hazard": parameters["hazard_factor"],
                "consequence": parameters["consequence"],
                "likelihood": pair[0] if pair else 3,
                "severity": pair[1] if pair else 3,
                "existing_controls": parameters["existing_controls"],
                "suggested_controls": parameters["suggested_controls"],
            },
            reason="生成一条 JSA 记录并写入当前会话的 JSA 表格",
            call_id=f"call_{counter[0]}_draft_jsa",
        )
    )
    return plans


def _plan_hazard(text: str, parameters: dict[str, Any], counter: list[int]) -> list[ToolCallPlan]:
    hazard_id = parameters["hazard_id"]
    wants_update = bool(
        re.search("|".join(re.escape(item) for item in _UPDATE_HINTS), text)
    )
    wants_create = bool(
        re.search("|".join(re.escape(item) for item in _CREATE_HINTS), text)
    )
    read_only = bool(
        re.search("|".join(re.escape(item) for item in _READ_ONLY_HINTS), text)
    )

    if wants_update and hazard_id:
        counter[0] += 1
        return [
            ToolCallPlan(
                tool="update_hazard",
                arguments={
                    "hazard_id": hazard_id,
                    "status": parameters["hazard_status"],
                    "risk_level": parameters["hazard_level"],
                    "owner": parameters["hazard_owner"],
                    "due_on": parameters["due_on"],
                },
                reason=f"按编号 {hazard_id} 更新现有隐患记录",
                call_id=f"call_{counter[0]}_update_hazard",
            )
        ]

    if not wants_create and read_only and hazard_id:
        counter[0] += 1
        return [
            ToolCallPlan(
                tool="update_hazard",
                arguments={"hazard_id": hazard_id},
                reason=f"定位到隐患 {hazard_id}",
                call_id=f"call_{counter[0]}_update_hazard",
            )
        ]

    if not wants_create and read_only:
        counter[0] += 1
        return [
            ToolCallPlan(
                tool="get_dashboard_summary",
                arguments={},
                reason="该问题只是查询隐患现状，返回汇总数据即可",
                call_id=f"call_{counter[0]}_get_dashboard_summary",
            )
        ]

    counter[0] += 1
    return [
        ToolCallPlan(
            tool="create_hazard",
            arguments={
                "description": parameters["hazard_description"],
                "hazard_type": parameters["hazard_type"] or "其他",
                "risk_level": parameters["hazard_level"] or "中",
                "owner": parameters["hazard_owner"] or "待分配（AI工作流）",
                "due_on": parameters["due_on"],
                "status": parameters["hazard_status"] or "待整改",
                "hazard_id": hazard_id,
            },
            reason="新增一条隐患记录到当前会话的隐患台账",
            call_id=f"call_{counter[0]}_create_hazard",
        )
    ]


def build_plan(
    text: str,
    task_types: tuple[str, ...],
    parameters: dict[str, Any],
) -> tuple[ToolCallPlan, ...]:
    """Translate the recognised task types into an ordered tool plan."""
    counter = [0]
    plans: list[ToolCallPlan] = []

    if TASK_SDS_QUERY in task_types:
        plan = _plan_sds(parameters)
        counter[0] += 1
        plans.append(plan)
    if TASK_JSA_RISK in task_types:
        plans.extend(_plan_jsa(parameters, counter))
    if TASK_HAZARD_MANAGEMENT in task_types:
        plans.extend(_plan_hazard(text, parameters, counter))
    if TASK_DASHBOARD_SUMMARY in task_types and not any(
        plan.tool == "get_dashboard_summary" for plan in plans
    ):
        counter[0] += 1
        plans.append(
            ToolCallPlan(
                tool="get_dashboard_summary",
                arguments={},
                reason="用户要求查看 EHS 整体指标与风险分布",
                call_id=f"call_{counter[0]}_get_dashboard_summary",
            )
        )
    return tuple(plans)


def route(user_input: str) -> RouteDecision:
    """Full routing decision for one user sentence."""
    text = str(user_input or "").strip()
    if not text:
        return RouteDecision()

    task_types = classify(text)
    matched: dict[str, list[str]] = {}
    for task in task_types:
        patterns = next(
            item for name, item in _INTENT_PATTERNS if name == task
        )
        matched[task] = _matched_terms(text, patterns)

    parameters = extract_parameters(text, task_types)
    plan = build_plan(text, task_types, parameters)
    return RouteDecision(
        task_types=task_types,
        label=task_label(task_types),
        matched_keywords=matched,
        parameters=parameters,
        plan=plan,
    )


__all__ = [
    "RouteDecision",
    "ToolCallPlan",
    "build_plan",
    "classify",
    "extract_hazard_id",
    "extract_parameters",
    "extract_risk_pair",
    "route",
]
