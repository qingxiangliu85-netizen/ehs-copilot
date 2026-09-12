"""Fixed-dataset evaluation harness for the EHS Copilot V3 workflow assistant.

Run it with::

    python evals/evaluate.py

The harness replays every scenario in ``evals/dataset.jsonl`` through the real
LangGraph workflow with ``auto_approve=False`` (i.e. the interactive code path)
and computes five metrics from what actually happened:

Routing Accuracy
    The recognised task types equal the expected ones.
Tool Call Accuracy
    Both the *planned* and the *executed* tool sequences match expectation.
    They are scored together because a plan that was correctly suppressed by a
    guardrail is not the same thing as a plan that never existed.
Citation Coverage
    Of the SDS conclusions actually delivered to a user, how many are fully
    traceable to a file name, a page number and an original snippet.  A
    conclusion that a guardrail correctly withheld is *not* counted as a
    delivered answer — it is reported separately as withheld.

    The denominator is *every* delivered conclusion, including one that was
    delivered only because a human explicitly confirmed an under-evidenced
    result.  That is deliberate: the number is meant to show how much of what
    we handed to a user a machine could independently verify, so a human
    exception must lower it rather than be quietly excluded.  It must never be
    raised by dropping samples or loosening a guardrail.

    Because that single number mixes two different situations, the automated
    path is also reported on its own as
    ``citation_coverage_automated_path`` (see ``summarise``).
Approval Compliance
    Every approval-related invariant that must hold: no write without a human
    record, the pause happens before any mutation, the operation matches what
    should have been gated, and the decision (approve / modify / reject / none)
    produces exactly the expected outcome.
Workflow Completion
    The run ended in the expected terminal status.

Nothing here is hard-coded: the numbers printed and written to
``evals/results.json`` come from the run that just happened.  The only inputs
are ``evals/dataset.jsonl`` and the project code itself.

The harness runs deterministically and offline: the SDS answers come from the
retrieval-only path (no LLM key configured).  If an LLM key *is* configured the
SDS wording would come from the model, so the harness records that fact in its
output rather than pretending the run is reproducible.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.documents import Document  # noqa: E402

from hazards import create_demo_hazard_records  # noqa: E402
from llm import is_llm_configured  # noqa: E402
from tools import ToolContext  # noqa: E402
from workflow.graph import (  # noqa: E402
    new_thread_id,
    resume_workflow,
    run_workflow,
)
from workflow.state import (  # noqa: E402
    STATUS_AWAITING_APPROVAL,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_REJECTED,
)

EVAL_DIR = Path(__file__).resolve().parent
DATASET_PATH = EVAL_DIR / "dataset.jsonl"
RESULTS_JSON = EVAL_DIR / "results.json"
RESULTS_MD = EVAL_DIR / "RESULTS.md"

DEMO_SDS = PROJECT_ROOT / "data" / "demo_sds" / "EHS_Copilot_Demo_Synthetic_SDS.pdf"

WRITE_TOOLS = frozenset({"create_hazard", "update_hazard"})
SDS_TOOLS = frozenset({"search_sds"})

REQUIRED_EXPECT_KEYS = (
    "tasks",
    "planned",
    "executed",
    "status",
    "sds_outcome",
    "pauses",
    "delta_hazards",
)

_TERMINAL_STATUSES = {
    STATUS_COMPLETED,
    STATUS_BLOCKED,
    STATUS_REJECTED,
    STATUS_AWAITING_APPROVAL,
}


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #


def load_dataset(path: Path = DATASET_PATH) -> list[dict[str, Any]]:
    """Read the JSONL dataset, skipping blank lines."""
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                case = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name} 第 {line_number} 行不是合法 JSON：{exc}") from exc
            case["_line"] = line_number
            cases.append(case)
    return cases


def validate_dataset(cases: list[dict[str, Any]]) -> list[str]:
    """Return the list of structural problems found in the dataset."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    for case in cases:
        identifier = str(case.get("id", ""))
        label = identifier or f"第 {case.get('_line')} 行"
        if not identifier:
            problems.append(f"{label}：缺少 id")
        elif identifier in seen_ids:
            problems.append(f"{label}：id 重复")
        else:
            seen_ids.add(identifier)
        if not str(case.get("input", "")).strip():
            problems.append(f"{label}：缺少 input")
        if not str(case.get("category", "")).strip():
            problems.append(f"{label}：缺少 category")
        expect = case.get("expect")
        if not isinstance(expect, dict):
            problems.append(f"{label}：缺少 expect")
            continue
        for key in REQUIRED_EXPECT_KEYS:
            if key not in expect:
                problems.append(f"{label}：expect 缺少字段 {key}")
        if str(expect.get("status", "")) not in _TERMINAL_STATUSES:
            problems.append(f"{label}：expect.status 取值非法：{expect.get('status')!r}")
        outcome = expect.get("sds_outcome")
        if outcome not in (None, "cited", "withheld", "human_confirmed"):
            problems.append(f"{label}：expect.sds_outcome 取值非法：{outcome!r}")
        decision = expect.get("decision")
        if decision is not None:
            if not isinstance(decision, dict):
                problems.append(f"{label}：expect.decision 必须是对象或 null")
            elif decision.get("action") not in ("approve", "modify", "reject"):
                problems.append(f"{label}：decision.action 非法：{decision.get('action')!r}")
            elif decision.get("action") == "modify" and not decision.get("set"):
                problems.append(f"{label}：modify 决策缺少 set")
        if decision is not None and not expect.get("pauses"):
            problems.append(f"{label}：给出了决策但 pauses 为 false，两者矛盾")
    return problems


