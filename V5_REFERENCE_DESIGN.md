# EHS Copilot V5 参考产品与实施设计

> 文档用途：供 coding agent 在现有 Python + Streamlit 项目上实施 V5。  
> 调研快照：2026-09-13。  
> 本文只提取产品结构、数据建模和实现模式，不复制三个参考项目的大段源码，也不构成其安全/合规规则的权威背书。

## 0. 结论先行

V5 不应继续增加并列功能页，而应把现有 SDS、JSA、隐患和 Dashboard 收束为一个可追踪的业务闭环：

**我的待办 → 作业/隐患详情 → 当前阶段主操作 → 人工审批或验证 → 状态流转 → 审计时间线 → Dashboard**

三个参考项目的最佳分工：

| 参考项目 | 主要借鉴 | 不应照搬 |
|---|---|---|
| Frappe Helpdesk | 我的待办、SLA/逾期、详情页信息架构、Activity Timeline、指派 | 工单客服语义、Frappe 框架、Vue UI 代码 |
| SafetyMP | Today 工作台、Permit/CAPA/Approval 的实体关系、RBAC、事务内审计 | 过宽的 EHS 模块版图、Next.js/Postgres 全栈、尚未成熟的 PTW 细节 |
| DefectDojo | Finding 生命周期、整改与验证分离、重新打开、期限化风险接受、风险 Dashboard | 网络安全领域字段、Finding 多布尔状态的复杂组合、庞大 Django 架构 |

V5 的产品主线建议命名为：

**EHS Copilot｜危化品作业许可与隐患闭环工作台**

---

## 1. 调研范围与许可证边界

### 1.1 已核验内容

- Frappe Helpdesk：Agent Home 查询与 Pending Tickets 页面、Ticket 模型、Ticket Activity/Timeline、SLA 模型、Assignment Rule。核验提交 `3ab76d7`，许可证 AGPL-3.0。[1][2][3][4][5]
- SafetyMP：任务优先导航、Permit 详情、CAPA 状态机、Approval 数据模型与串行审批、RBAC、Action Queue、Audit Log。核验提交 `aceb482`，许可证 Apache-2.0。[6][7][8][9][10][11]
- DefectDojo：Finding 模型与操作、Risk Acceptance 模型/流程、对象历史、Dashboard。核验提交 `254dab8`，许可证 BSD-3-Clause。[12][13][14][15][16]

### 1.2 coding agent 必须遵守的许可证规则

1. **Frappe Helpdesk 只借产品/UI/信息架构。** AGPL-3.0 具有强 copyleft 约束，禁止复制 Vue 组件、Python 查询或样式代码到当前 MIT 项目。
2. **SafetyMP 允许参考实现思路，但仍以独立重写为默认。** 若复制 Apache-2.0 代码片段，必须保留适用的版权、许可证和 NOTICE；V5 不需要这样做。
3. **DefectDojo 可参考小型算法/模型思路，但不要搬运 Django 模型、模板或视图。** BSD-3-Clause 仍要求保留版权与许可声明。
4. 新代码应根据本文接口和业务规则从零实现；变量名、页面结构和数据表应贴合 EHS Copilot，而非模仿原仓库。
5. `THIRD_PARTY_NOTICES` 中可写“产品设计参考”及仓库链接，但不要声称三个项目是 V5 依赖；未复制代码时无需把其许可证改成项目主许可证。

---

## 2. Frappe Helpdesk：借“个人工作台 + 工单详情”

### 2.1 我的待办

**原项目怎么设计**  
Agent Home 按当前登录用户读取个性化布局；查询通过工单 `_assign` JSON 与当前用户匹配。Pending Tickets 用 `SLA / Pending / Recent` 三个标签分别展示临近 SLA、等待回复和最近分配的工单，每行显示 ID、主题、状态、优先级、团队和“为什么现在需要处理”。空状态也按标签给出明确反馈。[1][2]

**值得直接借的产品结构**

- 首页首先回答“我今天要做什么”，而不是展示所有模块入口。
- 每条待办必须有 `reason` 和唯一主操作，例如“审批作业”“补充控制措施”“验证整改”。
- 用 `临近到期 / 已逾期 / 新分配` 解释优先级，不只显示一个红色风险标签。
- 待办点击后直接进入实体详情，而非先进入全量列表。

**值得参考的代码思路**

- 后端/服务层根据当前角色和负责人统一生成 Action Queue，不让每个页面各算一遍。
- 待办是多个实体的只读投影，不另建一份会失真的业务数据。
- 每条投影包含稳定的 `entity_type + entity_id + action_type`，用于跳转和去重。

**不适合 V5**

- 客服工单的“待回复”和评价指标不适用于 EHS。
- 不需要让用户自由拖拽首页布局；V5 的目标是讲清主流程。
- Streamlit 演示版不需要实时 WebSocket 更新。

**Python + Streamlit 轻量实现**

- 新建 `workflow/action_queue.py`，提供 `build_action_queue(user_id, role, now) -> list[ActionItem]`。
- `ActionItem` 至少包含：`entity_type`、`entity_id`、`title`、`action_type`、`reason`、`risk_level`、`due_at`、`is_overdue`、`priority_score`、`page`。
- 首页用 `st.tabs(["我的待办", "即将到期", "最近活动"])`；列表按 `priority_score, due_at` 排序。
- 使用 `st.query_params` 保存 `page`、`entity_type`、`entity_id`，点击后打开详情。

### 2.2 SLA / 逾期

**原项目怎么设计**  
Helpdesk 按优先级配置首次响应与解决目标，校验响应目标不能晚于解决目标；支持工作日/服务时间、暂停状态和恢复计时。Ticket 持久化 `response_by`、`resolution_by`、耗时及是否失败，Agent Home 对已逾期、1 小时内、2 小时内使用不同提示。[2][4]

