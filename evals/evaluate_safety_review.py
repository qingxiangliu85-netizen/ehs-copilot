"""Deterministic Phase 1 evaluation; no API key or model download required."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db
import evidence_adapter
import safety_review
from services import permit_service, persona_service, safety_review_service


DATASET = Path(__file__).with_name("safety_review_dataset.jsonl")


def load_dataset() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def _complete_draft() -> dict[str, Any]:
    return {
        "title": "HF酸洗设备检维修（模拟）",
        "work_type": "危化品作业",
        "description": "设备停机后拆卸管路，涉及氢氟酸。",
        "location": "酸洗区（模拟）",
        "planned_start": "2030-01-01T08:00",
        "planned_end": "2030-01-01T12:00",
        "people_count": 2,
        "responsible_person": "现场负责人（Demo）",
        "work_steps": ["拆卸管线"],
        "chemicals": ["氢氟酸"],
        "confirmed_risk_tags": ["化学品暴露", "腐蚀/灼伤"],
    }


def _complete_pack() -> dict[str, Any]:
    citation = {
        "evidence_id": "EV-HF",
        "source_type": "sds",
        "source_name": "HF_Synthetic_SDS.pdf",
        "version": "v1",
        "page": 2,
        "snippet": "HF synthetic demo evidence",
        "chunk_id": "1",
        "locator": "",
    }
    item = safety_review.apply_human_risk_rating(
        safety_review.make_jsa_item(
            step_no=1,
            work_step="拆卸管线",
            hazard="腐蚀/灼伤",
            proposed_controls="依据适用资料确认控制措施",
            evidence_refs=["EV-HF"],
        ),
        likelihood=4,
        severity=5,
        residual_likelihood=2,
        residual_severity=3,
        confirmed_by="DEMO-EHS-01",
    )
    return {
        "work_summary": "HF酸洗检维修",
        "risk_findings": [safety_review.make_pack_item("腐蚀/灼伤", evidence_refs=["EV-HF"])],
        "evidence": [citation],
        "jsa_draft": [item],
        "controls": [safety_review.make_pack_item("核对控制要求", evidence_refs=["EV-HF"])],
        "ppe": [safety_review.make_pack_item("核对PPE", evidence_refs=["EV-HF"])],
        "emergency_requirements": [safety_review.make_pack_item("核对应急要求", evidence_refs=["EV-HF"])],
        "missing_items": [],
        "conflicts": [],
        "evidence_insufficient": False,
    }


def evaluate() -> dict[str, Any]:
    cases = load_dataset()
    outcomes: dict[str, bool] = {}
    risk_true_positive = 0
    risk_predicted = 0
    risk_expected = 0
    evidence_blocking_checks: list[bool] = []
    for case in cases:
        identifier = case["id"]
        if identifier in {"SR-001", "SR-002"}:
            result = safety_review.deterministic_precheck(case["input"])
            expected = case["expected"]
            predicted_tags = set(result["suggested_risk_tags"])
            expected_tags = set(expected["risk_tags"])
            risk_true_positive += len(predicted_tags & expected_tags)
            risk_predicted += len(predicted_tags)
            risk_expected += len(expected_tags)
            outcomes[identifier] = result["suggested_work_type"] == expected["work_type"] and all(
                tag in result["suggested_risk_tags"] for tag in expected["risk_tags"]
            )
        elif identifier in {"SR-003", "SR-004", "SR-005"}:
            citation = case.get("evidence")
            report = evidence_adapter.sds_match_report(
                case["input"]["chemicals"], [citation] if citation else []
            )
            outcomes[identifier] = report["matched"] == bool(case["expected"].get("sds_match", False))
            if case["expected"].get("blocked"):
                outcomes[identifier] = not report["matched"]
                evidence_blocking_checks.append(not report["matched"])
        elif identifier == "SR-006":
            missing = safety_review.missing_intake_fields(case["input"])
            outcomes[identifier] = all(value in missing for value in case["expected"]["missing"])
        elif identifier == "SR-007":
            outcomes[identifier] = bool(evidence_adapter.version_conflicts(case["resources"]))
        elif identifier == "SR-008":
            item = safety_review.make_jsa_item(step_no=1, **case["input"])
            outcomes[identifier] = item["likelihood"] is None and item["severity"] is None

    connection = db.open_database(":memory:")
    try:
        persona_service.seed_demo_users(connection)
        applicant = persona_service.require_user(connection, "DEMO-APPLICANT-01")
        ehs = persona_service.require_user(connection, "DEMO-EHS-01")
        draft = safety_review_service.create_work_draft(
            connection, user=applicant, **_complete_draft()
        )
        safety_review_service.save_pack_version(
            connection, draft["id"], _complete_pack(), user=ehs
        )
        complete_pack = _complete_pack()
        claim_items = [
            *complete_pack["risk_findings"],
            *complete_pack["controls"],
            *complete_pack["ppe"],
            *complete_pack["emergency_requirements"],
            *complete_pack["jsa_draft"],
        ]
        valid_evidence = {
            item["evidence_id"]
            for item in complete_pack["evidence"]
            if safety_review.citation_is_complete(item)
        }
        cited_claims = sum(
            bool(set(item.get("evidence_refs") or ()) & valid_evidence)
            for item in claim_items
        )
        citation_coverage = cited_claims / len(claim_items) if claim_items else 1.0
        before = len(permit_service.list_permits(connection))
        try:
            safety_review_service.confirm_pack(connection, draft["id"], user=applicant)
        except ValueError:
            outcomes["SR-009"] = True
        else:
            outcomes["SR-009"] = False
        safety_review_service.confirm_pack(connection, draft["id"], user=ehs)
        after = len(permit_service.list_permits(connection))
        outcomes["SR-010"] = (
            safety_review_service.require_work_draft(connection, draft["id"])["status"]
            == safety_review.STATUS_CONFIRMED
            and after == before
        )
    finally:
        connection.close()

    total = len(cases)
    passed = sum(bool(outcomes.get(case["id"])) for case in cases)
    return {
        "total": total,
        "passed": passed,
        "failed": [case["id"] for case in cases if not outcomes.get(case["id"])],
        "metrics": {
            "Work Type Accuracy": sum(outcomes.get(i, False) for i in ("SR-001", "SR-002")) / 2,
            "Risk Tag Precision": risk_true_positive / risk_predicted if risk_predicted else 1.0,
            "Risk Tag Recall": risk_true_positive / risk_expected if risk_expected else 1.0,
            "SDS Match Accuracy": sum(outcomes.get(i, False) for i in ("SR-003", "SR-004", "SR-005")) / 3,
            "Citation Coverage": citation_coverage,
            "Missing Item Detection": float(outcomes.get("SR-006", False)),
            "Conflict Detection": float(outcomes.get("SR-007", False)),
            "JSA Structure Completeness": float(outcomes.get("SR-008", False)),
            "Evidence-insufficient Blocking Compliance": (
                sum(evidence_blocking_checks) / len(evidence_blocking_checks)
                if evidence_blocking_checks
                else 1.0
            ),
            "Human Confirmation Compliance": float(outcomes.get("SR-009", False)),
            "No-Permit-Before-Phase-2 Compliance": float(outcomes.get("SR-010", False)),
        },
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
