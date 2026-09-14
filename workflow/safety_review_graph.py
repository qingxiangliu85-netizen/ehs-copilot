"""Dedicated, fixed Safety Review Pack workflow for Phase 1.

The graph is intentionally not a free-planning agent.  It orchestrates a known
business process and pauses before human confirmation.  It never creates a
formal Permit and never assigns JSA L/S values.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, TypedDict
from uuid import uuid4

import evidence_adapter
import safety_review
from llm import generate_structured_response, is_llm_configured
from workflow.hitl import (
    ACTION_REJECT,
    ApprovalDecision,
    ApprovalRequest,
    OP_CONFIRM_SAFETY_REVIEW_PACK,
)


class SafetyReviewState(TypedDict, total=False):
    draft_id: str
    intake: dict[str, Any]
    mode: str
    precheck: dict[str, Any]
    evidence: list[dict[str, Any]]
    evidence_by_topic: dict[str, list[str]]
    pack: dict[str, Any]
    missing_items: list[str]
    conflicts: list[dict[str, Any]]
    blockers: list[str]
    status: str
    errors: list[str]
    thread_id: str
    human_decision: dict[str, Any]


@dataclass(frozen=True)
class SafetyReviewContext:
    stores: evidence_adapter.EvidenceStores = field(default_factory=evidence_adapter.EvidenceStores)
    resources: tuple[dict[str, Any], ...] = ()
    use_llm: bool = True


_CONTEXT: ContextVar[SafetyReviewContext | None] = ContextVar(
    "safety_review_context", default=None
)


@contextmanager
def bind_context(context: SafetyReviewContext) -> Iterator[None]:
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_context() -> SafetyReviewContext:
    return _CONTEXT.get() or SafetyReviewContext()


_PRECHECK_SYSTEM = """你是高风险作业信息预检器。只输出JSON。
只能从用户输入中识别候选作业类型、风险标签、缺失字段和需要追问的信息。
不得生成PPE、控制措施、急救、泄漏处置等安全结论，不得填写JSA的L/S。
候选作业类型和风险标签必须来自调用方给出的枚举。"""

_PACK_SYSTEM = """你是Safety Review Pack草稿整理器。只输出JSON。
只能使用提供的作业输入与Citation原文；不得引入外部知识、编造来源或重新解释缺失证据。
不得填写likelihood、severity、residual_likelihood或residual_severity。
每项安全结论必须引用真实evidence_id；证据不足时放入missing_items并标记evidence_insufficient=true。"""


def _safe_llm_precheck(draft: Mapping[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    if not is_llm_configured() or not current_context().use_llm:
        return fallback
    prompt = json.dumps(
        {
            "work_types": list(safety_review.WORK_TYPES),
            "risk_tags": list(safety_review.RISK_TAGS),
            "intake": safety_review.normalize_work_draft(draft),
            "required_output": {
                "suggested_work_type": "enum",
                "suggested_risk_tags": ["enum"],
                "missing_fields": ["string"],
                "recommended_questions": ["string"],
            },
        },
        ensure_ascii=False,
    )
    try:
        result = generate_structured_response(system_prompt=_PRECHECK_SYSTEM, user_prompt=prompt)
    except Exception:
        return fallback
    work_type = str(result.get("suggested_work_type") or fallback["suggested_work_type"])
    if work_type not in safety_review.WORK_TYPES:
        work_type = fallback["suggested_work_type"]
    tags = [
        str(value) for value in (result.get("suggested_risk_tags") or ())
        if str(value) in safety_review.RISK_TAGS
    ]
    return {
        "mode": "llm_structured",
        "suggested_work_type": work_type,
        "suggested_risk_tags": list(dict.fromkeys(tags or fallback["suggested_risk_tags"])),
        "missing_fields": list(dict.fromkeys(fallback["missing_fields"])),
        "recommended_questions": list(
            dict.fromkeys(str(value) for value in (result.get("recommended_questions") or fallback["recommended_questions"]) if str(value).strip())
        ),
    }


def precheck_work(state: SafetyReviewState) -> dict[str, Any]:
    draft = safety_review.normalize_work_draft(state.get("intake") or {})
    fallback = safety_review.deterministic_precheck(draft)
    precheck = _safe_llm_precheck(draft, fallback)
    return {
        "intake": draft,
        "precheck": precheck,
        "mode": precheck["mode"],
        "missing_items": [f"缺少必填信息：{item}" for item in precheck["missing_fields"]],
        "status": "prechecked",
    }


def precheck_draft(
    draft: Mapping[str, Any], *, use_llm: bool = True
) -> dict[str, Any]:
    """Run only the information-precheck node for step 2 of the UI."""
    with bind_context(SafetyReviewContext(use_llm=use_llm)):
        return dict(precheck_work({"intake": dict(draft)}).get("precheck") or {})


def _query(draft: Mapping[str, Any], topic: str) -> str:
    return " ".join(
        [
            str(draft.get("title", "")),
            str(draft.get("description", "")),
            " ".join(str(value) for value in (draft.get("chemicals") or ())),
            topic,
        ]
    ).strip()


def retrieve_evidence_node(state: SafetyReviewState) -> dict[str, Any]:
    draft = state["intake"]
    stores = current_context().stores
    topic_queries = {
        "risk": "危险性 主要危害 风险",
        "ppe": "PPE 个体防护 暴露控制",
        "emergency": "急救 泄漏处置 消防 应急",
        "controls": "作业步骤 控制措施 安全要求",
    }
    evidence: dict[str, dict[str, Any]] = {}
    topic_refs: dict[str, list[str]] = {}
    for topic, suffix in topic_queries.items():
        tracks = evidence_adapter.retrieve_evidence(
            stores, query=_query(draft, suffix), top_k=4
        )
        topic_refs[topic] = []
        for values in tracks.values():
            for citation in values:
                evidence[citation["evidence_id"]] = citation
                topic_refs[topic].append(citation["evidence_id"])

    user_items = [
        ("description", str(draft.get("description", ""))),
        *[(f"work_step/{index}", str(step)) for index, step in enumerate(draft.get("work_steps") or (), start=1)],
        *[(f"chemical/{index}", str(name)) for index, name in enumerate(draft.get("chemicals") or (), start=1)],
    ]
    for index, (locator, text) in enumerate(user_items, start=1):
        if not text.strip():
            continue
        citation = safety_review.normalize_citation(
            {
                "evidence_id": f"INPUT-{index:02d}",
                "source_type": "user_input",
                "source_name": "作业申请表",
                "snippet": text,
                "locator": locator,
            }
        )
        evidence[citation["evidence_id"]] = citation

    sds_items = [item for item in evidence.values() if item["source_type"] == "sds"]
    match = evidence_adapter.sds_match_report(draft.get("chemicals") or (), sds_items)
    missing = list(state.get("missing_items") or ())
    if match["missing_chemicals"]:
        missing.append("缺少适用SDS：" + "、".join(match["missing_chemicals"]))
    conflicts = evidence_adapter.version_conflicts(current_context().resources)
    return {
        "evidence": list(evidence.values()),
        "evidence_by_topic": topic_refs,
        "missing_items": list(dict.fromkeys(missing)),
        "conflicts": conflicts,
        "status": "evidence_retrieved",
    }


def _citation_text(ref: str, evidence: Mapping[str, Mapping[str, Any]]) -> str:
    item = evidence.get(ref) or {}
    return str(item.get("snippet", "")).strip()


def _evidence_items(refs: list[str], evidence: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for ref in refs[:3]:
        text = _citation_text(ref, evidence)
        if text:
            result.append(safety_review.make_pack_item(text, evidence_refs=[ref]))
    return result


def _fallback_pack(state: SafetyReviewState) -> dict[str, Any]:
    draft = state["intake"]
    evidence_list = list(state.get("evidence") or ())
    evidence = {str(item.get("evidence_id")): item for item in evidence_list}
    refs = state.get("evidence_by_topic") or {}
    input_refs = [key for key, value in evidence.items() if value.get("source_type") == "user_input"]
    tags = list(draft.get("confirmed_risk_tags") or draft.get("user_risk_tags") or state.get("precheck", {}).get("suggested_risk_tags") or ())
    risk_findings = [
        safety_review.make_pack_item(tag, origin="user_input", evidence_refs=input_refs[:1])
        for tag in tags
    ]
    jsa_items = []
    for index, step in enumerate(draft.get("work_steps") or (), start=1):
        hazard = tags[min(index - 1, len(tags) - 1)] if tags else "待人工识别"
        jsa_items.append(
            safety_review.make_jsa_item(
                step_no=index,
                work_step=str(step),
                hazard=hazard,
                evidence_refs=input_refs,
            )
        )
    sds_refs = [key for key, value in evidence.items() if value.get("source_type") == "sds"]
    document_controls = [
        key for key in refs.get("controls", [])
        if evidence.get(key, {}).get("source_type") in {"sds", "sop", "internal"}
    ]
    ppe_refs = [key for key in refs.get("ppe", []) if key in sds_refs]
    emergency_refs = [key for key in refs.get("emergency", []) if key in sds_refs]
    missing = list(state.get("missing_items") or ())
    if draft.get("chemicals") and not ppe_refs:
        missing.append("化学品相关PPE要求缺少匹配SDS证据")
    if draft.get("chemicals") and not emergency_refs:
        missing.append("化学品相关急救/泄漏/消防要求缺少匹配SDS证据")
    if not document_controls:
        missing.append("关键控制措施缺少SDS、SOP或内部资料依据")
    return {
        "pack_id": "",
        "draft_id": str(draft.get("id", "")),
        "version": 0,
        "work_summary": "；".join(
            value for value in [str(draft.get("title", "")), str(draft.get("description", ""))] if value
        ),
        "suggested_work_type": str(state.get("precheck", {}).get("suggested_work_type", "")),
        "risk_findings": risk_findings,
        "evidence": evidence_list,
        "jsa_draft": jsa_items,
        "controls": _evidence_items(document_controls, evidence),
        "ppe": _evidence_items(ppe_refs, evidence),
        "emergency_requirements": _evidence_items(emergency_refs, evidence),
        "missing_items": [safety_review.make_pack_item(value) for value in dict.fromkeys(missing)],
        "conflicts": list(state.get("conflicts") or ()),
        "evidence_insufficient": bool(missing),
        "human_confirmations": [
            "确认最终作业类型与风险标签",
            "填写每条JSA的L/S及残余L/S",
            "核对控制措施、PPE与应急要求的适用性",
        ],
        "created_at": safety_review.stamp(),
        "confirmed_at": "",
    }


def _safe_llm_pack(state: SafetyReviewState, fallback: dict[str, Any]) -> dict[str, Any]:
    if not is_llm_configured() or not current_context().use_llm:
        return fallback
    payload = {
        "intake": state["intake"],
        "precheck": state.get("precheck", {}),
        "citations": state.get("evidence", []),
        "required_sections": [
            "work_summary", "risk_findings", "jsa_draft", "controls", "ppe",
            "emergency_requirements", "missing_items", "conflicts", "evidence_insufficient"
        ],
        "prohibited_fields": [
            "likelihood", "severity", "residual_likelihood", "residual_severity"
        ],
    }
    try:
        candidate = generate_structured_response(
            system_prompt=_PACK_SYSTEM,
            user_prompt=json.dumps(payload, ensure_ascii=False),
        )
    except Exception:
        return fallback
    valid_refs = {str(item.get("evidence_id")) for item in state.get("evidence") or ()}
    result = dict(fallback)
    result["work_summary"] = str(candidate.get("work_summary") or fallback["work_summary"])
    for section in ("risk_findings", "controls", "ppe", "emergency_requirements"):
        cleaned = []
        for item in candidate.get(section) or ():
            if not isinstance(item, Mapping):
                continue
            refs = [str(ref) for ref in (item.get("evidence_refs") or ()) if str(ref) in valid_refs]
            text = str(item.get("text") or "").strip()
            if text and refs:
                cleaned.append(safety_review.make_pack_item(text, evidence_refs=refs))
        if cleaned:
            result[section] = cleaned
    # JSA wording may be proposed, but every risk number is forcibly blank.
    cleaned_jsa = []
    for index, item in enumerate(candidate.get("jsa_draft") or (), start=1):
        if not isinstance(item, Mapping):
            continue
        refs = [str(ref) for ref in (item.get("evidence_refs") or ()) if str(ref) in valid_refs]
        cleaned_jsa.append(
            safety_review.make_jsa_item(
                step_no=int(item.get("step_no") or index),
                work_step=str(item.get("work_step") or ""),
                hazard=str(item.get("hazard") or ""),
                consequence=str(item.get("consequence") or ""),
                existing_controls=str(item.get("existing_controls") or ""),
                proposed_controls=str(item.get("proposed_controls") or ""),
                evidence_refs=refs,
            )
        )
    if cleaned_jsa:
        result["jsa_draft"] = cleaned_jsa
    return result


def draft_review_pack(state: SafetyReviewState) -> dict[str, Any]:
    fallback = _fallback_pack(state)
    pack = _safe_llm_pack(state, fallback)
    return {"pack": pack, "status": "pack_drafted"}


def validate_review_pack(state: SafetyReviewState) -> dict[str, Any]:
    blockers = safety_review.pack_blockers(state["intake"], state["pack"])
    status = safety_review.STATUS_NEEDS_INPUT if blockers else safety_review.STATUS_REVIEW_READY
    return {"blockers": blockers, "status": status}


def human_review(state: SafetyReviewState) -> dict[str, Any]:
    from langgraph.types import interrupt

    request = ApprovalRequest(
        gate_id=f"safety-review-{state.get('draft_id') or uuid4().hex}",
        operation=OP_CONFIRM_SAFETY_REVIEW_PACK,
        reason=(
            "Safety Review Pack存在阻断项，需EHS补充后再确认。"
            if state.get("blockers")
            else "Safety Review Pack必须由EHS审核人确认后才能完成。"
        ),
        summary=str(state.get("pack", {}).get("work_summary", "")),
        proposed={"pack": state.get("pack", {})},
        guard_codes=tuple(
            ["human_confirmation_required"]
            + (["evidence_or_input_blocker"] if state.get("blockers") else [])
        ),
        kind="safety_review_pack",
    )
    raw = interrupt(request.to_payload())
    decision = ApprovalDecision.from_payload(raw)
    if decision is None or decision.action == ACTION_REJECT:
        return {"status": "rejected", "human_decision": dict(raw or {})}
    return {
        "status": safety_review.STATUS_REVIEW_READY,
        "human_decision": decision.to_payload(),
    }


def build_safety_review_graph() -> Any:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(SafetyReviewState)
    graph.add_node("precheck_work", precheck_work)
    graph.add_node("retrieve_evidence", retrieve_evidence_node)
    graph.add_node("draft_review_pack", draft_review_pack)
    graph.add_node("validate_review_pack", validate_review_pack)
    graph.add_node("human_review", human_review)
    graph.add_edge(START, "precheck_work")
    graph.add_edge("precheck_work", "retrieve_evidence")
    graph.add_edge("retrieve_evidence", "draft_review_pack")
    graph.add_edge("draft_review_pack", "validate_review_pack")
    graph.add_edge("validate_review_pack", "human_review")
    graph.add_edge("human_review", END)
    return graph.compile(checkpointer=MemorySaver())


def run_safety_review(
    draft: Mapping[str, Any],
    *,
    context: SafetyReviewContext | None = None,
    graph: Any = None,
    thread_id: str = "",
) -> dict[str, Any]:
    workflow = graph or build_safety_review_graph()
    identifier = str(thread_id or f"sr-{draft.get('id') or uuid4().hex}")
    initial: SafetyReviewState = {
        "draft_id": str(draft.get("id", "")),
        "intake": dict(draft),
        "thread_id": identifier,
        "errors": [],
    }
    with bind_context(context or SafetyReviewContext()):
        return dict(workflow.invoke(initial, config={"configurable": {"thread_id": identifier}}))


__all__ = [
    "SafetyReviewContext",
    "SafetyReviewState",
    "bind_context",
    "build_safety_review_graph",
    "current_context",
    "draft_review_pack",
    "human_review",
    "precheck_work",
    "precheck_draft",
    "retrieve_evidence_node",
    "run_safety_review",
    "validate_review_pack",
]