**值得直接借的产品结构**

- 将“风险”与“时间紧迫性”分开：高风险不一定逾期，低风险也可能已逾期。
- 列表显示具体截止时间与原因：`距审批截止 3 小时`，不要只写 `SLA告警`。
- 暂停必须是显式状态，并记录暂停原因与时间；否则时钟不能悄悄停止。

**值得参考的代码思路**

- 截止时间在任务创建/状态进入时计算并持久化，不在页面加载时随意重算。
- SLA 规则集中在配置和纯函数中，页面只读取结果。
- 对规则变更影响未完成记录给出警告，避免历史工单被静默改期。

**不适合 V5**

- 首次响应/解决时间是客服语义；V5 应改成审批、整改、复查三个时钟。
- 周末/班次/节假日工作时钟在作品集 MVP 中成本过高；先用自然日并明确规则。

**轻量实现**

- `workflow/sla.py`：`calculate_due_at(event_at, rule_days)`、`classify_deadline(due_at, now)`。
- `sla_rules` 用 Python 常量或 SQLite 配置表：如重大风险审批 0 天、高风险 1 天、中风险 3 天；Demo 中声明为模拟规则。
- 实体持久化 `approval_due_at`、`rectification_due_at`、`verification_due_at`；状态变化时写入，不回溯重算。

### 2.3 Ticket 详情页

**原项目怎么设计**  
Ticket 详情围绕一个工单聚合标题、状态、优先级、团队、SLA、联系人、活动、邮件、评论和分析；详情与沟通区在同一上下文中，而不是跳转多个模块。[3][5]

**值得直接借的产品结构**

- 详情页成为单一事实入口：作业基本信息、SDS 证据、JSA、审批、执行检查、关联隐患、时间线均围绕同一个 `permit_id`。
- 首屏只显示状态、风险、负责人、截止日期、当前阶段主操作；长证据和历史放下方折叠区。
- 次要操作放菜单或次级按钮，避免页面同时出现十几个同等按钮。

**值得参考的代码思路**

- 详情页通过一个聚合查询/服务返回 `record + relations + available_actions + timeline`。
- 保存后刷新单个实体，不全局重置 session state。

**不适合 V5**

- 邮件、电话、客户联系人、客服评价和可配置工单字段均无需实现。

**轻量实现**

- `services/permit_service.py:get_permit_detail(permit_id, actor)` 返回统一 `PermitDetailView`。
- Streamlit 首屏用 4 个紧凑字段列；下方依次 `作业信息 / SDS证据 / JSA / 审批 / 执行与交接 / 关联隐患 / 活动记录`。
- 不用多个独立页面维护同一数据；原 SDS/JSA 功能变成详情页内的子能力，同时保留独立工具页作为辅助入口。

### 2.4 Activity Timeline

**原项目怎么设计**  
Helpdesk 的 Activity Timeline 聚合版本变更、评论、邮件和电话；版本日志只保留状态、优先级、团队、类型、SLA 等有业务价值的字段变化，避免把所有字段改动都变成噪声。活动页还可按 Activity、Emails、Comments、Calls、Analytics 分类。[3]

**值得直接借的产品结构**

- 时间线是事实历史，不是可编辑的“备注列表”。
- 仅把重要状态、责任人、期限、审批、证据和复查结果展示为主事件。
- 评论和系统事件视觉区分，但按时间统一排序。

**值得参考的代码思路**

- Timeline 是 `audit_events` 的投影；业务表更新和审计写入同一事务。
- 使用事件类型映射生成自然语言，例如 `permit.submitted → 张三提交作业审批`。

**不适合 V5**

- 不需要邮件/电话子系统，也不需要深链接到每条沟通。
- 不应照搬已废弃的 `HD Ticket Activity` doctype；以当前聚合时间线模式为准。[5]

**轻量实现**

- SQLite 表 `audit_events` 只追加不覆盖。
- `workflow/audit.py:record_event(...)` 与业务写操作共用同一数据库事务。
- 详情页用 `st.container(border=True)` 逐条显示时间、操作者、动作、原因；默认 10 条，可展开全部。

### 2.5 Assignment

**原项目怎么设计**  
Helpdesk 使用 Frappe 的 Assignment Rule 和 ToDo，将 Ticket 分给 agent，并按团队约束分配；当工单团队变化且原 agent 不属于新团队时会清理无效分配。[1][5]

**值得直接借的产品结构**

- “负责人”必须是工作责任，不只是文本字段；负责人变化应同步改变其个人待办。
- 分配动作要记录谁在何时把什么任务分给谁。
- 角色/组织范围与可选负责人联动，避免把 EHS 审批分给无权限人员。

**值得参考的代码思路**

- assignment 通过服务层统一执行，校验角色、记录审计、刷新行动队列。

**不适合 V5**

- 不需要自动轮询、负载均衡或复杂团队路由。
- Demo 用户可采用固定模拟人员；不要伪装成真实企业通讯录。

**轻量实现**

- `users` 表预置 4 个 Demo 角色；业务表保存 `owner_id`。
- `assign_owner(entity_type, entity_id, owner_id, actor, reason)` 校验权限后更新并写审计。
- 负责人选择使用 `st.selectbox`，显示姓名 + 角色；Demo 顶部允许切换“当前身份”。

### 2.6 当前阶段主要操作

**原项目怎么设计**  
Helpdesk 并非完整状态机产品，但详情页会根据工单状态和用户角色突出回复、状态处理等上下文动作；首页也通过 Reason 把用户带到当前最需要的工作。[2][3]

**值得直接借的产品结构**

- 每个状态只突出 1 个主操作和最多 2 个次操作。
- 操作文案必须是业务动作，如“提交EHS审核”“确认开工条件”“提交整改证据”，而不是笼统“下一步”。

**值得参考的代码思路**