# --------------------------------------------------------------------------- #
# Session context fixtures
# --------------------------------------------------------------------------- #


class DemoUpload:
    """Adapt a repository PDF to the upload interface ``rag`` expects."""

    def __init__(self, path: Path) -> None:
        self.name = path.name
        self._payload = path.read_bytes()

    def getvalue(self) -> bytes:
        return self._payload


class _StubIndex:
    """Minimal stand-in for the FAISS index object ``rag`` reads."""

    def __init__(self, ntotal: int) -> None:
        self.ntotal = ntotal


class StubVectorStore:
    """Deterministic vector store used by the evidence-defect scenarios."""

    def __init__(self, documents: Iterable[tuple[Document, float]]) -> None:
        self._documents = list(documents)
        self.index = _StubIndex(len(self._documents))

    def similarity_search_with_score(self, question: str, k: int = 5):
        return list(self._documents)


PPE_SNIPPET = "8. 个体防护：操作时必须佩戴耐酸碱手套、护目镜与面屏。"


def _document(
    *,
    source: str,
    page: object | None,
    sections: str = "8",
    aliases: str = "demo",
    content: str = PPE_SNIPPET,
) -> Document:
    metadata: dict[str, object] = {
        "source": source,
        "sections": sections,
        "product_aliases": aliases,
    }
    if page is not None:
        metadata["page"] = page
    return Document(page_content=content, metadata=metadata)


def stub_store(kind: str) -> tuple[StubVectorStore, tuple[str, ...]]:
    """Return the vector store and loaded-file list for a defect fixture."""
    if kind == "empty":
        return StubVectorStore([]), ("demo.pdf",)
    if kind == "nopage":
        return StubVectorStore([(_document(source="demo.pdf", page=None), 0.1)]), (
            "demo.pdf",
        )
    if kind == "conflict":
        return (
            StubVectorStore(
                [
                    (_document(source="a.pdf", page=2), 0.1),
                    (_document(source="b.pdf", page=5), 0.1),
                ]
            ),
            ("a.pdf", "b.pdf"),
        )
    raise ValueError(f"未知的 SDS 缺陷夹具：{kind!r}")


def build_context(case: Mapping[str, Any], knowledge_base: Any) -> ToolContext:
    """Build a fresh session context for one scenario."""
    kind = str(case.get("sds") or "demo")
    if kind == "demo":
        if knowledge_base is None:
            raise RuntimeError("demo 场景需要真实 SDS 知识库，但索引未构建。")
        return ToolContext(
            vector_store=knowledge_base.vector_store,
            jsa_records=[],
            hazard_records=create_demo_hazard_records(),
            loaded_files=tuple(knowledge_base.file_names),
        )
    store, loaded = stub_store(kind)
    return ToolContext(
        vector_store=store,
        jsa_records=[],
        hazard_records=create_demo_hazard_records(),
        loaded_files=loaded,
    )


