"""Idempotent demo data bootstrap for the V5 browser experience.

The Demo covers **different kinds of high-risk non-routine work**, not just one
chemical job: exactly one hydrofluoric-acid (HF) permit is kept as the guided
golden example, and the other records demonstrate 动火 / 受限空间 / 电气隔离(LOTO)
/ 高处 / 开挖 / 其他高风险 work.

Every record here is simulated and labelled as such.  These are Demo work types
and Demo records only — the prototype does **not** claim to ship a
regulation-grade template for each special work type.

The seed only runs when the ``permits`` table is empty, so it never overwrites a
reviewer's own clicks.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from services import hazard_service, permit_service, persona_service

DEMO_DATA_LABEL = "Demo环境：模拟数据，不代表真实企业记录"
DEMO_SDS_FILE = "EHS_Copilot_Demo_Synthetic_SDS.pdf"

APPLICANT = "DEMO-APPLICANT-01"
EHS_REVIEWER = "DEMO-EHS-01"
APPROVER = "DEMO-APPROVER-01"
OWNER = "DEMO-OWNER-01"
ADMIN = "DEMO-ADMIN-01"

# The one and only HF case: it is the guided golden Demo, so it is never
# duplicated into other statuses.
GOLDEN_PERMIT_ID = "PERMIT-DEMO-001"
GOLDEN_PERMIT_TITLE = "槽罐区管道 HF 清洗作业（模拟）"
GOLDEN_WORK_TYPE = "危化品作业"

# Demo work types, in the order they are seeded.
WORK_TYPES: tuple[str, ...] = (
    "危化品作业",
    "动火作业",
    "受限空间作业",
    "电气隔离作业",
    "高处作业",
    "开挖作业",
    "其他高风险作业",
)

# Pipeline used to drive one seeded permit to its target status with real
# service commands, so every demo permit has a genuine audit trail.
PIPELINE_STAGES: tuple[str, ...] = (
    "draft",
    "ehs_review",
    "approval_pending",
    "approved",
    "active",
    "closeout_review",
    "closed",
)

_STEP_NOTE = "模拟步骤，不代表真实 SOP"


def _steps(*names: str) -> list[dict[str, Any]]:
    """Return a short simulated work-step list."""
    return [
        {"step_no": index, "name": name, "note": _STEP_NOTE}
        for index, name in enumerate(names, start=1)
    ]


def _sds_entry(snippet: str, page: int = 2) -> dict[str, Any]:
    """Return one synthetic SDS evidence row (never a public source)."""
    return {
        "track": "sds",
        "source": DEMO_SDS_FILE,
        "page": page,
        "sections": str(page),
        "snippet": snippet,
        "data_label": "Demo / Synthetic SDS",
        "is_demo": True,
    }


def _public_entry(topic: str = "ppe") -> dict[str, Any]:
    """Return one public-source row (explicitly NOT an SDS)."""
    return {
        "track": "public_sources",
        "evidence_id": "demo-public-1",
        "source_title": "NIOSH 公开安全资料（公开来源，非 SDS）",
        "source_url": "https://www.cdc.gov/niosh/",
        "organization": "CDC/NIOSH",
        "snippet": "Public-source safety information, not an SDS.",
        "retrieved_at": "2026-09-01",
        "topic": topic,
    }


def _jsa(
    *,
    work_step: str,
    hazard: str,
    consequence: str,
    controls: str,
    existing: str,
    initial: tuple[int, int],
    residual: tuple[int, int],
    confirmed_by: str = "",
    confirmed_at: str = "",
) -> dict[str, Any]:
    """Return one JSA row; an empty ``confirmed_by`` keeps it an AI draft."""
    return {
        "work_step": work_step,
        "hazard": hazard,
        "consequence": consequence,
        "likelihood": initial[0],
        "severity": initial[1],
        "existing_controls": existing,
        "proposed_controls": controls,
        "residual_likelihood": residual[0],
        "residual_severity": residual[1],
        "confirmed_by": confirmed_by,
        "confirmed_at": confirmed_at,
    }


# --------------------------------------------------------------------------- #
# Permit blueprints
# --------------------------------------------------------------------------- #


def _blueprints(moment: datetime) -> list[dict[str, Any]]:
    """Return the seven simulated permits, covering seven Demo work types."""
    confirmed_at = (moment - timedelta(days=5)).isoformat(timespec="seconds")
    return [
        # ---- 1. the guided golden Demo: HF chemical cleaning ---------------- #
        {
            "permit_id": GOLDEN_PERMIT_ID,
            "title": GOLDEN_PERMIT_TITLE,
            "permit_type": GOLDEN_WORK_TYPE,
            "area": "罐区北侧管道（模拟区域）",
            "chemical": {"chemical_name": "氢氟酸", "aliases": ["HF", "hydrofluoric acid"]},
            "steps": _steps("隔离与排空", "密闭配液", "循环清洗", "中和与废液收集"),
            "controls": "隔离作业区、连续气体监测、应急冲淋与中和物资、双人复核（模拟）",
            "existing": "局部排风、耐酸碱手套、护目镜（模拟）",
            "snippet": "危险性概述：模拟数据，可造成严重皮肤灼伤和眼损伤。",
            "included_hazard": "HF 飞溅与酸雾吸入（模拟）",
            "consequence": "化学灼伤与吸入伤害（模拟）",
            "initial": (4, 5),
            "residual": (4, 5),
            "valid_days": 10,
            "created_days_ago": 5,
            "submitted_days_ago": 5,
            "target": "draft",
            "public_evidence": True,
            "jsa": "draft_and_confirmed",
        },
        # ---- 2. 动火作业: waiting for the approver ------------------------- #
        {
            "permit_id": "PERMIT-DEMO-002",
            "title": "换热器管线焊接维修（模拟）",
            "permit_type": "动火作业",
            "area": "一号线检修区（模拟区域）",
            "chemical": {"chemical_name": "乙炔", "aliases": ["acetylene", "C2H2"]},
            "steps": _steps("动火区隔离", "管线切割与焊接", "焊后检查与清理"),
            "controls": "动火许可、监火人、灭火器材与防火毯、可燃气体检测（模拟）",
            "existing": "防火毯、灭火器与监火人（模拟）",
            "snippet": "危险性概述：模拟数据，易燃气体，与空气混合可形成爆炸性混合物。",
            "included_hazard": "焊接火花引燃可燃物（模拟）",
            "consequence": "火灾与爆炸（模拟）",
            "initial": (4, 4),
            "residual": (3, 4),
            "valid_days": 5,
            "created_days_ago": 4,
            "submitted_days_ago": 4,
            "target": "approval_pending",
            "jsa": "confirmed",
        },
        # ---- 3. 受限空间作业: waiting for EHS review ----------------------- #
        {
            "permit_id": "PERMIT-DEMO-003",
            "title": "储罐内部检查与清理（模拟）",
            "permit_type": "受限空间作业",
            "area": "罐区 2 号储罐（模拟区域）",
            "chemical": {"chemical_name": "硫化氢", "aliases": ["H2S", "hydrogen sulfide"]},
            "steps": _steps("隔离与吹扫置换", "气体检测合格", "入罐检查清理", "出罐清点"),
            "controls": "受限空间许可、连续气体检测、强制通风、监护人全程在场（模拟）",
            "existing": "通风、安全绳与监护（模拟）",
            "snippet": "危险性概述：模拟数据，有毒气体，高浓度可致迅速窒息。",
            "included_hazard": "有毒气体或缺氧（模拟）",
            "consequence": "中毒与窒息（模拟）",
            "initial": (3, 5),
            "residual": (3, 5),
            "valid_days": 7,
            "created_days_ago": 3,
            "submitted_days_ago": 1,
            "target": "ehs_review",
            "jsa": "draft",
        },
        # ---- 4. 电气隔离 / LOTO: approved, waiting for the pre-start check -- #
        {
            "permit_id": "PERMIT-DEMO-004",
            "title": "配电柜检修作业（模拟）",
            "permit_type": "电气隔离作业",
            "area": "配电间 3 号柜（模拟区域）",
            "chemical": {"chemical_name": "六氟化硫", "aliases": ["SF6"]},
            "steps": _steps("停电与验电", "挂牌上锁（LOTO）", "检修作业", "拆除隔离并复电"),
            "controls": "停电验电、挂牌上锁、绝缘工具与专人监护（模拟）",
            "existing": "绝缘工具与验电器（模拟）",
            "snippet": "危险性概述：模拟数据，惰性气体，泄漏可致局部缺氧。",
            "included_hazard": "误送电与电弧伤害（模拟）",
            "consequence": "电击与电弧灼伤（模拟）",
            "initial": (3, 3),
            "residual": (2, 3),
            # 许可已过有效期：一个「已批准但时间窗已失效」的例子，
            # 用来验证「许可过期后不得开工」。
            "valid_days": 2,
            "created_days_ago": 3,
            "submitted_days_ago": 3,
            "target": "approved",
            "jsa": "confirmed",
        },
        # ---- 5. 开挖作业: the applicant's own draft ------------------------ #
        {
            "permit_id": "PERMIT-DEMO-005",
            "title": "厂区地下管线开挖作业（模拟）",
            "permit_type": "开挖作业",
            "area": "厂区西侧管廊（模拟区域）",
            "chemical": {"chemical_name": "甲烷", "aliases": ["methane", "CH4"]},
            "steps": _steps("管线探测与标识", "人工试挖", "机械开挖", "回填与恢复"),
            "controls": "管线探测交底、人工试挖、动土许可与围挡警戒（模拟）",
            "existing": "围挡与警示标识（模拟）",
            "snippet": "危险性概述：模拟数据，易燃气体，泄漏遇火源可致火灾。",
            "included_hazard": "挖破地下管线（模拟）",
            "consequence": "介质泄漏与人身伤害（模拟）",
            "initial": (3, 4),
            "residual": (2, 4),
            "valid_days": 10,
            "created_days_ago": 1,
            "target": "draft",
            "jsa": "draft",
        },
        # ---- 6. 高处作业: executing, with a hazard found on site ----------- #
        {
            "permit_id": "PERMIT-DEMO-006",
            "title": "屋面风机维护作业（模拟）",
            "permit_type": "高处作业",
            "area": "主厂房屋面（模拟区域）",
            "chemical": {"chemical_name": "环氧树脂涂料", "aliases": ["epoxy resin"]},
            "steps": _steps("屋面警戒与临边防护", "风机拆检", "部件更换与紧固", "试运行与清理"),
            "controls": "双钩安全带、生命线与临边防护、工具防坠（模拟）",
            "existing": "安全带、生命线与临边防护（模拟）",
            "snippet": "危险性概述：模拟数据，涂料成分可致皮肤过敏。",
            "included_hazard": "高处坠落与物体打击（模拟）",
            "consequence": "坠落与打击伤害（模拟）",
            "initial": (4, 4),
            "residual": (2, 4),
            "valid_days": 7,
            "created_days_ago": 2,
            "submitted_days_ago": 2,
            "target": "active",
            "jsa": "confirmed",
            "confirmed_at": confirmed_at,
        },
        # ---- 7. 其他高风险作业: executing but paused on site ---------------- #
        {
            "permit_id": "PERMIT-DEMO-007",
            "title": "厂区管廊防腐喷涂作业（模拟）",
            "permit_type": "其他高风险作业",
            "area": "厂区管廊东段（模拟区域）",
            "chemical": {
                "chemical_name": "环氧富锌底漆",
                "aliases": ["epoxy zinc-rich primer"],
            },
            "steps": _steps("管廊警戒与临边防护", "表面除锈处理", "喷涂与固化", "清理与验收"),
            "controls": "双钩安全带与生命线、强制通风、有机气体检测、防火隔离（模拟）",
            "existing": "安全带、防护面罩与局部通风（模拟）",
            "snippet": "危险性概述：模拟数据，含有机溶剂，蒸气可燃并可致呼吸道刺激。",
            "included_hazard": "高处坠落与有机溶剂蒸气暴露（模拟）",
            "consequence": "坠落伤害与吸入中毒（模拟）",
            "initial": (3, 4),
            "residual": (2, 4),
            "valid_days": 10,
            "created_days_ago": 2,
            "submitted_days_ago": 2,
            "target": "suspended",
            "jsa": "confirmed",
            "suspend_reason": "现场风速超过喷涂作业限值，暂停作业待条件恢复（模拟）",
        },
    ]


def _permit_payload(
    blueprint: Mapping[str, Any], *, moment: datetime
) -> dict[str, Any]:
    """Return the ``create_permit`` keyword arguments of one blueprint."""
    chemical = dict(blueprint["chemical"])
    chemical["sds_status"] = "demo_synthetic"

    evidence: list[dict[str, Any]] = [
        _sds_entry(str(blueprint["snippet"]), int(blueprint.get("page", 2))),
    ]
    if blueprint.get("public_evidence"):
        evidence.append(_public_entry())

    created = moment - timedelta(days=int(blueprint["created_days_ago"]))
    confirmed_at = str(
        blueprint.get("confirmed_at") or created.isoformat(timespec="seconds")
    )
    initial = tuple(blueprint["initial"])
    residual = tuple(blueprint["residual"])
    common_jsa = {
        "hazard": str(blueprint["included_hazard"]),
        "consequence": str(blueprint["consequence"]),
        "controls": str(blueprint["controls"]),
        "existing": str(blueprint["existing"]),
        "initial": initial,
        "residual": residual,
    }
    mode = str(blueprint.get("jsa", "confirmed"))
    first_step = str(blueprint["steps"][0]["name"])
    second_step = str(blueprint["steps"][1]["name"])
    third_step = str(blueprint["steps"][-1]["name"])
    if mode == "draft_and_confirmed":
        # One unconfirmed AI draft plus one EHS-confirmed row: the pair is what
        # makes the «AI 草稿 vs EHS 人工确认版» distinction visible.
        jsa_items = [
            _jsa(work_step=first_step, **common_jsa),
            _jsa(
                work_step=second_step,
                confirmed_by=EHS_REVIEWER,
                confirmed_at=confirmed_at,
                **common_jsa,
            ),
        ]
    elif mode == "draft":
        jsa_items = [_jsa(work_step=first_step, **common_jsa)]
    else:
        jsa_items = [
            _jsa(
                work_step=third_step,
                confirmed_by=EHS_REVIEWER,
                confirmed_at=confirmed_at,
                **common_jsa,
            )
        ]

    return {
        "title": str(blueprint["title"]),
        "permit_id": str(blueprint["permit_id"]),
        "permit_type": str(blueprint["permit_type"]),
        "applicant_id": APPLICANT,
        "owner_id": OWNER,
        "ehs_reviewer_id": EHS_REVIEWER,
        "designated_approver_id": APPROVER,
        "area": str(blueprint["area"]),
        "data_label": DEMO_DATA_LABEL,
        "is_demo": True,
        "chemicals": [chemical],
        "steps": list(blueprint["steps"]),
        "evidence": evidence,
        "jsa_items": jsa_items,
        "valid_from": created.isoformat(timespec="seconds"),
        "valid_to": (
            created + timedelta(days=int(blueprint["valid_days"]))
        ).isoformat(timespec="seconds"),
    }


_PRESTART_CHECKS: tuple[dict[str, Any], ...] = (
    {"item_code": "PPE", "item_text": "作业人员资质与防护装备就位", "result": "pass"},
    {"item_code": "AREA", "item_text": "作业区隔离、警戒与监护人就位", "result": "pass"},
    {"item_code": "EMERG", "item_text": "应急物资与通讯手段就位", "result": "pass"},
)


def _seed_permit(
    connection: sqlite3.Connection,
    blueprint: Mapping[str, Any],
    moment: datetime,
) -> None:
    """Seed one permit and drive it to the status the blueprint asks for."""
    permit_id = str(blueprint["permit_id"])
    target = str(blueprint["target"])
    created = moment - timedelta(days=int(blueprint["created_days_ago"]))
    submitted = moment - timedelta(
        days=int(blueprint.get("submitted_days_ago", blueprint["created_days_ago"]))
    )
    reviewed = submitted + timedelta(hours=4)
    approved = reviewed + timedelta(hours=6)
    activated = approved + timedelta(hours=3)
    handed_back = activated + timedelta(days=int(blueprint.get("work_days", 1)))

    permit_service.create_permit(
        connection,
        actor="system",
        now=created,
        **_permit_payload(blueprint, moment=moment),
    )
    if target == "draft":
        return

    permit_service.submit_permit(
        connection, permit_id, actor=APPLICANT, now=submitted
    )
    if target == "ehs_review":
        return

    permit_service.complete_ehs_review(
        connection,
        permit_id,
        actor=EHS_REVIEWER,
        decision="confirm",
        now=reviewed,
    )
    if target == "approval_pending":
        return

    permit_service.decide_approval(
        connection, permit_id, actor=APPROVER, decision="approve", now=approved
    )
    if target == "approved":
        return

    permit_service.confirm_prestart(
        connection,
        permit_id,
        [dict(item) for item in _PRESTART_CHECKS],
        actor=OWNER,
        now=activated,
    )
    if target == "active":
        return

    if target == "suspended":
        permit_service.suspend_permit(
            connection,
            permit_id,
            actor=OWNER,
            reason=str(
                blueprint.get("suspend_reason", "现场条件不满足，暂停作业（模拟）")
            ),
            now=activated + timedelta(hours=2),
        )
        return

    permit_service.complete_work(
        connection,
        permit_id,
        actor=OWNER,
        handback_note="作业完成，现场已交接（模拟）",
        now=handed_back,
    )
    if target == "closeout_review":
        return

    permit_service.close_permit(
        connection,
        permit_id,
        actor=EHS_REVIEWER,
        note="关联隐患已关闭，许可闭环（模拟）",
        now=handed_back + timedelta(hours=5),
    )


# --------------------------------------------------------------------------- #
# Hazards: the exception loop of the execution stage
# --------------------------------------------------------------------------- #

# One hazard per work type family; each is linked to the permit it was found on.
HAZARD_BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "hazard_id": "HZ-DEMO-001",
        "permit_id": "PERMIT-DEMO-002",
        "title": "临时动火区灭火器配置不足（模拟）",
        "hazard_type": "消防",
        "risk_level": "高",
        "created_days_ago": 3,
        "due_days_ago": 2,
        "action_text": "按动火作业要求补齐灭火器材并设置监火人（模拟）",
        "target": "assigned",
    },
    {
        "hazard_id": "HZ-DEMO-002",
        "permit_id": "PERMIT-DEMO-006",
        "title": "高处作业安全带挂点不规范（模拟）",
        "hazard_type": "PPE",
        "risk_level": "高",
        "created_days_ago": 4,
        "due_days_ago": 2,
        "action_text": "增设合格挂点并复核安全带佩戴（模拟）",
        "target": "verification_pending",
        "evidence": {
            "file_name": "（占位）挂点整改与复核记录（模拟）.pdf",
            "evidence_type": "记录",
            "note": "模拟整改证据占位",
        },
    },
    {
        "hazard_id": "HZ-DEMO-003",
        "permit_id": "PERMIT-DEMO-004",
        "title": "LOTO 挂牌记录不完整（模拟）",
        "hazard_type": "电气安全",
        "risk_level": "中",
        "created_days_ago": 6,
        "due_days_ago": 3,
        "action_text": "补齐挂牌上锁记录并与现场逐一核对（模拟）",
        "target": "closed",
        "evidence": {
            "file_name": "（占位）LOTO 挂牌记录（模拟）.xlsx",
            "evidence_type": "记录",
            "note": "模拟整改证据占位",
        },
    },
    {
        "hazard_id": "HZ-DEMO-004",
        "permit_id": "PERMIT-DEMO-003",
        "title": "受限空间气体检测记录缺失（模拟）",
        "hazard_type": "作业现场",
        "risk_level": "高",
        "created_days_ago": 1,
        "due_days_ahead": 3,
        "action_text": "补齐入罐前后气体检测记录并复核检测频次（模拟）",
        "target": "open",
    },
    {
        "hazard_id": "HZ-DEMO-005",
        "permit_id": GOLDEN_PERMIT_ID,
        "title": "HF 作业区应急冲洗设施点检记录缺失（模拟）",
        "hazard_type": "危化品管理",
        "risk_level": "高",
        "created_days_ago": 4,
        "due_days_ago": 1,
        "action_text": "补充点检记录并核对点检频次（模拟）",
        "target": "assigned",
    },
)


def _seed_hazard(
    connection: sqlite3.Connection,
    blueprint: Mapping[str, Any],
    moment: datetime,
) -> None:
    """Seed one hazard and drive it to the status the blueprint asks for."""
    hazard_id = str(blueprint["hazard_id"])
    target = str(blueprint["target"])
    created = moment - timedelta(days=int(blueprint["created_days_ago"]))
    if "due_days_ago" in blueprint:
        due = moment - timedelta(days=int(blueprint["due_days_ago"]))
    else:
        due = moment + timedelta(days=int(blueprint.get("due_days_ahead", 3)))

    hazard_service.create_hazard(
        connection,
        hazard_id=hazard_id,
        permit_id=str(blueprint["permit_id"]),
        title=str(blueprint["title"]),
        description=str(blueprint["title"]),
        hazard_type=str(blueprint["hazard_type"]),
        risk_level=str(blueprint["risk_level"]),
        reported_by_id=OWNER,
        owner_id="",
        corrective_actions=[
            {"action_text": str(blueprint["action_text"]), "owner_id": OWNER}
        ],
        data_label=DEMO_DATA_LABEL,
        is_demo=True,
        actor="system",
        now=created,
    )
    if target == "open":
        return

    hazard_service.assign_hazard(
        connection, hazard_id, owner_id=OWNER, actor=EHS_REVIEWER, now=created
    )
    if target == "assigned":
        return

    hazard_service.assign_hazard_verifier(
        connection, hazard_id, verifier_id=EHS_REVIEWER, actor=EHS_REVIEWER, now=created
    )
    hazard_service.start_rectification(
        connection, hazard_id, actor=OWNER, now=created
    )
    hazard_service.submit_rectification(
        connection,
        hazard_id,
        actor=OWNER,
        evidence=[dict(blueprint["evidence"])],
        notes="已按要求完成整改（模拟）",
        now=moment - timedelta(days=2),
    )
    if target == "verification_pending":
        return

    hazard_service.verify_hazard(
        connection,
        hazard_id,
        actor=EHS_REVIEWER,
        result="pass",
        notes="现场复核通过（模拟）",
        now=moment - timedelta(days=2),
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def seed_demo_data(
    connection: sqlite3.Connection, *, now: datetime | None = None
) -> dict[str, Any]:
    """Seed fixed demo users and records when the store is still empty."""
    persona_service.seed_demo_users(connection, now=now)
    existing = connection.execute(
        "SELECT COUNT(*) AS total FROM permits"
    ).fetchone()
    if existing is not None and int(existing["total"]) > 0:
        return {"seeded": False, "permits": int(existing["total"])}

    moment = now or datetime.now()
    blueprints = _blueprints(moment)
    for blueprint in blueprints:
        _seed_permit(connection, blueprint, moment)
    for blueprint in HAZARD_BLUEPRINTS:
        _seed_hazard(connection, blueprint, moment)

    return {
        "seeded": True,
        "permits": len(blueprints),
        "hazards": len(HAZARD_BLUEPRINTS),
        "work_types": len(WORK_TYPES),
        "golden_permit": GOLDEN_PERMIT_ID,
    }


def work_types_seeded(connection: sqlite3.Connection) -> list[str]:
    """Return the distinct work types actually present in the store."""
    rows = connection.execute(
        "SELECT DISTINCT permit_type FROM permits WHERE permit_type != '' "
        "ORDER BY permit_type"
    ).fetchall()
    return [str(row["permit_type"]) for row in rows]


__all__ = [
    "ADMIN",
    "APPLICANT",
    "APPROVER",
    "DEMO_DATA_LABEL",
    "DEMO_SDS_FILE",
    "EHS_REVIEWER",
    "GOLDEN_PERMIT_ID",
    "GOLDEN_PERMIT_TITLE",
    "GOLDEN_WORK_TYPE",
    "HAZARD_BLUEPRINTS",
    "OWNER",
    "PIPELINE_STAGES",
    "WORK_TYPES",
    "seed_demo_data",
    "work_types_seeded",
]