- 页面不自己判断按钮；由状态机返回 `available_actions`。

**不适合 V5**

- 不应通过 CSS 隐藏无权限按钮代替后端校验。

**轻量实现**

- `workflow/actions.py:get_available_actions(entity, actor_role) -> list[Action]`。
- `Action` 包含 `code`、`label`、`kind(primary/secondary/danger)`、`requires_reason`、`requires_confirmation`。
- UI 按返回结果渲染，服务层再次校验，防止绕过页面直接修改状态。

---

## 3. SafetyMP：借“EHS 实体骨架 + 人工门控”

### 3.1 EHS 首页 / Today

**原项目怎么设计**  
SafetyMP 将主导航按任务语义分成 `Today / Capture / Decide / Prove`：Today 放 Command Center 与 Tasks & Reviews；Capture 放 Incident、Observation、Inspection、Permit；Decide 放 Approval 和 CAPA；Prove 放 Audit Trail 和 Documents。Action Queue 将审批、CAPA 等不同实体转成统一待办，并按逾期和类型评分。[6][9]

**值得直接借的产品结构**

- 导航按工作目的组织，而不是按代码模块组织。
- Today 页面同时展示“我的下一步”和少量风险摘要；Dashboard 不替代行动队列。
- 同一事项不要既出现在 KPI 告警又重复占据待办首位。

**值得参考的代码思路**

- `collectActionQueueItems` 从不同实体查询待办，再统一评分和链接。
- 权限过滤发生在队列生成阶段，用户只看到有权处理的任务。

**不适合 V5**

- SafetyMP 的导航范围过大。V5 不做培训、承包商、管理评审、环境许可等全套 EHS 平台。

**轻量实现**

- 导航只保留：`今日工作台 / 作业许可 / 隐患与整改 / SDS知识库 / 风险看板 / 审计记录`。
- Today 上半区展示 3 个指标：我的待办、已逾期、高/重大风险；下半区展示按优先级排序的行动队列。

### 3.2 Permit

**原项目怎么设计**  
Work Permit 模型包含组织、场所、类型、状态、有效期、工作摘要、危害与控制、申请人及审批信息；状态为 draft、pending_approval、active、rejected、completed、cancelled、expired。详情页顶部提示当前审批人，审批动作就地出现；表单只在 draft/pending 阶段可编辑，激活后只显示完成、提前到期或取消等阶段动作。[7][8]

**值得直接借的产品结构**

- Permit 是聚合根：SDS、JSA、审批、执行与隐患都挂在同一作业记录下。
- 有效期是核心字段，批准不能无限期生效。
- “编辑资料”和“改变状态”是两类操作，必须分开。

**值得参考的代码思路**

- 显式状态转换表；审批通过才能把 pending 变成 active，普通更新接口无权激活。
- 详情页根据状态锁定字段，降低审批后被静默改写的风险。

**不适合 V5**

- SafetyMP 当前 Permit 只用一块自由文本表示 hazards & controls，不能直接当作工业级作业许可模板。
- V5 不应宣称覆盖所有特殊作业；先聚焦“危化品非例行作业/异常处置 Demo”。

**轻量实现**

- `permits` 表保存基本信息；`permit_chemicals`、`permit_sds_evidence`、`jsa_items`、`prestart_checks` 分表关联。
- Permit 详情首屏显示 `status / risk_level / owner / valid_to / current_action`。
- 审批后核心字段只读；如需变更，执行“退回修改”并产生新版本/审计事件。

### 3.3 CAPA

**原项目怎么设计**  
CAPA 状态机是 `pending_approval → planned → in_progress → completed → verified`；详情页显示来源、负责人、期限、状态步进器、证据和当前工作流操作。完成到验证时必须填写验证说明，负责人可重新指派。[8][10]

**值得直接借的产品结构**

- “整改完成”和“验证有效”必须分成两个状态，执行者不能用一个按钮自证关闭。
- CAPA 保留来源关系：来自事故、巡检、作业或审计。
- 验证说明/证据是关闭门槛。

**值得参考的代码思路**

- 纯函数状态表统一约束转换，测试覆盖所有允许/禁止路径。
- 详情服务同时返回 `sources` 和 `hasOpenApproval`，避免页面自行拼接。

**不适合 V5**

- V5 现有“隐患”不必再创建一套独立 CAPA 大模块；可以将整改措施作为 Hazard 的子实体。

**轻量实现**

- Hazard 主状态：`open → assigned → in_progress → verification_pending → closed`；`closed → reopened`。
- `corrective_actions` 子表允许一个隐患有多项措施；V5 演示可先限制 1–3 项。
- 进入 `closed` 前要求 EHS reviewer 填 `verification_result` 和证据/说明。

### 3.4 Approval

**原项目怎么设计**  
SafetyMP 用通用 `approval_request(entityType, entityId, status)` 与串行 `approval_step(stepOrder, approverUserId, status, comment, decidedAt, dueAt)`，而不是给每类实体各建审批表。只有当前 pending step 的指定审批人且具备实体对应权限时才能决定；最终批准再触发父实体状态变化，拒绝也有明确回退/终态规则。[7]

**值得直接借的产品结构**

- 审批是独立记录，不是 Permit 上两个布尔值。
- 申请、每一步决策、评论、到期时间均可追溯。
- AI 只能生成草稿或风险提示，不能替人点击批准。

**值得参考的代码思路**

- 通用审批表 + 实体类型/ID；提交、决定、父实体更新和审计在同一事务。
- 用 `current_step_order` 或查询最小 pending step，确保串行审批。

**不适合 V5**

- 两天版只做单步 EHS 审批；不要立即做并行会签、动态 BPMN 和复杂加签。

**轻量实现**