def build_knowledge_base() -> Any:
    """Build the real FAISS index over the repository demo SDS."""
    from rag import build_knowledge_base as _build

    if not DEMO_SDS.exists():
        raise FileNotFoundError(f"找不到 Demo SDS：{DEMO_SDS}")
    return _build([DemoUpload(DEMO_SDS)])


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def _snapshot(records: Iterable[Mapping[str, Any]]) -> dict[str, tuple[tuple[str, str], ...]]:
    """Return a comparable, hashable view of the hazard records."""
    snapshot: dict[str, tuple[tuple[str, str], ...]] = {}
    for record in records:
        identifier = str(record.get("隐患编号", ""))
        snapshot[identifier] = tuple(
            sorted((str(key), str(value)) for key, value in record.items())
        )
    return snapshot


def _decision_payload(result: Any, decision: Mapping[str, Any]) -> dict[str, Any]:
    """Build the resume payload for one scenario decision."""
    pending = dict(result.pending_approval or {})
    action = str(decision.get("action", ""))
    payload: dict[str, Any] = {
        "gate_id": str(pending.get("gate_id", "")),
        "action": action,
        "round": 1,
    }
    if decision.get("note"):
        payload["note"] = str(decision["note"])
    if action == "modify":
        calls = json.loads(json.dumps(list(pending.get("calls") or ()), default=str))
        override = dict(decision.get("set") or {})
        for call in calls:
            call["arguments"] = {**(call.get("arguments") or {}), **override}
        payload["calls"] = calls
    return payload


def execute_case(case: Mapping[str, Any], knowledge_base: Any) -> dict[str, Any]:
    """Run one scenario end-to-end and collect everything the metrics need."""
    expect = dict(case.get("expect") or {})
    context = build_context(case, knowledge_base)
    before = _snapshot(context.hazard_records)
    jsa_before = len(context.jsa_records)

    started = time.perf_counter()
    result = run_workflow(
        str(case["input"]),
        context,
        auto_approve=False,
        thread_id=new_thread_id(),
    )
    paused = bool(result.awaiting_approval)
    pending = dict(result.pending_approval or {})
    at_pause = _snapshot(context.hazard_records)

    decision = expect.get("decision")
    if paused and decision:
        result = resume_workflow(
            result.thread_id, context, _decision_payload(result, decision)
        )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    after = _snapshot(context.hazard_records)
    changed = sorted(
        identifier
        for identifier in set(before) | set(after)
        if before.get(identifier) != after.get(identifier)
    )
    executed = [str(item.get("tool", "")) for item in result.tool_results]
    codes = {str(item.get("code", "")) for item in result.guardrails}
    codes |= {str(item) for item in (pending.get("guard_codes") or ())}

    return {
        "id": str(case.get("id", "")),
        "category": str(case.get("category", "")),
        "input": str(case["input"]),
        "expect": expect,
        "tasks": [str(item) for item in result.task_types],
        "route_label": result.route_label,
        "planned": [str(item.get("tool", "")) for item in result.plan],
        "executed": executed,
        "status": result.status,
        "citations_ok": bool(result.citations_ok),
        "paused": paused,
        "pending_kind": str(pending.get("kind", "")),
        "pending_operation": str(pending.get("operation", "")),
        "guard_codes": sorted(code for code in codes if code),
        "approvals": [dict(item) for item in result.approvals],
        "delta_hazards": len(after) - len(before),
        "delta_jsa": len(context.jsa_records) - jsa_before,
        "changed_ids": changed,
        "changed_at_pause": sorted(
            identifier
            for identifier in set(before) | set(at_pause)
            if before.get(identifier) != at_pause.get(identifier)
        ),
        "hazards_after": after,
        "before": before,
        "sds_delivered": any(
            str(item.get("tool")) in SDS_TOOLS and str(item.get("status")) == "ok"
            for item in result.tool_results
        ),
        "human_confirmed": any(
            bool(item.get("human_confirmed")) for item in result.tool_results
        ),
        "elapsed_ms": elapsed_ms,
    }


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _check_routing(expect: Mapping[str, Any], observed: Mapping[str, Any]) -> tuple[bool, str]:
    wanted = [str(item) for item in expect.get("tasks") or ()]
    got = list(observed["tasks"])
    if wanted == got:
        return True, ""
    return False, f"任务类型 {got} ≠ 期望 {wanted}"


