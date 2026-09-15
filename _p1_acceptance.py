# -*- coding: utf-8 -*-
"""Phase 1 P1 closure acceptance: golden case + HF failure case.

Programmatic replay of the exact UI path: create draft -> precheck -> confirm
tags -> select demo documents -> build evidence stores -> run safety review ->
save pack -> human L/S -> confirm. Run:
    .venv/Scripts/python.exe _p1_acceptance.py
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db
import evidence_adapter
import safety_review
from services import persona_service, safety_review_service
from workflow.safety_review_graph import (
    SafetyReviewContext,
    precheck_draft,
    run_safety_review,
)

GOLDEN_IDS = [
    "DEMO-DOC-METHANOL-SDS",
    "DEMO-DOC-SOP-CONFINED-SPACE",
    "DEMO-DOC-SOP-LOTO",
]
HF_WRONG_IDS = ["DEMO-DOC-METHANOL-SDS", "DEMO-DOC-SOP-CONFINED-SPACE", "DEMO-DOC-SOP-LOTO"]
HF_RIGHT_IDS = ["DEMO-DOC-HF-SDS", "DEMO-DOC-SOP-CONFINED-SPACE", "DEMO-DOC-SOP-LOTO"]

GOLDEN_DRAFT = {
    "title": "甲醇储罐内部阀门检维修",
    "description": "进入曾储存甲醇的储罐内部更换阀门，作业前需要断电并拆卸连接管线。",
    "location": "甲醇罐区（演示区域）",
    "planned_start": "2030-01-01T08:00",
    "planned_end": "2030-01-01T12:00",
    "people_count": 2,
    "responsible_person": "演示负责人",
    "contractor_involved": False,
    "work_steps": [
        "停机并断电、上锁挂牌",
        "拆卸连接管线并置换通风",
        "气体检测合格后进入储罐内部更换阀门",
        "作业结束恢复现场",
    ],
    "chemicals": ["Methanol", "甲醇"],
}

HF_DRAFT = {
    "title": "HF酸洗管线检维修（失败案例）",
    "description": "使用氢氟酸对换热管线进行酸洗后检维修，涉及HF暴露。",
    "location": "酸洗区（演示区域）",
    "planned_start": "2030-02-01T08:00",
    "planned_end": "2030-02-01T12:00",
    "people_count": 2,
    "responsible_person": "演示负责人",
    "contractor_involved": False,
    "work_steps": ["排空残液", "拆卸管线检查腐蚀"],
    "chemicals": ["氢氟酸"],
}


def build_stores(resources):
    uploads = [item for item in resources if item.get("file_bytes")]
    return (
        evidence_adapter.build_evidence_stores(uploads)
        if uploads
        else evidence_adapter.EvidenceStores()
    )


def section(title):
    print("\n" + "=" * 12, title, "=" * 12)


def main() -> int:
    failures: list[str] = []
    connection = db.open_database(":memory:")
    persona_service.seed_demo_users(connection)
    applicant = persona_service.require_user(connection, "DEMO-APPLICANT-01")
    ehs = persona_service.require_user(connection, "DEMO-EHS-01")

    # ---------------- GOLDEN CASE ----------------
    section("黄金案例：甲醇储罐内部阀门检维修")
    draft = safety_review_service.create_work_draft(connection, user=applicant, **GOLDEN_DRAFT)
    print("draft:", draft["id"], draft["title"])

    pre = precheck_draft(draft, use_llm=False)
    print("precheck mode:", pre["mode"])
    print("suggested_work_type:", pre["suggested_work_type"])
    print("suggested_risk_tags:", pre["suggested_risk_tags"])
    if pre["suggested_work_type"] != "受限空间":
        failures.append(f"golden: work type = {pre['suggested_work_type']}")
    for expected in ("受限空间", "化学品暴露", "触电", "压力/能量释放"):
        if expected not in pre["suggested_risk_tags"]:
            failures.append(f"golden: missing risk tag {expected}")

    confirmed_tags = list(dict.fromkeys(draft["user_risk_tags"] + pre["suggested_risk_tags"]))
    draft = safety_review_service.update_work_draft(
        connection, draft["id"], user=applicant,
        work_type=pre["suggested_work_type"],
        ai_risk_tags=pre["suggested_risk_tags"],
        confirmed_risk_tags=confirmed_tags,
    )
    print("confirmed_risk_tags:", confirmed_tags)

    resources = evidence_adapter.load_demo_resources(GOLDEN_IDS)
    stores = build_stores(resources)
    clean_resources = tuple(
        {k: v for k, v in item.items() if k != "file_bytes"} for item in resources
    )
    result = run_safety_review(
        draft, context=SafetyReviewContext(stores=stores, resources=clean_resources),
        thread_id="p1-accept-golden",
    )
    pack = result["pack"]
    print("mode:", result["mode"], "| status:", result["status"])

    sds_cites = [e for e in pack["evidence"] if e["source_type"] == "sds"]
    sop_cites = [e for e in pack["evidence"] if e["source_type"] == "sop"]
    print("SDS citations:", [(c["source_name"], c["page"]) for c in sds_cites])
    print("SOP citations:", [(c["source_name"], c["page"]) for c in sop_cites])
    if not sds_cites or not any("Methanol" in c["source_name"] for c in sds_cites):
        failures.append("golden: no real Methanol SDS citation")
    for c in sds_cites + sop_cites:
        if not safety_review.citation_is_complete(c):
            failures.append(f"golden: incomplete citation {c['evidence_id']}")
    if not sop_cites:
        failures.append("golden: no SOP citation")

    print("risk_findings:", [i["text"] for i in pack["risk_findings"]])
    print("controls:", len(pack["controls"]), "items | ppe:", len(pack["ppe"]),
          "| emergency:", len(pack["emergency_requirements"]))
    print("missing_items:", [i["text"] if isinstance(i, dict) else i for i in pack["missing_items"]])
    print("conflicts:", pack["conflicts"])
    print("conflict_note:", pack.get("conflict_note"))
    print("evidence_insufficient:", pack["evidence_insufficient"])
    if pack["conflicts"]:
        failures.append(f"golden: unexpected conflicts {pack['conflicts']}")
    if "No unresolved evidence conflict detected." not in str(pack.get("conflict_note")):
        failures.append(f"golden: conflict_note = {pack.get('conflict_note')!r}")

    v1 = safety_review_service.save_pack_version(connection, draft["id"], pack, user=ehs)
    print("pack version:", v1["version"], "| draft status:",
          safety_review_service.require_work_draft(connection, draft["id"])["status"])

    rated = []
    for index, item in enumerate(pack["jsa_draft"], start=1):
        # EHS human review: confirm control wording and bind real evidence refs
        # (same capability the review form exposes), then rate L/S by hand.
        item = dict(item)
        item["proposed_controls"] = "按SOP执行：断电上锁挂牌、管线打开前泄压置换、气体检测合格后进入"
        item["evidence_refs"] = [c["evidence_id"] for c in sop_cites]
        rated.append(safety_review.apply_human_risk_rating(
            item, likelihood=4, severity=5, residual_likelihood=2, residual_severity=3,
            confirmed_by="DEMO-EHS-01",
        ))
    updated = dict(pack)
    updated["jsa_draft"] = rated
    # Resolve residual missing items with human-confirmed entries bound to real evidence.
    sop_refs = [c["evidence_id"] for c in sop_cites]
    updated["controls"] = list(updated["controls"]) + [
        safety_review.make_pack_item(
            "按SOP执行：断电上锁挂牌、管线打开前泄压置换、气体检测合格后进入",
            origin="human", evidence_refs=sop_refs, requires_confirmation=False,
        )
    ]
    v2 = safety_review_service.save_pack_version(connection, draft["id"], updated, user=ehs)
    blockers = safety_review.pack_blockers(draft, v2)
    print("v", v2["version"], "blockers:", blockers)
    if blockers:
        failures.append(f"golden: unresolved blockers {blockers}")
    try:
        safety_review_service.confirm_pack(connection, draft["id"], user=applicant)
        failures.append("golden: applicant could confirm (should be blocked)")
    except ValueError:
        print("applicant confirm blocked: OK")
    confirmed = safety_review_service.confirm_pack(connection, draft["id"], user=ehs)
    print("confirmed by:", confirmed["confirmed_by"], "at", confirmed["confirmed_at"])
    final = safety_review_service.require_work_draft(connection, draft["id"])
    print("final draft status:", final["status"])
    if final["status"] != safety_review.STATUS_CONFIRMED:
        failures.append("golden: not confirmed")

    # ---------------- HF FAILURE CASE ----------------
    section("失败案例：HF作业 + 错误的Methanol SDS")
    hf = safety_review_service.create_work_draft(connection, user=applicant, **HF_DRAFT)
    pre_hf = precheck_draft(hf, use_llm=False)
    print("suggested_work_type:", pre_hf["suggested_work_type"])
    print("suggested_risk_tags:", pre_hf["suggested_risk_tags"])
    hf = safety_review_service.update_work_draft(
        connection, hf["id"], user=applicant,
        ai_risk_tags=pre_hf["suggested_risk_tags"],
        confirmed_risk_tags=list(dict.fromkeys(hf["user_risk_tags"] + pre_hf["suggested_risk_tags"])),
    )
    wrong = evidence_adapter.load_demo_resources(HF_WRONG_IDS)
    result = run_safety_review(
        hf, context=SafetyReviewContext(stores=build_stores(wrong),
        resources=tuple({k: v for k, v in i.items() if k != "file_bytes"} for i in wrong)),
        thread_id="p1-accept-hf-wrong",
    )
    pack = result["pack"]
    print("evidence_insufficient:", pack["evidence_insufficient"])
    print("missing_items:", [i["text"] if isinstance(i, dict) else i for i in pack["missing_items"]])
    print("conflicts:", json.dumps(pack["conflicts"], ensure_ascii=False, indent=1))
    mismatches = [c for c in pack["conflicts"] if c.get("kind") == "chemical_mismatch"]
    if not pack["evidence_insufficient"]:
        failures.append("hf: evidence_insufficient is False")
    if not mismatches:
        failures.append("hf: no chemical_mismatch conflict")
    else:
        refs = mismatches[0]["evidence_refs"]
        print("mismatch evidence_refs:", refs)
        if not refs:
            failures.append("hf: mismatch conflict without evidence_refs")
    sds_cites = [e for e in pack["evidence"] if e["source_type"] == "sds"]
    print("selected-but-wrong SDS citations:", [(c["source_name"], c["page"]) for c in sds_cites])
    if not sds_cites or not any("Methanol" in c["source_name"] for c in sds_cites):
        failures.append("hf: methanol SDS was not actually selected/used")
    if not any("PPE相关要求无法可靠确认" in m for m in map(str, pack["missing_items"])):
        failures.append("hf: PPE-unreliable message absent")
    if not any("急救/泄漏处置/消防要求无法可靠确认" in m for m in map(str, pack["missing_items"])):
        failures.append("hf: emergency-unreliable message absent")
    v1 = safety_review_service.save_pack_version(connection, hf["id"], pack, user=ehs)
    print("pack version:", v1["version"], "| draft status:",
          safety_review_service.require_work_draft(connection, hf["id"])["status"])
    blockers = safety_review.pack_blockers(hf, v1)
    print("blockers:", blockers)
    try:
        safety_review_service.confirm_pack(connection, hf["id"], user=ehs)
        failures.append("hf: confirmation NOT blocked with wrong SDS")
    except ValueError as exc:
        print("confirm blocked: OK ->", str(exc)[:120])

    section("失败案例后续：补充正确的HF SDS")
    right = evidence_adapter.load_demo_resources(HF_RIGHT_IDS)
    result = run_safety_review(
        hf, context=SafetyReviewContext(stores=build_stores(right),
        resources=tuple({k: v for k, v in i.items() if k != "file_bytes"} for i in right)),
        thread_id="p1-accept-hf-right",
    )
    pack = result["pack"]
    print("evidence_insufficient:", pack["evidence_insufficient"])
    print("conflicts:", pack["conflicts"], "| note:", pack.get("conflict_note"))
    sds_cites = [e for e in pack["evidence"] if e["source_type"] == "sds"]
    print("SDS citations:", [(c["source_name"], c["page"]) for c in sds_cites])
    if not any("Hydrofluoric" in c["source_name"] or "HF" in c["source_name"] for c in sds_cites):
        failures.append("hf-right: HF SDS citation missing")
    if any(c.get("kind") == "chemical_mismatch" for c in pack["conflicts"]):
        failures.append("hf-right: chemical_mismatch still present")
    print("ppe:", len(pack["ppe"]), "| emergency:", len(pack["emergency_requirements"]))
    rated = [
        safety_review.apply_human_risk_rating(
            {**item,
             "proposed_controls": "按SOP执行能量隔离与受限空间管理；HF作业前核对SDS急救与泄漏处置要求",
             "evidence_refs": [e["evidence_id"] for e in pack["evidence"] if e["source_type"] == "sop"]},
            likelihood=3, severity=5, residual_likelihood=1, residual_severity=4,
            confirmed_by="DEMO-EHS-01",
        )
        for item in pack["jsa_draft"]
    ]
    updated = dict(pack)
    updated["jsa_draft"] = rated
    sop_cites = [e["evidence_id"] for e in pack["evidence"] if e["source_type"] == "sop"]
    updated["controls"] = list(updated["controls"]) + [
        safety_review.make_pack_item(
            "按SOP执行能量隔离与受限空间管理；HF作业前核对SDS急救与泄漏处置要求",
            origin="human", evidence_refs=sop_cites, requires_confirmation=False,
        )
    ]
    v2 = safety_review_service.save_pack_version(connection, hf["id"], updated, user=ehs)
    blockers = safety_review.pack_blockers(hf, v2)
    print("v", v2["version"], "blockers:", blockers)
    if blockers:
        print("=> 说明：除SDS mismatch外仍存在的阻断项如上（如PPE/应急证据不足）")
    else:
        confirmed = safety_review_service.confirm_pack(connection, hf["id"], user=ehs)
        print("confirmed after correct HF SDS: OK", confirmed["confirmed_at"])

    section("结果")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL ACCEPTANCE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