- 仍采用通用 `approval_requests`、`approval_steps`，但 UI 只创建 1 个步骤。
- `decide_approval(request_id, actor, decision, comment)` 校验指定审批人、状态和权限；事务内更新 step、request、permit、audit。
- 拒绝必须有原因；批准重大风险时可要求二次确认，但不要自动增加虚构审批层级。

### 3.5 RBAC

**原项目怎么设计**  
SafetyMP 定义细粒度 permission keys，如 `work_permit:create/update/approve`、`capa:create/update/approve`、`audit_trail:read`；服务端通过 `assertPermission` 查询用户在组织中的角色权限，Demo 只读模式在 mutation 层阻断写操作。[10][11]

**值得直接借的产品结构**

- 角色决定“可见数据 + 可执行动作”，而不只是换一个页面标题。
- 演示环境与生产权限概念分开，明确“模拟身份切换”。

**值得参考的代码思路**

- `PERMISSIONS` 常量 + `ROLE_PERMISSIONS` 映射 + 服务层断言。
- 每个 mutation 首先校验权限和实体状态。

**不适合 V5**

- 不需要组织多租户、SSO、真实登录或数据库级行权限。

**轻量实现**

- 固定角色：`operator/applicant`、`ehs_reviewer`、`approver`、`action_owner`、`demo_admin`。
- `workflow/permissions.py:assert_can(actor, action, entity)`；所有按钮显示与服务写入都调用同一规则。
- 页面顶部注明“当前为 Demo 身份”，避免让面试官误认为已有企业账号体系。

### 3.6 Audit Log

**原项目怎么设计**  
SafetyMP 的 `audit_log` 保存组织、actor、action、entityType、entityId、payload、createdAt；`writeAuditLog` 接受数据库事务对象，因此业务写入与审计可在同一事务完成。审计页面按实体、动作、时间、操作者筛选并可导出 CSV，导出行为本身也被记录。[9][11]

**值得直接借的产品结构**

- 审计日志回答“谁、何时、对什么、做了什么、为什么、从什么变成什么”。
- 时间线服务业务详情，审计页服务追溯；两者可来自同一事件表。
- 审计记录不可在普通 UI 中编辑或删除。

**值得参考的代码思路**

- 事务中写 audit；实体详情显示过滤后的事件，审计页显示全量字段。
- 结构化 `before_json/after_json` 比一条自由文本更可验证。

**不适合 V5**

- 不宣称不可篡改、电子签名或法规级审计；SQLite 原型只能称“追加式操作记录”。

**轻量实现**

- 表字段见第 6 节；普通服务不提供 update/delete audit API。
- 审计页支持实体类型、动作、操作者、日期筛选与 CSV 下载。

### 3.7 实体之间关系

**原项目怎么设计**  
SafetyMP 将 Incident、Audit Finding、CAPA、Permit、Approval、Evidence、Site、User 等建成独立实体，以外键或 `entityType + entityId` 建立来源和审批关系。重大审计发现可在同一事务创建待审批 CAPA；CAPA 详情展示来源实体。[7][8][11]

**值得直接借的产品结构**

- 不把一次危化品作业的所有内容塞进一条 JSON。
- Permit 与 Hazard 是多对多业务关系中的简化一对多：一次作业可产生多个隐患；一个隐患保留 `source_type/source_id`。
- Evidence 是独立记录，必须知道关联实体、来源、创建人和时间。

**值得参考的代码思路**

- 强关系用外键；跨实体通用关系（审批、审计）用 `entity_type + entity_id`。

**不适合 V5**

- 不需要 SafetyMP 的全部 ISO/环境/承包商实体；模型越多不等于企业感越强。

**轻量实现**

```text
Permit 1 ── n JSAItem
Permit 1 ── n SDSEvidence
Permit 1 ── n PreStartCheck
Permit 1 ── n Hazard
Permit 1 ── n ApprovalRequest ── n ApprovalStep
Hazard 1 ── n CorrectiveAction
Hazard 1 ── n Evidence
Any Entity 1 ── n AuditEvent
User 1 ── n Owned Permit / Hazard / ApprovalStep
```

---

## 4. DefectDojo：借“发现—整改—验证—例外”

### 4.1 Finding 状态

**原项目怎么设计**  
DefectDojo 的 Finding 以多个布尔字段组合表达 Active、Verified、False Positive、Duplicate、Out of Scope、Under Review、Risk Accepted、Is Mitigated 等状态，并通过校验禁止冲突组合，例如 Duplicate 不能同时 Active/Verified，Active 不能同时 Risk Accepted。[12][13]

**值得直接借的产品结构**

- 隐患除了开/关，还需要“待验证”“风险接受”“重新打开”等明确业务含义。
- 状态组合必须有不变量，不能出现“已关闭但仍整改中”。

**值得参考的代码思路**

- 写入前集中校验状态不变量；每种变更必须经过命令函数。

**不适合 V5**

- 多布尔状态容易出现组合爆炸。V5 应使用单一主状态 + 单独的 Risk Acceptance 实体，而非复制 Finding 模型。

**轻量实现**

- Hazard 单状态枚举；`validate_hazard_transition(from, to)` 纯函数。
- `risk_acceptance` 不作为永久主状态；有效接受时显示 badge 并暂停/调整处理逻辑，过期后回到待处理。

### 4.2 负责人

**原项目怎么设计**  
DefectDojo OS 的 Finding 模型有 reporter、reviewers、mitigated_by、last_reviewed_by 等责任痕迹，但当前源码没有一个通用 `assigned_to` 字段；风险接受有明确 owner。也就是说，它强在“谁报告/谁验证/谁接受风险”，不等同于完整整改责任人模型。[12][14]

**值得直接借的产品结构**

- 不要把“发现人、整改负责人、验证人、风险接受人”混成一个责任人。

**值得参考的代码思路**

- 不同动作持久化 actor；验证人与整改人分开。

**不适合 V5**