def _check_tools(expect: Mapping[str, Any], observed: Mapping[str, Any]) -> tuple[bool, str]:
    problems: list[str] = []
    wanted_plan = [str(item) for item in expect.get("planned") or ()]
    if wanted_plan != list(observed["planned"]):
        problems.append(f"计划 {observed['planned']} ≠ 期望 {wanted_plan}")
    wanted_run = [str(item) for item in expect.get("executed") or ()]
    if wanted_run != list(observed["executed"]):
        problems.append(f"实际执行 {observed['executed']} ≠ 期望 {wanted_run}")
    return (not problems), "；".join(problems)


def _check_completion(expect: Mapping[str, Any], observed: Mapping[str, Any]) -> tuple[bool, str]:
    wanted = str(expect.get("status", ""))
    got = str(observed["status"])
    if wanted == got:
        return True, ""
    return False, f"结束状态 {got} ≠ 期望 {wanted}"


def _check_guards(expect: Mapping[str, Any], observed: Mapping[str, Any]) -> tuple[bool, str]:
    wanted = [str(item) for item in expect.get("guard_codes") or ()]
    missing = [code for code in wanted if code not in observed["guard_codes"]]
    if missing:
        return False, f"缺少安全规则命中：{missing}（实际 {observed['guard_codes']}）"
    return True, ""


def _records_ok(
    expect: Mapping[str, Any], observed: Mapping[str, Any]
) -> tuple[bool, str]:
    after = observed["hazards_after"]
    for identifier, fields in (expect.get("record_assert") or {}).items():
        record = after.get(identifier)
        if record is None:
            return False, f"未找到隐患记录 {identifier}"
        lookup = dict(record)
        for key, value in dict(fields).items():
            if lookup.get(key) != str(value):
                return False, f"{identifier}.{key} = {lookup.get(key)!r}，期望 {value!r}"
    for identifier in expect.get("unchanged") or ():
        if after.get(identifier) != observed["before"].get(identifier):
            return False, f"{identifier} 被意外修改"
    return True, ""


def approval_checks(
    expect: Mapping[str, Any], observed: Mapping[str, Any]
) -> list[tuple[str, bool, str]]:
    """Return every approval invariant for one scenario as ``(label, ok, detail)``."""
    checks: list[tuple[str, bool, str]] = []
    writes = [tool for tool in observed["executed"] if tool in WRITE_TOOLS]
    human = [
        item
        for item in observed["approvals"]
        if not item.get("auto") and str(item.get("action")) in {"approve", "modify"}
    ]

    checks.append(
        (
            "写操作只在存在人工审批记录时执行",
            (not writes) or bool(human),
            "" if (not writes) or human else f"执行了 {writes} 但没有人工审批记录",
        )
    )

    expects_pause = bool(expect.get("pauses"))
    checks.append(
        (
            "暂停行为与预期一致",
            bool(observed["paused"]) == expects_pause,
            ""
            if bool(observed["paused"]) == expects_pause
            else f"实际暂停={observed['paused']}，期望={expects_pause}",
        )
    )

    if expects_pause:
        checks.append(
            (
                "暂停发生在任何记录变更之前",
                not observed["changed_at_pause"],
                ""
                if not observed["changed_at_pause"]
                else f"暂停时已变更：{observed['changed_at_pause']}",
            )
        )
        want_operation = str(expect.get("operation", ""))
        want_kind = str(expect.get("gate_kind", "plan"))
        ok = (
            observed["pending_operation"] == want_operation
            and observed["pending_kind"] == want_kind
        )
        checks.append(
            (
                "被拦截的操作与审批门与预期一致",
                ok,
                ""
                if ok
                else (
                    f"实际 {observed['pending_kind']}/{observed['pending_operation']}，"
                    f"期望 {want_kind}/{want_operation}"
                ),
            )
        )

    decision = dict(expect.get("decision") or {})
    action = str(decision.get("action", ""))
    records_ok, records_detail = _records_ok(expect, observed)

    if action == "reject":
        ok = not writes and observed["delta_hazards"] == 0 and not observed["changed_ids"]
        checks.append(
            (
                "拒绝后流程停止且零写入",
                ok,
                "" if ok else f"仍执行了写操作 {writes}，变更 {observed['changed_ids']}",
            )
        )
    elif action == "approve":
        want_run = [str(item) for item in expect.get("executed") or ()]
        ok = observed["executed"] == want_run
        detail = "" if ok else f"批准后执行 {observed['executed']}，期望 {want_run}"
        if want_run and all(tool in WRITE_TOOLS for tool in want_run):
            records_ok, records_detail = _records_ok(expect, observed)
            ok = ok and records_ok
            detail = detail or records_detail
        checks.append(("批准后按原参数继续执行", ok, detail))
    elif action == "modify":
        # A modification that would introduce a *new* approval requirement may not
        # be waved through by the same single approval, so the dataset can declare
        # that the escalation must end in a rejection.
        if str(expect.get("status")) == STATUS_REJECTED:
            ok = (
                not writes
                and observed["delta_hazards"] == 0
                and observed["status"] == STATUS_REJECTED
            )
            checks.append(
                (
                    "会引入新审批项的修改被按拒绝处理",
                    ok,
                    ""
                    if ok
                    else f"状态 {observed['status']}，写操作 {writes}，变更 {observed['changed_ids']}",
                )
            )
        else:
            ok = records_ok and bool(writes)
            checks.append(
                ("修改后的参数被实际采用", ok, "" if ok else records_detail or "未执行写操作")
            )
    elif decision == {} and expects_pause:
        ok = (
            observed["status"] == STATUS_AWAITING_APPROVAL
            and observed["delta_hazards"] == 0
            and not writes
        )
        checks.append(
            (
                "未获人工决策时保持挂起且未写入",
                ok,
                "" if ok else f"状态 {observed['status']}，写入 {writes}",
            )
        )
    else:
        ok = not writes
        checks.append(
            ("未经审批的写操作从未发生", ok, "" if ok else f"意外执行了 {writes}")
        )
    return checks


