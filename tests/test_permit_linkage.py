# -*- coding: utf-8 -*-
"""Phase 2 acceptance: confirmed Safety Review Pack -> formal Permit.

Covers scenarios A-G:
A. confirmed pack, no blockers -> one Permit in the existing draft state with
   inherited work information;
B. unconfirmed pack -> creation forbidden;
C. chemical mismatch / evidence insufficient -> creation forbidden;
D. idempotency: second call returns the same Permit;
E. provenance: permit detail can trace back to WD / pack;
F. the new Permit still walks the unchanged review flow (submit works);
G. existing demo Permit/Hazard data is untouched.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db  # noqa: E402
import demo_seed  # noqa: E402
import evidence_adapter  # noqa: E402
import safety_review  # noqa: E402
from services import persona_service, safety_review_service  # noqa: E402
from workflow import audit, permit_state  # noqa: E402
from workflow.safety_review_graph import (  # noqa: E402
    SafetyReviewContext,
    precheck_draft,
    run_safety_review,
)

GOLDEN_IDS = [
    "DEMO-DOC-METHANOL-SDS",
    "DEMO-DOC-SOP-CONFINED-SPACE",
    "DEMO-DOC-SOP-LOTO",
]

GOLDEN_DRAFT = {
    "title": "甲醇储罐内部阀门检维修（Phase 2 验收）",
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
    "title": "HF酸洗管线检维修（Phase 2 失败案例）",
    "description": "使用氢氟酸对换热管线进行酸洗后检维修。",
    "location": "酸洗区（演示区域）",
    "planned_start": "2030-02-01T08:00",
    "planned_end": "2030-02-01T12:00",
    "people_count": 2,
    "responsible_person": "演示负责人",
    "contractor_involved": False,
    "work_steps": ["排空残液", "拆卸管线检查腐蚀"],
    "chemicals": ["氢氟酸"],
}


def _resources_and_stores(ids):
    resources = evidence_adapter.load_demo_resources(ids)
    uploads = [item for item in resources if item.get("file_bytes")]
    stores = (
        evidence_adapter.build_evidence_stores(uploads)
        if uploads
        else evidence_adapter.EvidenceStores()
    )
    clean = tuple(
        {k: v for k, v in item.items() if k != "file_bytes"} for item in resources
    )
    return clean, stores


def _confirm_golden(connection, applicant, ehs):
    """Replay the P1 golden path; return the confirmed draft id."""
    draft = safety_review_service.create_work_draft(
        connection, user=applicant, **GOLDEN_DRAFT
    )
    pre = precheck_draft(draft, use_llm=False)
    draft = safety_review_service.update_work_draft(
        connection,
        draft["id"],
        user=applicant,
        work_type=pre["suggested_work_type"],
        ai_risk_tags=pre["suggested_risk_tags"],
        confirmed_risk_tags=list(
            dict.fromkeys(draft["user_risk_tags"] + pre["suggested_risk_tags"])
        ),
    )
    resources, stores = _resources_and_stores(GOLDEN_IDS)
    result = run_safety_review(
        draft,
        context=SafetyReviewContext(stores=stores, resources=resources),
        thread_id=f"p2-{draft['id']}",
    )
    pack = result["pack"]
    safety_review_service.save_pack_version(connection, draft["id"], pack, user=ehs)
    sop_refs = [
        c["evidence_id"] for c in pack["evidence"] if c["source_type"] == "sop"
    ]
    rated = []
    for index, item in enumerate(pack["jsa_draft"], start=1):
        item = dict(item)
        item["proposed_controls"] = "按SOP执行：断电上锁挂牌、管线打开前泄压置换、气体检测合格后进入"
        item["evidence_refs"] = sop_refs
        rated.append(
            safety_review.apply_human_risk_rating(
                item,
                likelihood=4,
                severity=5,
                residual_likelihood=2,
                residual_severity=3,
                confirmed_by="DEMO-EHS-01",
            )
        )
    updated = dict(pack)
    updated["jsa_draft"] = rated
    updated["controls"] = list(updated["controls"]) + [
        safety_review.make_pack_item(
            "按SOP执行：断电上锁挂牌、管线打开前泄压置换、气体检测合格后进入",
            origin="human",
            evidence_refs=sop_refs,
            requires_confirmation=False,
        )
    ]
    safety_review_service.save_pack_version(connection, draft["id"], updated, user=ehs)
    safety_review_service.confirm_pack(connection, draft["id"], user=ehs)
    return draft["id"]


class PermitLinkageTest(unittest.TestCase):
    def setUp(self):
        self.connection = db.open_database(":memory:")
        persona_service.seed_demo_users(self.connection)
        demo_seed.seed_demo_data(self.connection)
        self.applicant = persona_service.require_user(self.connection, "DEMO-APPLICANT-01")
        self.ehs = persona_service.require_user(self.connection, "DEMO-EHS-01")
        self.owner = persona_service.require_user(self.connection, "DEMO-OWNER-01")

    # -- Scenario A -------------------------------------------------------- #
    def test_a_confirmed_pack_creates_draft_permit_with_inherited_data(self):
        draft_id = _confirm_golden(self.connection, self.applicant, self.ehs)
        outcome = safety_review_service.create_permit_from_pack(
            self.connection, draft_id, user=self.applicant
        )
        self.assertTrue(outcome["created"])
        permit = outcome["permit"]
        self.assertEqual(permit["status"], permit_state.PERMIT_DRAFT)
        self.assertEqual(permit["title"], GOLDEN_DRAFT["title"])
        self.assertEqual(permit["area"], GOLDEN_DRAFT["location"])
        self.assertEqual(permit["valid_from"], GOLDEN_DRAFT["planned_start"])
        self.assertEqual(permit["valid_to"], GOLDEN_DRAFT["planned_end"])
        self.assertEqual(permit["applicant_id"], "DEMO-APPLICANT-01")
        # 负责人继承：自由文本姓名无法匹配 Demo 用户时回落到申请人
        self.assertEqual(permit["owner_id"], "DEMO-APPLICANT-01")
        # Methanol/甲醇 归一为同一实体，Methanol 变成别名，SDS 证据挂接
        chemical_names = [row["chemical_name"] for row in permit["chemicals"]]
        self.assertEqual(chemical_names.count("甲醇"), 1)
        self.assertNotIn("Methanol", chemical_names)
        methanol = permit["chemicals"][chemical_names.index("甲醇")]
        self.assertIn("Methanol", methanol["aliases"])
        self.assertTrue(methanol["sds_file"])
        self.assertEqual(methanol["sds_status"], "confirmed")
        self.assertEqual(len(permit["steps"]), len(GOLDEN_DRAFT["work_steps"]))
        self.assertEqual(len(permit["jsa_items"]), 4)
        self.assertTrue(
            all(row["confirmed_by"] == "DEMO-EHS-01" for row in permit["jsa_items"])
        )
        self.assertGreaterEqual(len(permit["evidence"]["sds"]), 1)
        # 控制措施聚合不重复拼接相同片段
        self.assertEqual(permit["control_measures"].count("按SOP执行"), 1)
        # provenance row + audit event
        source = safety_review_service.get_permit_source(self.connection, permit["id"])
        self.assertIsNotNone(source)
        self.assertEqual(source["draft_id"], draft_id)
        events = self.connection.execute(
            "SELECT * FROM audit_events WHERE entity_type=? AND entity_id=? "
            "AND action='work_draft.permit_created'",
            ("work_draft", draft_id),
        ).fetchall()
        self.assertEqual(len(events), 1)

    # -- Scenario B -------------------------------------------------------- #
    def test_b_unconfirmed_pack_is_blocked(self):
        draft_id = _confirm_golden(self.connection, self.applicant, self.ehs)
        # a fresh draft with a saved pack but no confirmation
        draft = safety_review_service.create_work_draft(
            self.connection, user=self.applicant, **HF_DRAFT
        )
        pre = precheck_draft(draft, use_llm=False)
        draft = safety_review_service.update_work_draft(
            self.connection,
            draft["id"],
            user=self.applicant,
            ai_risk_tags=pre["suggested_risk_tags"],
            confirmed_risk_tags=list(
                dict.fromkeys(draft["user_risk_tags"] + pre["suggested_risk_tags"])
            ),
        )
        resources, stores = _resources_and_stores(GOLDEN_IDS)
        result = run_safety_review(
            draft,
            context=SafetyReviewContext(stores=stores, resources=resources),
            thread_id=f"p2-b-{draft['id']}",
        )
        safety_review_service.save_pack_version(
            self.connection, draft["id"], result["pack"], user=self.ehs
        )
        with self.assertRaises(ValueError) as ctx:
            safety_review_service.create_permit_from_pack(
                self.connection, draft["id"], user=self.applicant
            )
        self.assertIn("尚未由EHS确认", str(ctx.exception))
        # the golden draft's confirmed state is untouched by the failed call
        self.assertIsNone(
            safety_review_service.get_pack_permit_link(self.connection, draft["id"])
        )

    # -- Scenario C -------------------------------------------------------- #
    def test_c_mismatch_pack_cannot_create_permit(self):
        draft = safety_review_service.create_work_draft(
            self.connection, user=self.applicant, **HF_DRAFT
        )
        pre = precheck_draft(draft, use_llm=False)
        draft = safety_review_service.update_work_draft(
            self.connection,
            draft["id"],
            user=self.applicant,
            ai_risk_tags=pre["suggested_risk_tags"],
            confirmed_risk_tags=list(
                dict.fromkeys(draft["user_risk_tags"] + pre["suggested_risk_tags"])
            ),
        )
        # wrong SDS (Methanol) for an HF job -> chemical mismatch
        resources, stores = _resources_and_stores(
            ["DEMO-DOC-METHANOL-SDS", "DEMO-DOC-SOP-CONFINED-SPACE", "DEMO-DOC-SOP-LOTO"]
        )
        result = run_safety_review(
            draft,
            context=SafetyReviewContext(stores=stores, resources=resources),
            thread_id=f"p2-c-{draft['id']}",
        )
        pack = result["pack"]
        self.assertTrue(
            any(c.get("kind") == "chemical_mismatch" for c in pack["conflicts"])
        )
        saved = safety_review_service.save_pack_version(
            self.connection, draft["id"], pack, user=self.ehs
        )
        blockers = safety_review.pack_blockers(draft, saved)
        self.assertTrue(
            any("Methanol" in blocker or "氢氟酸" in blocker for blocker in blockers)
            or any("SDS" in blocker for blocker in blockers)
        )
        with self.assertRaises(ValueError):
            safety_review_service.create_permit_from_pack(
                self.connection, draft["id"], user=self.applicant
            )

    # -- Scenario D -------------------------------------------------------- #
    def test_d_idempotent_creation(self):
        draft_id = _confirm_golden(self.connection, self.applicant, self.ehs)
        first = safety_review_service.create_permit_from_pack(
            self.connection, draft_id, user=self.applicant
        )
        second = safety_review_service.create_permit_from_pack(
            self.connection, draft_id, user=self.applicant
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["permit"]["id"], second["permit"]["id"])
        rows = self.connection.execute(
            "SELECT COUNT(*) AS total FROM permit_sources WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        self.assertEqual(rows["total"], 1)
        same_title = self.connection.execute(
            "SELECT COUNT(*) AS total FROM permits WHERE title=?",
            (GOLDEN_DRAFT["title"],),
        ).fetchone()
        self.assertEqual(same_title["total"], 1)

    # -- Scenario E -------------------------------------------------------- #
    def test_e_permit_detail_traces_back_to_source(self):
        draft_id = _confirm_golden(self.connection, self.applicant, self.ehs)
        outcome = safety_review_service.create_permit_from_pack(
            self.connection, draft_id, user=self.applicant
        )
        permit_id = outcome["permit"]["id"]
        source = safety_review_service.get_permit_source(self.connection, permit_id)
        self.assertEqual(source["draft_id"], draft_id)
        self.assertEqual(source["pack_version"], 2)
        self.assertEqual(source["confirmed_by"], "DEMO-EHS-01")
        self.assertTrue(source["confirmed_at"])
        self.assertIn(
            "化学品暴露", " ".join(source["snapshot"]["confirmed_risk_tags"])
        )
        # the source draft still resolves and is the confirmed one
        draft = safety_review_service.require_work_draft(self.connection, draft_id)
        self.assertEqual(draft["status"], safety_review.STATUS_CONFIRMED)
        pack = safety_review_service.get_latest_pack(self.connection, draft_id)
        self.assertEqual(pack["version"], 2)
        self.assertTrue(pack["confirmed_at"])

    # -- Scenario F -------------------------------------------------------- #
    def test_f_new_permit_walks_existing_review_flow(self):
        draft_id = _confirm_golden(self.connection, self.applicant, self.ehs)
        outcome = safety_review_service.create_permit_from_pack(
            self.connection, draft_id, user=self.applicant
        )
        permit_id = outcome["permit"]["id"]
        submitted = permit_service_submit(self.connection, permit_id, self.applicant)
        self.assertEqual(submitted["status"], permit_state.PERMIT_EHS_REVIEW)

    # -- Scenario G -------------------------------------------------------- #
    def test_g_demo_permit_flow_untouched(self):
        demo = self.connection.execute(
            "SELECT * FROM permits WHERE id='PERMIT-DEMO-001'"
        ).fetchone()
        self.assertIsNotNone(demo)
        self.assertIn(str(demo["status"]), permit_state.PERMIT_STATUSES)


def permit_service_submit(connection, permit_id, user):
    from services import permit_service

    return permit_service.submit_permit(connection, permit_id, user=user)


if __name__ == "__main__":
    unittest.main()