- 不应声称 DefectDojo 已提供可直接复刻的整改 owner 模型。

**轻量实现**

- Hazard 保存 `reported_by_id`、`owner_id`；关闭时保存 `verified_by_id`；RiskAcceptance 保存 `owner_id/approved_by_id`。
- 规则：`verified_by_id != owner_id`（Demo 默认要求职责分离；管理员可解释性覆盖但必须记录原因）。

### 4.3 截止日期

**原项目怎么设计**  
Finding 有 `planned_remediation_date`、SLA 起始/到期字段；风险接受有 expiration_date，并可在到期时重新激活 Finding、选择是否重启 SLA。[12][14][15]

**值得直接借的产品结构**

- 整改截止日期与风险接受到期日期是两件事。
- 风险接受不能永久消失于指标，应可查询、可审计、可到期复审。

**值得参考的代码思路**

- 到期判断基于持久化日期；周期任务/页面加载只产生到期事件，不篡改历史时间。

**不适合 V5**

- Streamlit Cloud 不适合依赖常驻 cron。两天版可在每次访问时幂等执行 `process_expirations(now)`。

**轻量实现**

- `hazards.due_at`、`risk_acceptances.expires_at` 分开。
- `process_expired_acceptances()` 仅处理未标记过期的记录，写 `expired_at` 和审计，再将 Hazard 恢复为 open/assigned。

### 4.4 整改

**原项目怎么设计**  
Finding 记录 mitigation 文本、planned remediation date/version 和修复工作量；关闭动作要求说明/Note，并记录 mitigated、mitigated_by、is_mitigated。[12][13]

**值得直接借的产品结构**

- 整改方案、预计日期、实际完成时间、完成证据要分开。
- 关闭必须有理由和责任痕迹。

**值得参考的代码思路**

- `close_finding` 是受控命令而非任意勾选 active=false。

**不适合 V5**

- 不需要漏洞版本、CWE、Jira 同步和扫描器导入语义。

**轻量实现**

- `corrective_actions` 保存 measure、owner、due、status、completed_at。
- `submit_rectification()` 只能进入 `verification_pending`，不能直接 closed。

### 4.5 验证

**原项目怎么设计**  
DefectDojo 将 Verified 与 Mitigated 分开；可以请求 peer review，指定 reviewers，验证动作记录说明、last_reviewed 和执行人。[12][13]

**值得直接借的产品结构**

- “措施已做”不是“风险已消除”；复查需要结论和证据。
- 复查人、复查时间和失败原因必须留痕。

**值得参考的代码思路**

- `verify` 是单独动作；状态校验和审计集中处理。

**不适合 V5**

- 不做完整 peer-review 通知系统。

**轻量实现**

- `verify_hazard(hazard_id, result, notes, evidence, actor)`：通过→closed；不通过→reopened/assigned，并创建审计事件。
- UI 只有 EHS reviewer 能看到“验证关闭/退回整改”。

### 4.6 重新打开

**原项目怎么设计**  
DefectDojo 的 reopen 会恢复 Active、清除 mitigated 状态与旧 review 上下文、解除风险接受，并同步相关状态/通知；不是简单把一个布尔值改回去。[13]

**值得直接借的产品结构**

- 重新打开必须说明原因，并保留此前关闭历史。
- 重开后创建新的行动队列项，而不是覆盖旧关闭时间。

**值得参考的代码思路**

- 用显式 `reopen` 命令完成一组一致性更新。

**不适合 V5**

- 不做 Jira/GitHub 外部问题同步。

**轻量实现**

- `reopen_hazard(..., reason)`：状态设为 reopened/assigned、清除当前验证通过标记但保留历史事件，重新设置 due_at 和 owner。

### 4.7 风险接受

**原项目怎么设计**  
Full Risk Acceptance 可聚合多个 Findings，记录安全团队建议、风险所有者决策、补偿控制、接受人、证明、owner、到期日期；到期可重新激活 Findings、选择是否重启 SLA。Simple Risk Acceptance 更轻，但文档建议一个组织统一使用一种路径，避免来源不清。[14][15]

**值得直接借的产品结构**

- 风险接受不是“忽略”；必须有理由、补偿措施、批准人、期限和复审。
- 风险接受仍然可查询和审计，只是在当前指标中单独统计。

**值得参考的代码思路**

- 独立 RiskAcceptance 实体关联 Hazard；到期处理幂等；接受/取消/到期都是审计事件。

**不适合 V5**

- 不做批量跨任务风险接受；不宣称符合任何具体法规。
- 高/重大风险默认不允许普通角色接受；具体企业规则应配置，不由 AI 决定。

**轻量实现**

- 只实现“单隐患风险接受申请”：`rationale`、`compensating_controls`、`owner_id`、`approved_by_id`、`expires_at`、`status`。
- 重大风险隐藏/禁用接受按钮；高风险必须 approver 批准；规则明确标为 Demo policy。

### 4.8 Dashboard

**原项目怎么设计**  
DefectDojo 首页把近期新增、近期关闭、近期风险接受等摘要卡与按严重度的历史/趋势图结合；卡片可下钻到过滤列表。它同时区分开放、关闭和风险接受，不把风险接受当“已修复”。[16]

**值得直接借的产品结构**

- 指标必须可下钻到记录列表。
- 数量、趋势、严重度、逾期和例外状态分开。
- Dashboard 用于管理判断，Today 用于个人行动，两者不可混为一页大杂烩。

**值得参考的代码思路**

- 所有图表由业务表查询聚合，不存第二份 Dashboard 数据。

**不适合 V5**

- 不需要安全扫描趋势、产品漏洞排行榜和大量图表。

**轻量实现**

- KPI：进行中作业、待我审批、逾期整改、待验证、有效风险接受。
- 图表：隐患风险分布、状态分布、类型分布、7/30 天关闭趋势。
- 重点列表：高/重大且未关闭、即将到期许可、逾期整改；每项可打开详情。