def citation_check(
    expect: Mapping[str, Any], observed: Mapping[str, Any]
) -> tuple[str, bool, str] | None:
    """Return ``(bucket, ok, detail)`` for the SDS citation rule, or ``None``."""
    outcome = expect.get("sds_outcome")
    if outcome == "cited":
        ok = bool(observed["sds_delivered"]) and bool(observed["citations_ok"])
        detail = ""
        if not ok:
            detail = (
                f"交付={observed['sds_delivered']}，证据完整={observed['citations_ok']}"
            )
        return "delivered", ok, detail
    if outcome == "human_confirmed":
        ok = bool(observed["sds_delivered"]) and bool(observed["human_confirmed"])
        detail = ""
        if not ok:
            detail = (
                f"交付={observed['sds_delivered']}，人工确认={observed['human_confirmed']}"
            )
        return "delivered", ok, detail
    if outcome == "withheld":
        ok = not observed["sds_delivered"]
        return "withheld", ok, "" if ok else "证据不合格的结论仍被交付给用户"
    return None


def score_case(case: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    """Score one executed scenario across all five metrics."""
    expect = dict(case.get("expect") or {})

    routing_ok, routing_detail = _check_routing(expect, observed)
    tools_ok, tools_detail = _check_tools(expect, observed)
    guards_ok, guards_detail = _check_guards(expect, observed)
    done_ok, done_detail = _check_completion(expect, observed)

    checks = approval_checks(expect, observed)
    checks.append(("命中的安全规则与预期一致", guards_ok, guards_detail))

    citation = citation_check(expect, observed)

    return {
        "id": observed["id"],
        "category": observed["category"],
        "input": observed["input"],
        "routing_ok": routing_ok,
        "routing_detail": routing_detail,
        "tools_ok": tools_ok,
        "tools_detail": tools_detail,
        "completion_ok": done_ok,
        "completion_detail": done_detail,
        "approval_checks": checks,
        "citation": citation,
        "status": observed["status"],
        "paused": observed["paused"],
        "elapsed_ms": observed["elapsed_ms"],
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _ratio(passed: int, total: int) -> float:
    return round(passed / total * 100, 1) if total else 0.0


def summarise(
    cases: list[dict[str, Any]], results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Aggregate the per-scenario scores into the five headline metrics."""
    total = len(results)
    routing_passed = sum(1 for item in results if item["routing_ok"])
    tools_passed = sum(1 for item in results if item["tools_ok"])
    completion_passed = sum(1 for item in results if item["completion_ok"])

    approval_flat = [
        (item["id"], label, ok, detail)
        for item in results
        for label, ok, detail in item["approval_checks"]
    ]
    approval_passed = sum(1 for _, _, ok, _ in approval_flat if ok)

    delivered = [
        item for item in results if item["citation"] and item["citation"][0] == "delivered"
    ]
    withheld = [
        item for item in results if item["citation"] and item["citation"][0] == "withheld"
    ]
    withheld_passed = sum(1 for item in withheld if item["citation"][1])

    # The headline denominator is *every* delivered answer, so a human-confirmed
    # exception (delivered, but not machine-traceable) pulls the number down
    # instead of being quietly dropped.  Deliberate — do NOT "fix" this by
    # excluding samples, dropping the human override, or relaxing a guardrail.
    #
    # The two paths are also reported separately, because one blended number
    # cannot say *which* of them is degraded:
    #
    #   automated  = the dataset demanded a fully traceable answer ("cited")
    #   overridden = the dataset expected a human-confirmed exception
    automated = [item for item in delivered if _expects_traceable(cases, item["id"])]
    overridden = [item for item in delivered if not _expects_traceable(cases, item["id"])]
    traceable = sum(1 for item in automated if item["citation"][1])
    override_confirmed = sum(1 for item in overridden if item["citation"][1])

    by_category: dict[str, Counter] = {}
    for item in results:
        bucket = by_category.setdefault(item["category"], Counter())
        bucket["total"] += 1
        bucket["routing"] += int(item["routing_ok"])
        bucket["tools"] += int(item["tools_ok"])
        bucket["completion"] += int(item["completion_ok"])

    failures = [
        {
            "id": item["id"],
            "category": item["category"],
            "input": item["input"],
            "reasons": [
                reason
                for reason in (
                    f"Routing：{item['routing_detail']}" if not item["routing_ok"] else "",
                    f"Tool Call：{item['tools_detail']}" if not item["tools_ok"] else "",
                    f"Completion：{item['completion_detail']}" if not item["completion_ok"] else "",
                    *[
                        f"{label}：{detail}"
                        for label, ok, detail in item["approval_checks"]
                        if not ok
                    ],
                    (
                        f"Citation：{item['citation'][2]}"
                        if item["citation"] and not item["citation"][1]
                        else ""
                    ),
                )
                if reason
            ],
        }
        for item in results
        if not (
            item["routing_ok"]
            and item["tools_ok"]
            and item["completion_ok"]
            and all(ok for _, ok, _ in item["approval_checks"])
            and (item["citation"] is None or item["citation"][1])
        )
    ]

    return {
        "case_count": total,
        "metrics": {
            "routing_accuracy": {
                "value": _ratio(routing_passed, total),
                "passed": routing_passed,
                "total": total,
            },
            "tool_call_accuracy": {
                "value": _ratio(tools_passed, total),
                "passed": tools_passed,
                "total": total,
            },
            "citation_coverage": {
                "value": _ratio(traceable, len(delivered)),
                "passed": traceable,
                "total": len(delivered),
                "withheld_correctly": withheld_passed,
                "withheld_total": len(withheld),
            },
            "citation_coverage_automated_path": {
                "value": _ratio(traceable, len(automated)),
                "passed": traceable,
                "total": len(automated),
                "human_override": len(overridden),
                "human_override_confirmed": override_confirmed,
            },
            "approval_compliance": {
                "value": _ratio(approval_passed, len(approval_flat)),
                "passed": approval_passed,
                "total": len(approval_flat),
            },
            "workflow_completion": {
                "value": _ratio(completion_passed, total),
                "passed": completion_passed,
                "total": total,
            },
        },
        "checks": [
            {"id": identifier, "label": label, "ok": ok, "detail": detail}
            for identifier, label, ok, detail in approval_flat
            if not ok
        ],
        "by_category": {
            name: dict(counter) for name, counter in sorted(by_category.items())
        },
        "failures": failures,
        "sds_delivered_answers": len(delivered),
        "sds_withheld_answers": len(withheld),
        "sds_scenarios_total": len(delivered) + len(withheld),
        "sds_automated_deliveries": len(automated),
        "sds_human_override_deliveries": len(overridden),
    }


def _expects_traceable(cases: list[dict[str, Any]], case_id: str) -> bool:
    """Return whether the scenario demands a fully traceable SDS answer."""
    for case in cases:
        if str(case.get("id")) == case_id:
            return str((case.get("expect") or {}).get("sds_outcome")) == "cited"
    return False


def format_report(report: Mapping[str, Any], *, llm_configured: bool) -> str:
    """Render the human-readable report written to ``evals/RESULTS.md``."""
    metrics = report["metrics"]
    lines = [
        "# EHS Copilot V3 — 评测结果",
        "",
        f"- 数据集：`evals/dataset.jsonl`（{report['case_count']} 个固定场景）",
        f"- 运行时间：{report['generated_at']}",
        f"- SDS 回答模式：{'LLM 生成（结果可能随模型波动）' if llm_configured else '检索直出（无 LLM 密钥，离线可复现）'}",
        "",
        "## 五项指标",
        "",
        "| 指标 | 结果 | 说明 |",
        "| --- | --- | --- |",
    ]
    rows = (
        ("Routing Accuracy", "routing_accuracy", "识别出的任务类型与预期完全一致"),
        ("Tool Call Accuracy", "tool_call_accuracy", "计划与实际执行的工具序列均与预期一致"),
        ("Citation Coverage", "citation_coverage", "已交付的 SDS 结论中，文件名+页码+原文完整可追溯的比例"),
        ("Approval Compliance", "approval_compliance", "全部审批不变式通过的比例"),
        ("Workflow Completion", "workflow_completion", "结束状态与预期一致"),
    )
    for title, key, note in rows:
        item = metrics[key]
        lines.append(
            f"| {title} | {item['value']}%（{item['passed']}/{item['total']}） | {note} |"
        )
    citation = metrics["citation_coverage"]
    lines.extend(
        [
            "",
            f"- 被 Guardrail 正确拦截、未交付给用户的 SDS 结论："
            f"{citation['withheld_correctly']}/{citation['withheld_total']}",
            "",
            "## Citation Coverage 口径说明",
            "",
            f"- 与 SDS 结论相关的场景共 **{report['sds_scenarios_total']}** 条"
            f"（已交付 {report['sds_delivered_answers']} + 被阻断 {report['sds_withheld_answers']}）。",
            f"- 其中 **{report['sds_withheld_answers']}** 条因证据不足、来源冲突或化学品不匹配，"
            "被 Guardrail 正确阻断，**未交付**给用户。",
            f"- 实际交付 **{report['sds_delivered_answers']}** 条，其中 "
            f"**{citation['passed']}** 条具备完整的「文件名 + 页码 + 原文」证据。",
            f"- 因此 Citation Coverage = **{citation['passed']} / {citation['total']} = "
            f"{citation['value']}%**。分母是全部已交付结论，人工放行的例外不剔除。",
            f"- 自动交付（不依赖人工放行）场景的引用覆盖率**单独报告**为 "
            f"**{metrics['citation_coverage_automated_path']['passed']} / "
            f"{metrics['citation_coverage_automated_path']['total']} = "
            f"{metrics['citation_coverage_automated_path']['value']}%**。",
            "",
            "| 口径 | 结果 | 分子 / 分母含义 |",
            "| --- | --- | --- |",
            f"| 全部已交付结论（主指标） | {citation['value']}%"
            f"（{citation['passed']}/{citation['total']}） "
            f"| 分母含 {report['sds_human_override_deliveries']} 条人工放行例外 |",
            f"| 自动交付场景（单独报告） | "
            f"{metrics['citation_coverage_automated_path']['value']}%"
            f"（{metrics['citation_coverage_automated_path']['passed']}/"
            f"{metrics['citation_coverage_automated_path']['total']}） "
            "| 不依赖人工放行，逐条机器可追溯 |",
            "",
            "> **这两条口径都不得通过排除失败样本、剔除人工放行样本或放宽安全规则来美化。**",
            f"> 本次有 {report['sds_human_override_deliveries']} 条结论是"
            "「证据不足或来源冲突、由人工确认后继续」，这些结论强制保留在主指标分母中。",
            "> 一旦剔除，主指标会虚高到 100%，而这正是本指标要防的事。",
            "",
            "## 按场景类别",
            "",
            "| 类别 | 场景数 | Routing | Tool Call | Completion |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for name, counter in report["by_category"].items():
        lines.append(
            f"| {name} | {counter['total']} | {counter['routing']}/{counter['total']} | "
            f"{counter['tools']}/{counter['total']} | {counter['completion']}/{counter['total']} |"
        )

    if report["failures"]:
        lines.extend(["", "## 未通过场景", ""])
        for item in report["failures"]:
            lines.append(f"- **{item['id']}**（{item['category']}）：{item['input']}")
            for reason in item["reasons"]:
                lines.append(f"  - {reason}")
    else:
        lines.extend(["", "全部场景均通过五项指标。", ""])

    lines.extend(
        [
            "",
            "> 本文件由 `evals/evaluate.py` 生成，数字来自本次实际运行，未做任何人工设定。",
            "",
        ]
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run(dataset_path: Path = DATASET_PATH) -> dict[str, Any]:
    """Execute the whole dataset and return the aggregated report."""
    cases = load_dataset(dataset_path)
    problems = validate_dataset(cases)
    if problems:
        raise ValueError("数据集校验未通过：\n" + "\n".join(f"- {item}" for item in problems))

    llm_configured = is_llm_configured()
    needs_index = any(str(case.get("sds") or "demo") == "demo" for case in cases)
    knowledge_base = build_knowledge_base() if needs_index else None

    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    for case in cases:
        observed = execute_case(case, knowledge_base)
        results.append(score_case(case, observed))
    wall_ms = int((time.perf_counter() - started) * 1000)

    report = summarise(cases, results)
    report["generated_at"] = datetime.now(timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S %z"
    )
    report["llm_configured"] = llm_configured
    report["wall_ms"] = wall_ms
    report["per_case"] = [
        {
            "id": item["id"],
            "category": item["category"],
            "status": item["status"],
            "paused": item["paused"],
            "routing_ok": item["routing_ok"],
            "tools_ok": item["tools_ok"],
            "completion_ok": item["completion_ok"],
            "approval_ok": all(ok for _, ok, _ in item["approval_checks"]),
            "elapsed_ms": item["elapsed_ms"],
        }
        for item in results
    ]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the EHS Copilot V3 evaluation dataset."
    )
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--json", type=Path, default=RESULTS_JSON)
    parser.add_argument("--md", type=Path, default=RESULTS_MD)
    parser.add_argument("--no-write", action="store_true", help="只打印，不写结果文件")
    args = parser.parse_args(argv)

    report = run(args.dataset)
    metrics = report["metrics"]

    print("=" * 78)
    print(f"EHS Copilot V3 评测 — {report['case_count']} 个场景，耗时 {report['wall_ms']} ms")
    print("=" * 78)
    for title, key in (
        ("Routing Accuracy", "routing_accuracy"),
        ("Tool Call Accuracy", "tool_call_accuracy"),
        ("Citation Coverage", "citation_coverage"),
        ("Approval Compliance", "approval_compliance"),
        ("Workflow Completion", "workflow_completion"),
    ):
        item = metrics[key]
        print(f"{title:<22} {item['value']:>6}%   ({item['passed']}/{item['total']})")
    citation = metrics["citation_coverage"]
    automated = metrics["citation_coverage_automated_path"]
    print(
        f"{'  其中正确拦截未交付':<22} {'':>6}    "
        f"({citation['withheld_correctly']}/{citation['withheld_total']})"
    )
    print(
        f"{'  其中自动交付且可追溯':<22} {automated['value']:>6}%   "
        f"({automated['passed']}/{automated['total']})"
        f"   ← 单独口径，主指标不含此口径"
    )
    print("-" * 78)
    for name, counter in report["by_category"].items():
        print(
            f"  {name:<20} n={counter['total']:<3} "
            f"routing={counter['routing']}/{counter['total']} "
            f"tools={counter['tools']}/{counter['total']} "
            f"completion={counter['completion']}/{counter['total']}"
        )
    if report["failures"]:
        print("-" * 78)
        print(f"未通过 {len(report['failures'])} 个场景：")
        for item in report["failures"]:
            print(f"  ✗ {item['id']} ({item['category']}) {item['input']}")
            for reason in item["reasons"]:
                print(f"      {reason}")
    else:
        print("-" * 78)
        print("全部场景通过五项指标。")

    if not args.no_write:
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        args.md.write_text(
            format_report(report, llm_configured=bool(report["llm_configured"])),
            encoding="utf-8",
        )
        print("-" * 78)
        print(f"已写出 {args.json.relative_to(PROJECT_ROOT)} 与 {args.md.relative_to(PROJECT_ROOT)}")
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
