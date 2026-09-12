"""Guardrails for the EHS Copilot workflow.

Guardrails are *pure functions*: they read the plan and the tool payloads and
return :class:`Violation` objects.  Nothing here calls an LLM, and nothing here
computes a risk score — the score always comes from :mod:`jsa`.

Three severities are used:

``block``
    该动作直接不执行（例如化学品与 SDS 不匹配、风险分值不是代码算出来的）。
``approval``
    必须由人确认后才能继续（例如写操作、SDS 证据不足时继续）。
``notice``
    只是提示，不阻断（例如紧急事件必须遵循企业应急预案）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import jsa

from .hitl import (
    OP_CLOSE_HAZARD,
    OP_CREATE_HAZARD,
    OP_MAJOR_RISK_WRITE,
    OP_RISK_DOWNGRADE,
    OP_SDS_CONFLICT,
    OP_SDS_INSUFFICIENT,
    OP_UPDATE_HAZARD,
)

# --------------------------------------------------------------------------- #
# Severity
# --------------------------------------------------------------------------- #

SEVERITY_BLOCK = "block"
SEVERITY_APPROVAL = "approval"
SEVERITY_NOTICE = "notice"

# --------------------------------------------------------------------------- #
# Codes
# --------------------------------------------------------------------------- #

GUARD_SDS_EVIDENCE = "sds_evidence_required"
GUARD_SDS_CONFLICT = "sds_source_conflict"
GUARD_RISK_BY_CODE = "risk_score_computed_by_code"
GUARD_RISK_ARGUMENTS = "risk_arguments_invalid"
GUARD_CHEMICAL_MISMATCH = "chemical_sds_mismatch"
GUARD_WRITE_APPROVAL = "write_requires_approval"
GUARD_MAJOR_RISK_WRITE = "major_risk_write_requires_approval"
GUARD_RISK_DOWNGRADE = "risk_downgrade_requires_approval"
GUARD_HAZARD_CLOSE = "hazard_close_requires_approval"
GUARD_EMERGENCY = "emergency_protocol"

GUARD_TITLES: dict[str, str] = {
    GUARD_SDS_EVIDENCE: "SDS 结论必须有文件名、页码与原文证据",
    GUARD_SDS_CONFLICT: "多个 SDS 文件对同一章节给出不同来源",
    GUARD_RISK_BY_CODE: "风险分值必须由现有代码计算",
    GUARD_RISK_ARGUMENTS: "风险参数必须是 1-5 的整数",
    GUARD_CHEMICAL_MISMATCH: "化学品与 SDS 不匹配",
    GUARD_WRITE_APPROVAL: "写操作必须经过审批",
    GUARD_MAJOR_RISK_WRITE: "涉及重大风险的写操作必须经过审批",
    GUARD_RISK_DOWNGRADE: "降低风险等级必须经过审批",
    GUARD_HAZARD_CLOSE: "关闭隐患必须经过审批",
    GUARD_EMERGENCY: "紧急事件须遵循企业应急预案与专业人员指挥",
}

EMERGENCY_MESSAGE = (
    "⚠️ 检测到紧急事件描述。请立即停止依赖本工具，按以下顺序处置："
    "① 立即执行所在单位的**应急预案**（报警、疏散、隔离、切断源头）；"
    "② 由**现场应急处置负责人/专业人员**统一指挥，不得自行处置；"
    "③ 立即拨打企业应急电话及 119/120；"
    "④ 处置结束后再进行记录与原因分析。本工具不提供现场应急决策支持。"
)

MAJOR_RISK_LEVEL = "重大"
_RISK_ORDER: dict[str, int] = {"低": 1, "中": 2, "高": 3, "重大": 4}

RISK_TOOLS = frozenset({"calculate_risk", "draft_jsa"})
WRITE_TOOLS = frozenset({"create_hazard", "update_hazard"})

_SCORE_KEYS = ("risk_score", "risk_level", "风险值", "风险值r", "风险等级", "residual_risk_score")


# --------------------------------------------------------------------------- #
# Violation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Violation:
    """One guardrail finding."""

    code: str
    severity: str
    detail: str = ""
    tool: str = ""
    operation: str = ""
    title: str = ""

    def __post_init__(self) -> None:  # pragma: no cover - trivial defaulting
        if not self.title:
            object.__setattr__(self, "title", GUARD_TITLES.get(self.code, self.code))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "severity": self.severity,
            "detail": self.detail,
            "tool": self.tool,
            "operation": self.operation,
        }


# --------------------------------------------------------------------------- #
# Chemical lexicon
# --------------------------------------------------------------------------- #

CHEMICAL_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("氢氟酸", ("氢氟酸", "hf", "hydrofluoric")),
    ("硫酸", ("硫酸", "h2so4", "sulfuric", "sulphuric")),
    ("盐酸", ("盐酸", "hcl", "hydrochloric")),
    ("硝酸", ("硝酸", "hno3", "nitric")),
    ("磷酸", ("磷酸", "h3po4", "phosphoric")),
    ("氢氧化钠", ("氢氧化钠", "烧碱", "苛性钠", "naoh", "sodiumhydroxide")),
    ("氢氧化钾", ("氢氧化钾", "koh", "potassiumhydroxide")),
    ("氨水", ("氨水", "液氨", "nh3", "ammonia")),
    ("过氧化氢", ("过氧化氢", "双氧水", "h2o2", "peroxide")),
    ("次氯酸钠", ("次氯酸钠", "漂白水", "naclo", "hypochlorite")),
    ("丙酮", ("丙酮", "acetone")),
    ("甲醇", ("甲醇", "methanol")),
    ("乙醇", ("乙醇", "酒精", "ethanol")),
    ("甲苯", ("甲苯", "toluene")),
    ("二甲苯", ("二甲苯", "xylene")),
    ("苯", ("苯", "benzene")),
    ("乙酸乙酯", ("乙酸乙酯", "ethylacetate")),
    ("天然气", ("天然气", "甲烷", "lng", "naturalgas", "methane")),
    ("液化石油气", ("液化石油气", "lpg")),
    ("氢气", ("氢气", "hydrogen")),
    ("氮气", ("氮气", "nitrogen")),
    ("一氧化碳", ("一氧化碳", "carbonmonoxide")),
    ("硫化氢", ("硫化氢", "h2s", "hydrogensulfide")),
    ("铬酸", ("铬酸", "chromic")),
    ("甲醛", ("甲醛", "formaldehyde")),
)

CHEMICAL_ALIAS_MAP: dict[str, tuple[str, ...]] = dict(CHEMICAL_ALIASES)


def _alias_hits(alias: str, text: str) -> bool:
    """Return whether ``alias`` occurs in ``text`` with safe word boundaries."""
    if alias.isascii():
        if len(alias) <= 4:
            return bool(
                re.search(
                    rf"(?<![0-9a-z]){re.escape(alias)}(?![0-9a-z])", text.casefold()
                )
            )
        return alias in text.casefold()
    return alias in text


def chemical_mentions(text: str) -> tuple[str, ...]:
    """Return the chemicals explicitly named in ``text``.

    Overlapping short names are dropped in favour of the longest alias, so
    ``甲苯`` does not also report a bare ``苯``.
    """
    haystack = str(text or "")
    if not haystack.strip():
        return ()

    matched: dict[str, str] = {}
    for name, aliases in CHEMICAL_ALIASES:
        for alias in aliases:
            if _alias_hits(alias, haystack):
                previous = matched.get(name, "")
                if len(alias) > len(previous):
                    matched[name] = alias

    names = [
        name
        for name, alias in matched.items()
        if not any(
            alias != other and alias in other for other in matched.values()
        )
    ]
    return tuple(names)


def _coverage_tokens(
    loaded_files: Iterable[str] = (),
    document_aliases: Iterable[str] = (),
) -> set[str]:
    """Return the tokens that describe which SDS documents this session covers."""
    tokens: set[str] = set()
    for item in loaded_files or ():
        for part in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", str(item)):
            if part:
                tokens.add(part.casefold())
    for item in document_aliases or ():
        value = str(item).strip().casefold()
        if value:
            tokens.add(value)
    return tokens


def _chemical_covered(name: str, tokens: set[str]) -> bool:
    for alias in CHEMICAL_ALIAS_MAP.get(name, (name,)):
        value = alias.casefold()
        if value in tokens:
            return True
        if value.isascii():
            if len(value) >= 3 and any(value in token for token in tokens):
                return True
        elif any(value in token for token in tokens):
            return True
    return False


def chemical_mismatch(
    question: str,
    *,
    loaded_files: Iterable[str] = (),
    document_aliases: Iterable[str] = (),
) -> Violation | None:
    """Return a ``block`` violation when the question names an uncovered chemical."""
    mentioned = chemical_mentions(question)
    if not mentioned:
        return None
    tokens = _coverage_tokens(loaded_files, document_aliases)
    if not tokens:
        # Nothing is known about the loaded SDS corpus, so no mismatch can be claimed.
        return None
    if any(_chemical_covered(name, tokens) for name in mentioned):
        return None
    return Violation(
        code=GUARD_CHEMICAL_MISMATCH,
        severity=SEVERITY_BLOCK,
        detail=(
            f"问题点名的化学品（{'、'.join(mentioned)}）不在当前已加载 SDS 的覆盖范围内，"
            "已停止该 SDS 结论，避免用其它化学品的 SDS 回答。"
            "请上传对应的 SDS，或改问已加载 SDS 中明确列出的化学品。"
            "（说明：SDS 只能回答其所描述的化学品，不可跨化学品外推。）"
        ),
        tool="search_sds",
    )


# --------------------------------------------------------------------------- #
# Emergency
# --------------------------------------------------------------------------- #

_EMERGENCY_MARKER = re.compile(
    r"(发生|突发|正在|已经|刚刚|现场|立即|紧急|事故|有人|出现|闻到|"
    r"请求|求助|突然|一下子)"
)
_EMERGENCY_HAZARD = re.compile(
    r"(泄漏|着火|起火|火灾|爆炸|中毒|窒息|触电|灼伤|烧伤|受伤|昏迷|"
    r"呼吸困难|人员被困)"
)
# "泄漏了 / 着火了" is a live event even without an explicit marker word.
_LIVE_EVENT = re.compile(r"(泄漏|着火|起火|爆炸|中毒|触电|灼伤|烧伤)(了|啦|中)")


def emergency_notice(user_input: str) -> Violation | None:
    """Return a ``notice`` violation when the sentence describes a live emergency.

    A *question about* an emergency topic (e.g. "查询该 SDS 中关于泄漏处置的
    要求") is not an emergency, so a hazard noun on its own is never enough —
    the sentence must also read as a live event.
    """
    text = str(user_input or "")
    if not text.strip():
        return None

    triggered = bool(
        _EMERGENCY_MARKER.search(text) and _EMERGENCY_HAZARD.search(text)
    )
    if not triggered:
        triggered = bool(_LIVE_EVENT.search(text))
    if not triggered:
        return None

    return Violation(
        code=GUARD_EMERGENCY,
        severity=SEVERITY_NOTICE,
        detail=EMERGENCY_MESSAGE,
        tool="",
    )


# --------------------------------------------------------------------------- #
# Risk integrity
# --------------------------------------------------------------------------- #


def risk_argument_violations(plan: Iterable[Mapping[str, Any]]) -> list[Violation]:
    """Check that a plan never carries a pre-computed or out-of-range risk score."""
    violations: list[Violation] = []
    for item in plan or ():
        tool = str(item.get("tool", ""))
        if tool not in RISK_TOOLS:
            continue
        call_id = str(item.get("call_id", ""))
        arguments = dict(item.get("arguments") or {})

        injected = [
            key
            for key in arguments
            if str(key).strip().casefold() in _SCORE_KEYS
        ]
        if injected:
            violations.append(
                Violation(
                    code=GUARD_RISK_BY_CODE,
                    severity=SEVERITY_BLOCK,
                    detail=(
                        f"计划中的 {tool} 携带了预先给定的分数字段 {injected}。"
                        "风险分值只能由 jsa.calculate_risk 计算，不允许由模型或调用方直接给出。"
                    ),
                    tool=tool,
                )
            )
            continue

        for key, label in (("likelihood", "可能性 L"), ("severity", "严重度 S")):
            raw = arguments.get(key, 0)
            if isinstance(raw, bool) or not isinstance(raw, int):
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    violations.append(
                        Violation(
                            code=GUARD_RISK_ARGUMENTS,
                            severity=SEVERITY_BLOCK,
                            detail=f"{tool} 的 {label}（{key}）不是整数：{raw!r}。",
                            tool=tool,
                        )
                    )
                    continue
            else:
                value = raw
            if not 1 <= value <= 5:
                violations.append(
                    Violation(
                        code=GUARD_RISK_ARGUMENTS,
                        severity=SEVERITY_BLOCK,
                        detail=(
                            f"{tool} 的 {label}（{key}）必须在 1-5 之间，当前为 {value}。"
                        ),
                        tool=tool,
                    )
                )
    return violations


def risk_payload_violation(payload: Mapping[str, Any]) -> Violation | None:
    """Re-derive the risk score with :mod:`jsa` and compare it to the payload."""
    tool = str(payload.get("tool", ""))
    if tool not in RISK_TOOLS:
        return None
    if str(payload.get("status", "")) != "ok":
        return None

    record = dict(payload.get("record") or {})
    likelihood = payload.get("likelihood")
    severity = payload.get("severity")
    if likelihood is None:
        likelihood = record.get("可能性L")
    if severity is None:
        severity = record.get("严重度S")

    if likelihood is None or severity is None:
        if payload.get("risk_score") is None and record.get("风险值R") is None:
            return None
        return Violation(
            code=GUARD_RISK_BY_CODE,
            severity=SEVERITY_BLOCK,
            detail=(
                f"{tool} 返回了风险分值，却没有给出可复算的 L/S，"
                "无法确认该分值来自 jsa.calculate_risk，已停止使用该结果。"
            ),
            tool=tool,
        )

    try:
        expected_score, expected_level = jsa.calculate_risk(
            int(likelihood), int(severity)
        )
    except (TypeError, ValueError) as exc:
        return Violation(
            code=GUARD_RISK_BY_CODE,
            severity=SEVERITY_BLOCK,
            detail=f"{tool} 返回的 L/S 无法用 jsa.calculate_risk 复算：{exc}",
            tool=tool,
        )

    actual_score = payload.get("risk_score")
    actual_level = payload.get("risk_level")
    if actual_score is None and record:
        actual_score = record.get("风险值R")
    if actual_level is None and record:
        actual_level = record.get("风险等级")

    if actual_score != expected_score or actual_level != expected_level:
        return Violation(
            code=GUARD_RISK_BY_CODE,
            severity=SEVERITY_BLOCK,
            detail=(
                f"{tool} 返回 {actual_score}·{actual_level}，"
                f"但按 jsa.calculate_risk({likelihood}, {severity}) 应为 "
                f"{expected_score}·{expected_level}。分值与现有代码不一致，已停止使用该结果。"
            ),
            tool=tool,
        )

    residual_l = record.get("控制后可能性L")
    residual_s = record.get("控制后严重度S")
    if residual_l is not None and residual_s is not None:
        try:
            expected_residual = jsa.calculate_risk(int(residual_l), int(residual_s))
        except (TypeError, ValueError):
            expected_residual = None
        if expected_residual is not None:
            got_score = payload.get("residual_risk_score", record.get("残余风险R"))
            got_level = payload.get("residual_risk_level", record.get("残余风险等级"))
            if (got_score, got_level) != expected_residual:
                return Violation(
                    code=GUARD_RISK_BY_CODE,
                    severity=SEVERITY_BLOCK,
                    detail=(
                        f"{tool} 的残余风险 {got_score}·{got_level} 与 "
                        f"jsa.calculate_risk({residual_l}, {residual_s}) = "
                        f"{expected_residual[0]}·{expected_residual[1]} 不一致，"
                        "已停止使用该结果。"
                    ),
                    tool=tool,
                )
    return None


# --------------------------------------------------------------------------- #
# Plan-level write approval
# --------------------------------------------------------------------------- #


def _find_hazard(
    hazard_id: str, hazard_records: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    target = str(hazard_id or "").strip().upper()
    for record in hazard_records or ():
        if str(record.get("隐患编号", "")).strip().upper() == target:
            return dict(record)
    return {}


def write_violations(
    tool: str,
    arguments: Mapping[str, Any],
    hazard_records: Iterable[Mapping[str, Any]] = (),
) -> list[Violation]:
    """Return the approval requirements triggered by one write call."""
    args = dict(arguments or {})
    violations: list[Violation] = []

    if tool == "create_hazard":
        violations.append(
            Violation(
                code=GUARD_WRITE_APPROVAL,
                severity=SEVERITY_APPROVAL,
                detail="新增隐患会写入隐患台账，必须由人工确认后才能执行。",
                tool=tool,
                operation=OP_CREATE_HAZARD,
            )
        )
        if str(args.get("risk_level", "")) == MAJOR_RISK_LEVEL:
            violations.append(
                Violation(
                    code=GUARD_MAJOR_RISK_WRITE,
                    severity=SEVERITY_APPROVAL,
                    detail="该隐患被判为重大风险，涉及重大风险的写操作必须由人工确认。",
                    tool=tool,
                    operation=OP_MAJOR_RISK_WRITE,
                )
            )
        return violations

    if tool != "update_hazard":
        return violations

    identifier = str(args.get("hazard_id", ""))
    changes = {
        key: value
        for key, value in args.items()
        if key != "hazard_id" and value not in (None, "", [], {})
    }
    if changes:
        violations.append(
            Violation(
                code=GUARD_WRITE_APPROVAL,
                severity=SEVERITY_APPROVAL,
                detail=f"该操作会修改隐患 {identifier or '（未指定）'} 的字段 {sorted(changes)}，必须由人工确认。",
                tool=tool,
                operation=OP_UPDATE_HAZARD,
            )
        )

    if str(args.get("status", "")) == "已关闭":
        violations.append(
            Violation(
                code=GUARD_HAZARD_CLOSE,
                severity=SEVERITY_APPROVAL,
                detail="关闭隐患意味着整改闭环，必须由人工确认。",
                tool=tool,
                operation=OP_CLOSE_HAZARD,
            )
        )

    new_level = str(args.get("risk_level", ""))
    current = _find_hazard(identifier, hazard_records)
    if new_level and current:
        old_level = str(current.get("风险等级", ""))
        if _RISK_ORDER.get(new_level, 0) < _RISK_ORDER.get(old_level, 0):
            violations.append(
                Violation(
                    code=GUARD_RISK_DOWNGRADE,
                    severity=SEVERITY_APPROVAL,
                    detail=(
                        f"该操作把隐患 {identifier} 的风险等级从「{old_level}」"
                        f"降为「{new_level}」，降级必须由人工确认。"
                    ),
                    tool=tool,
                    operation=OP_RISK_DOWNGRADE,
                )
            )

    if new_level == MAJOR_RISK_LEVEL:
        violations.append(
            Violation(
                code=GUARD_MAJOR_RISK_WRITE,
                severity=SEVERITY_APPROVAL,
                detail=f"该操作把隐患 {identifier} 置为重大风险，必须由人工确认。",
                tool=tool,
                operation=OP_MAJOR_RISK_WRITE,
            )
        )
    return violations


def plan_write_violations(
    plan: Iterable[Mapping[str, Any]],
    hazard_records: Iterable[Mapping[str, Any]] = (),
) -> list[Violation]:
    """Return every write-approval violation in the plan."""
    found: list[Violation] = []
    for item in plan or ():
        found.extend(
            write_violations(
                str(item.get("tool", "")),
                item.get("arguments") or {},
                hazard_records,
            )
        )
    return found


# --------------------------------------------------------------------------- #
# Evidence-level guardrails
# --------------------------------------------------------------------------- #


def evidence_gaps(payload: Mapping[str, Any]) -> list[str]:
    """Return the reasons an SDS payload lacks usable evidence."""
    gaps: list[str] = []
    evidence = list(payload.get("evidence") or ())
    sources = list(payload.get("sources") or ())

    if not evidence or not sources:
        gaps.append("检索结果中没有返回任何证据片段。")
        return gaps

    for index, row in enumerate(evidence, start=1):
        source = str(row.get("source", "") or "")
        snippet = str(row.get("snippet", "") or "").strip()
        try:
            page = int(row.get("page", 0) or 0)
        except (TypeError, ValueError):
            page = 0
        if not source:
            gaps.append(f"第 {index} 条证据缺少文件名。")
        if page <= 0:
            gaps.append(f"第 {index} 条证据缺少页码。")
        if not snippet:
            gaps.append(f"第 {index} 条证据没有原文片段。")
    return gaps


def evidence_conflicts(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return SDS sections that are supported by more than one source file."""
    by_section: dict[str, set[str]] = {}
    for row in payload.get("evidence") or ():
        source = str(row.get("source", "") or "")
        raw_sections = str(row.get("sections", "") or "")
        for token in raw_sections.split(","):
            token = token.strip()
            if token and source:
                by_section.setdefault(token, set()).add(source)
    return [
        {"section": section, "sources": sorted(sources)}
        for section, sources in sorted(by_section.items())
        if len(sources) > 1
    ]


def sds_violations(
    payload: Mapping[str, Any],
    *,
    question: str = "",
    loaded_files: Iterable[str] = (),
    document_aliases: Iterable[str] = (),
) -> list[Violation]:
    """Return every guardrail finding for one ``search_sds`` payload."""
    if str(payload.get("tool", "")) != "search_sds":
        return []
    if str(payload.get("status", "")) != "ok":
        return []

    violations: list[Violation] = []

    mismatch = chemical_mismatch(
        question or str(payload.get("question", "")),
        loaded_files=loaded_files,
        document_aliases=document_aliases
        or list(payload.get("aliases") or ()),
    )
    if mismatch:
        violations.append(mismatch)

    gaps = evidence_gaps(payload)
    if gaps:
        violations.append(
            Violation(
                code=GUARD_SDS_EVIDENCE,
                severity=SEVERITY_APPROVAL,
                detail=(
                    "SDS 安全结论必须同时给出文件名、页码与原文证据，但本次检索存在缺陷："
                    + "；".join(gaps)
                    + " 若仍要继续使用该结论，必须由人工确认。"
                ),
                tool="search_sds",
                operation=OP_SDS_INSUFFICIENT,
            )
        )

    conflicts = evidence_conflicts(payload)
    if conflicts:
        detail = "；".join(
            f"第 {item['section']} 节同时命中 {'、'.join(item['sources'])}"
            for item in conflicts
        )
        violations.append(
            Violation(
                code=GUARD_SDS_CONFLICT,
                severity=SEVERITY_APPROVAL,
                detail=(
                    "同一 SDS 章节命中了多个不同来源文件，存在来源冲突："
                    + detail
                    + " 继续使用前必须由人工确认以哪一份为准。"
                ),
                tool="search_sds",
                operation=OP_SDS_CONFLICT,
            )
        )
    return violations


__all__ = [
    "CHEMICAL_ALIASES",
    "CHEMICAL_ALIAS_MAP",
    "EMERGENCY_MESSAGE",
    "GUARD_CHEMICAL_MISMATCH",
    "GUARD_EMERGENCY",
    "GUARD_HAZARD_CLOSE",
    "GUARD_MAJOR_RISK_WRITE",
    "GUARD_RISK_ARGUMENTS",
    "GUARD_RISK_BY_CODE",
    "GUARD_RISK_DOWNGRADE",
    "GUARD_SDS_CONFLICT",
    "GUARD_SDS_EVIDENCE",
    "GUARD_TITLES",
    "GUARD_WRITE_APPROVAL",
    "MAJOR_RISK_LEVEL",
    "RISK_TOOLS",
    "SEVERITY_APPROVAL",
    "SEVERITY_BLOCK",
    "SEVERITY_NOTICE",
    "Violation",
    "WRITE_TOOLS",
    "chemical_mentions",
    "chemical_mismatch",
    "emergency_notice",
    "evidence_conflicts",
    "evidence_gaps",
    "plan_write_violations",
    "risk_argument_violations",
    "risk_payload_violation",
    "sds_violations",
    "write_violations",
]