---

## 5. V5 产品结构

### 5.1 用户与核心场景

**核心用户**

- 作业申请人/班组人员：发起作业，补充信息，执行检查，提交整改证据。
- EHS 审核人：检查 SDS/JSA、退回修改、验证整改。
- 作业批准人/现场负责人：在证据完整后批准或拒绝。
- 整改负责人：执行措施并提交完成证据。
- 管理者：查看风险与逾期，不直接替代专业判断。

**V5 唯一主线 Demo**

> 模拟：在指定区域开展含 HF 体系的非例行清洗/处置作业。系统从 SDS 证据和 JSA 草稿开始，经 EHS 人工审核、作业批准、开工检查、发现隐患、整改、复查到关闭。

必须始终标注：Synthetic/Demo 数据，不构成真实 SOP 或作业许可。

### 5.2 导航

```text
今日工作台       我的待办、逾期、最近活动
作业许可         列表、新建、详情与完整阶段
隐患与整改       列表、详情、整改、验证、风险接受
SDS 知识库       上传/演示数据、检索、来源追溯
风险看板         聚合指标与下钻
审计记录         操作日志筛选与导出
```

JSA 不再作为顶层孤立导航；它属于作业许可详情。旧 JSA 页面可保留兼容入口，但不作为 V5 演示主路径。

### 5.3 作业许可状态机

```text
draft
  └─ submit → ehs_review
ehs_review
  ├─ return → draft
  └─ confirm → approval_pending
approval_pending
  ├─ reject → returned
  └─ approve → approved
approved
  └─ prestart_confirm → active
active
  ├─ suspend → suspended
  └─ complete_work → closeout_review
suspended
  └─ resume → active
closeout_review
  ├─ return → active
  └─ close → closed

任何未激活状态可按规则 cancel；超过 valid_to 可 expired。
```

**硬门槛**

- 进入 `ehs_review`：至少 1 个化学品、1 条 SDS 证据、1 条 JSA。
- 进入 `approval_pending`：EHS 已确认，残余风险字段完整；高/重大风险必须有明确控制说明。
- 进入 `approved`：指定审批人完成批准；AI 无权批准。
- 进入 `active`：开工检查全部通过，作业仍在有效期内。
- 进入 `closed`：作业已完成、现场交接完成、关联重大未关闭隐患为 0。

### 5.4 隐患状态机

```text
open → assigned → in_progress → verification_pending → closed
                                   └─ fail → reopened → assigned
closed ── reopen(reason required) → reopened
```

- `risk_acceptance` 是关联记录，不替代 closed。
- `verification_pending` 后，整改负责人不可自行关闭。

---

## 6. SQLite 数据模型

### 6.1 核心表

| 表 | 关键字段 | 说明 |
|---|---|---|
| `users` | id, display_name, demo_role, active | 仅 Demo 身份，不做真实认证 |
| `permits` | id, title, permit_type, site, area, equipment, applicant_id, owner_id, status, risk_level, valid_from, valid_to, approval_due_at, version, created_at, updated_at | 作业聚合根 |
| `permit_chemicals` | id, permit_id, chemical_name, sds_document_id | 化学品清单 |
| `sds_evidence` | id, permit_id, document_name, page, section, snippet, source_hash, captured_at | 固化当时引用证据，不只保留检索 query |
| `jsa_items` | id, permit_id, step_no, work_step, hazard, consequence, initial_l, initial_s, initial_r, existing_controls, proposed_controls, residual_l, residual_s, residual_r, confirmed_by, confirmed_at | 沿用统一 R=L×S 逻辑 |
| `approval_requests` | id, entity_type, entity_id, status, submitted_by, submitted_at, decided_at | 通用审批头 |
| `approval_steps` | id, request_id, step_order, approver_id, status, due_at, comment, decided_at | 两天版 UI 只生成一步 |
| `prestart_checks` | id, permit_id, item_code, item_text, required, result, checked_by, checked_at, evidence_note | 开工前硬门槛 |
| `hazards` | id, source_type, source_id, permit_id, title, hazard_type, risk_level, description, reported_by_id, owner_id, status, due_at, verification_due_at, created_at, updated_at | 隐患主记录 |
| `corrective_actions` | id, hazard_id, action_text, owner_id, due_at, status, completed_at | 整改措施 |
| `evidence_files` | id, entity_type, entity_id, file_name, mime_type, storage_path, note, uploaded_by, uploaded_at | Demo 可先保存元数据/受控目录 |
| `risk_acceptances` | id, hazard_id, rationale, compensating_controls, owner_id, approved_by_id, status, expires_at, expired_at, created_at | 有期限的例外流程 |
| `audit_events` | id, correlation_id, entity_type, entity_id, action, actor_id, from_state, to_state, reason, before_json, after_json, created_at | 追加式操作记录 |

### 6.2 数据一致性约束

- `initial_r = initial_l * initial_s`，`residual_r = residual_l * residual_s`；存储前和读取测试均校验。
- `valid_to > valid_from`。
- `closed` Hazard 必须有最近一次成功验证事件。
- `approved/active` Permit 必须有 approved ApprovalRequest。
- 一个实体同一时刻最多一个 open ApprovalRequest。
- 审计事件不允许从 UI 删除或编辑。
- 所有写操作使用 `with connection:` 事务；业务更新与审计写入要么同时成功，要么同时回滚。

---

## 7. 实施接口与文件结构

```text
app.py
pages/
  today_page.py
  permit_list_page.py
  permit_detail_page.py
  hazard_list_page.py
  hazard_detail_page.py
  sds_page.py
  dashboard_page.py
  audit_page.py
db.py
schema.py
services/
  permit_service.py
  hazard_service.py
  approval_service.py
  evidence_service.py
  ai_assist_service.py
workflow/
  permit_state.py
  hazard_state.py
  permissions.py
  actions.py
  action_queue.py
  sla.py
  audit.py
tests/
  test_permit_state.py
  test_hazard_state.py
  test_permissions.py
  test_action_queue.py
  test_sla.py
  test_audit_atomicity.py
  test_approval.py
  test_detail_aggregation.py
```

### 7.1 关键纯函数

```python
validate_permit_transition(from_status, action, actor_role, context) -> TransitionResult
validate_hazard_transition(from_status, action, actor_role, context) -> TransitionResult
get_available_actions(entity_type, record, actor) -> list[Action]
calculate_deadline_status(due_at, now) -> DeadlineStatus
build_action_queue(actor, now) -> list[ActionItem]
calculate_risk_level(likelihood, severity) -> str
```

### 7.2 关键命令服务

```python
submit_permit(permit_id, actor)
complete_ehs_review(permit_id, actor, decision, reason)
decide_approval(request_id, actor, decision, comment)
confirm_prestart(permit_id, actor, checks)
complete_work(permit_id, actor, handback_note)
create_hazard(source, payload, actor)
assign_hazard(hazard_id, owner_id, actor, reason)
submit_rectification(hazard_id, actions, evidence, actor)
verify_hazard(hazard_id, result, notes, actor)
reopen_hazard(hazard_id, reason, actor)
request_risk_acceptance(hazard_id, payload, actor)
```

每个命令必须：**校验权限 → 校验当前状态与必填证据 → 执行业务写入 → 写审计 → 返回更新后的详情**。

---

## 8. 页面实施规格

### 8.1 今日工作台

1. 顶部说明：`你当前以“EHS审核人（Demo）”身份查看`。
2. 指标：我的待办、逾期、今日到期、高/重大风险。
3. 待办标签：`全部 / 待审批 / 待整改 / 待验证 / 即将到期`。
4. 每行：风险、标题、原因、负责人、截止时间、主操作。
5. 最近活动最多 8 条；不在首页堆完整 Dashboard。

### 8.2 Permit 详情

```text
[返回作业列表]
标题                         [状态] [风险]
负责人 / 有效期 / 当前阶段

[当前需要你做什么]
简短原因 + 主按钮 + 次按钮

基本信息
化学品与SDS证据
JSA风险评估
审批记录
开工前检查
执行与交接
关联隐患
活动时间线
```

### 8.3 Hazard 详情

```text
标题 + 风险 + 状态 + 是否逾期
来源作业 / 发现人 / 整改负责人 / 截止日期
[当前阶段主操作]
隐患描述
整改措施与完成证据
验证记录
风险接受（如有）
活动时间线
```

### 8.4 Dashboard

- 所有卡片和图表可下钻到带过滤条件的列表。
- 默认管理视角，不展示“模型准确率”等技术指标；技术评测放 GitHub README/Evals 页面。
- 把 `风险接受` 单列，绝不能计入 `已整改关闭`。

---

## 9. AI 在 V5 中的边界

### 9.1 可以做

- 根据任务描述建议需要查询的 SDS 章节。
- 从已检索证据中生成 PPE、泄漏、急救要求摘要，并逐条绑定文件/页码/片段。
- 基于用户输入和 SDS 证据生成 JSA **草稿**。
- 建议隐患描述、整改措施草稿和缺失字段提示。
- 对当前流程给出“下一步建议”，但实际可执行动作仍由状态机决定。

### 9.2 不可以做

- 自动批准作业、自动接受风险、自动验证整改有效。
- 没有证据时生成安全要求。
- 修改 L×S 的人工输入或企业风险矩阵。
- 绕过权限、状态、有效期和必填检查。
- 直接写数据库；AI 输出先进入预览，由用户确认后调用受控命令服务。

### 9.3 Guardrail

- 引用覆盖：每条关键安全结论必须有 `evidence_id`；无证据则显示“知识库中未找到”。
- 风险门槛：高/重大残余风险不允许 AI 建议“可直接开工”。
- 人工门槛：审批、验证、风险接受始终显示操作者确认框和结果预览。
- Prompt 与业务规则分离：规则由 Python 状态机强制，不依赖模型遵守提示词。

---

## 10. 测试与验收标准

### 10.1 必测状态与权限

- 非法状态跳转全部失败，例如 draft 不能直接 active。
- 普通申请人不能审批；整改负责人不能验证自己提交的整改。
- Approval 只有当前指定审批人可决定；重复决定失败。
- 重大未关闭隐患存在时，Permit 不能 closed。
- 风险接受到期后幂等重开，不重复写事件。

### 10.2 必测 SLA / Queue

- 逾期项排在未逾期项前；高风险不是唯一排序因素。
- 同一实体/动作不重复出现在队列。
- 改负责人后旧负责人待办消失，新负责人出现。
- 完成动作后队列立即刷新。

### 10.3 必测审计

- 每个 mutation 产生一条结构化审计事件。
- 业务写入失败时不留下孤立审计；审计失败时业务写入回滚。
- before/after、actor、reason、timestamp 可查询。

### 10.4 必测 AI 边界

- 无检索证据不生成具体 PPE/急救结论。
- AI 草稿不会自动改变状态。
- 引用文件、页码和片段来自真实检索结果。

### 10.5 端到端 Demo 验收

1. 申请人创建含 HF 的模拟非例行作业。
2. 选择 Synthetic SDS，生成可追溯证据和 JSA 草稿。
3. EHS 审核人查看缺失项，退回或确认。
4. 批准人从“我的待办”批准。
5. 申请人完成开工检查后激活作业。
6. 执行中创建“PPE 配置不完整”模拟隐患。
7. 指派整改负责人、设置期限并提交证据。
8. EHS 复查，不通过则重开；通过则关闭。
9. Permit 完成交接后关闭。
10. Dashboard 与 Timeline 同步反映全部变化。

---

## 11. 实施优先级

### P0：先做成企业工作流

1. SQLite 持久化与 V4 Demo 数据迁移/种子脚本。
2. Permit/Hazard 显式状态机与服务层命令。
3. Today 我的待办 + SLA/逾期 + 角色过滤。
4. Permit/Hazard 详情页的当前阶段主操作。
5. Approval、整改验证和追加式 Audit Timeline。

### P1：完成可演示闭环

1. 风险接受及到期复审。
2. Dashboard 下钻与 7/30 天趋势。
3. AI 草稿预览、证据绑定和人工确认。

### 本轮不要做

- 多 Agent、Supervisor Agent、MCP。
- BPMN/通用流程设计器。
- 真实登录、SSO、多租户和企业通讯录。
- 自动邮件/短信/企业微信通知；先只做站内逾期提示。
- 全行业特殊作业模板、法规合规宣称、电子签名。

---

## 12. 给 coding agent 的最终约束

1. 不重写已通过测试的 SDS 解析、Embedding、FAISS 与风险矩阵核心；通过 service adapter 接入新流程。
2. 先做确定性工作流，再接 AI；状态、权限、审批、SLA 全部由 Python 规则控制。
3. 禁止复制三仓库源码；仅按本文规格独立实现。
4. 所有 Demo 人员、SLA、化学品和作业必须明确标注模拟。
5. 不宣称企业生产级、法规合规、不可篡改审计或真实企业提效数据。
6. 每完成一个 P0 能力都先补纯函数/服务测试，再接 Streamlit 页面。
7. UI 的判断不能成为安全边界；服务层必须重复校验权限与状态。

---

## Sources

[1] Frappe Helpdesk, Agent Home backend: https://github.com/frappe/helpdesk/blob/3ab76d795fcba7ee48d54c4897900df5781ce950/helpdesk/api/agent_home/agent_home.py  
[2] Frappe Helpdesk, Pending Tickets UI: https://github.com/frappe/helpdesk/blob/3ab76d795fcba7ee48d54c4897900df5781ce950/desk/src/pages/home/components/PendingTickets.vue  
[3] Frappe Helpdesk, Ticket Activity/Timeline: https://github.com/frappe/helpdesk/blob/3ab76d795fcba7ee48d54c4897900df5781ce950/desk/src/components/ticket-agent/timeline/TicketTimeline.vue  
[4] Frappe Helpdesk, SLA implementation: https://github.com/frappe/helpdesk/blob/3ab76d795fcba7ee48d54c4897900df5781ce950/helpdesk/helpdesk/doctype/hd_service_level_agreement/hd_service_level_agreement.py  
[5] Frappe Helpdesk, Ticket model and assignment handling: https://github.com/frappe/helpdesk/blob/3ab76d795fcba7ee48d54c4897900df5781ce950/helpdesk/helpdesk/doctype/hd_ticket/hd_ticket.py  
[6] SafetyMP, task-oriented dashboard navigation: https://github.com/SafetyMP/Autonomous-EHS-Management/blob/aceb482f0ddb422a990fe7bf2545baa62768a9fa/src/lib/dashboard-nav-links.ts  
[7] SafetyMP, approval workflow and data model: https://github.com/SafetyMP/Autonomous-EHS-Management/blob/aceb482f0ddb422a990fe7bf2545baa62768a9fa/docs/approval-workflow.md  
[8] SafetyMP, Work Permit detail UI: https://github.com/SafetyMP/Autonomous-EHS-Management/blob/aceb482f0ddb422a990fe7bf2545baa62768a9fa/src/app/dashboard/permits/%5BpermitId%5D/page.tsx  
[9] SafetyMP, action queue query and ranking: https://github.com/SafetyMP/Autonomous-EHS-Management/blob/aceb482f0ddb422a990fe7bf2545baa62768a9fa/src/server/services/tasks/actionQueueQuery.ts  
[10] SafetyMP, RBAC permissions: https://github.com/SafetyMP/Autonomous-EHS-Management/blob/aceb482f0ddb422a990fe7bf2545baa62768a9fa/src/lib/rbac.ts  
[11] SafetyMP, workflow depth and audit design: https://github.com/SafetyMP/Autonomous-EHS-Management/blob/aceb482f0ddb422a990fe7bf2545baa62768a9fa/docs/workflow-depth.md  
[12] DefectDojo, Finding model: https://github.com/DefectDojo/django-DefectDojo/blob/254dab83759d0d2fd17b5610d7d8413c790c608a/dojo/finding/models.py  
[13] DefectDojo, Finding UI actions including close, verify and reopen: https://github.com/DefectDojo/django-DefectDojo/blob/254dab83759d0d2fd17b5610d7d8413c790c608a/dojo/finding/ui/views.py  
[14] DefectDojo, Risk Acceptance model: https://github.com/DefectDojo/django-DefectDojo/blob/254dab83759d0d2fd17b5610d7d8413c790c608a/dojo/risk_acceptance/models.py  
[15] DefectDojo, Risk Acceptance workflow documentation: https://github.com/DefectDojo/django-DefectDojo/blob/254dab83759d0d2fd17b5610d7d8413c790c608a/docs/content/triage_findings/findings_workflows/OS__risk_acceptance.md  
[16] DefectDojo, open-source dashboard documentation: https://github.com/DefectDojo/django-DefectDojo/blob/254dab83759d0d2fd17b5610d7d8413c790c608a/docs/content/metrics_reports/dashboards/Introduction_dashboard.md  
[17] HSE, Permit-to-work systems: https://www.hse.gov.uk/humanfactors/topics/ptw.htm  
[18] HSE, Permits to work under COSHH: https://www.hse.gov.uk/coshh/basics/permits.htm
