# STATUS_TRACKER — EHS Copilot

> 项目定位（不可含糊）：面向真实 EHS 业务流程设计的 **AI 工作流原型**，**未在真实企业生产环境部署**。
> 所有 SDS / SOP / 隐患数据中，仅 Methanol SDS 为真实厂商文件（本地保留、不入库）；两个 SOP 为标注清楚的 Synthetic Demo；其余均为模拟数据。

## 阶段状态总览

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | 核心工作流（LangGraph 固定编排 + HITL 审批门） | ✅ 完成 |
| P0C | 面向业务的 5 页产品界面（今日工作台 / 作业许可 / 隐患与整改 / 风险看板 / 资料与审计） | ✅ 完成 |
| P1 | Safety Review Pack 可演示闭环（真实资料 + 冲突结构化 + 关键词扩充 + UI 选资料） | ✅ **收口（2026-09-14）** |
| P2 | confirmed Pack → 正式 Permit（连接现有 Permit 工作流） | ✅ **已收口（2026-09-15）** |
| P3（未来） | 完整黄金流程人工验收（Permit→Hazard→整改→关闭）、真实身份体系、生产化部署 | ⛔ 未开始 |

> **项目组合冻结（2026-09-15）**：P2 人工验收的 4 个 P1 问题修复完毕并通过定向/全量/冒烟验证后，
> 本仓库进入冻结状态，停止功能开发。

## P1 收口记录（2026-09-14）

### 交付内容

1. **P1-1 SDS 版本冲突**：`evidence_adapter.version_conflicts` 输出结构化冲突
   （`conflict_id` / `description` / `evidence_refs` / `requires_human_resolution`），
   仅在存在真实 SDS 证据时产生，杜绝无依据的伪造冲突；无冲突时明确输出
   「No unresolved evidence conflict detected」。
2. **P1-2 化学品错配冲突**：`chemical_mismatch_conflicts` —— 声明化学品与所选 SDS 不一致时
   产生 `chemical_mismatch` 冲突并阻断确认（HF 作业错选 Methanol SDS → 阻断，提示补传 HF SDS）。
3. **P1-3 关键词扩充**：`safety_review.py` 的 `_WORK_TYPE_KEYWORDS` / `_RISK_KEYWORDS` 覆盖
   受限空间、检维修与能量隔离（断电 / 管线打开 / 法兰拆卸等）、甲醇 / 储罐，
   以及化学品暴露 / 中毒窒息 / 触电 / 机械伤害 / 压力或能量释放 / 环境泄漏。
4. **P1-4 UI**：作业许可第 3 步可直接勾选内置资料库（真实 SDS 与 Synthetic Demo SOP 分标注）；
   第 4 步新增「资料冲突（Conflicts）」区块；版本冲突支持人工选择现行版本解决；
   JSA 评审表单每项可绑定证据引用。
5. **Demo 资料库**：`data/demo_documents/registry.json` 登记 4 份文档及来源核验状态
   （详见 `data/demo_documents/README.md`）。厂商 SDS PDF 一律本地保留，`.gitignore` 排除不入库。

### 验收结果（数字均由实际运行得出，禁止手写）

| 验收项 | 结果 |
| --- | --- |
| 全量测试 `pytest` | **518 passed**，449 subtests passed |
| 评测 `python evals/evaluate_safety_review.py` | 10/10 场景，全部指标 = 1.0 |
| 程序化验收 `_p1_acceptance.py` | 金路径 + HF 错配阻断 + HF 正确放行 全部通过 |
| 真实浏览器金路径 | **通过**（详见下） |

**真实浏览器金路径**（Streamlit 真实 UI，全程无后门注入）：
作业申请人填写「甲醇储罐内部阀门检维修」→ AI 预检确认 → 勾选内置资料
（Methanol 真实 SDS + 有限空间 SOP + 能量隔离 SOP，Synthetic SDS 知识库未勾选）→
生成审核包 → 切换 EHS审核人 → 4 项 JSA 人工评分（L=4/S=5/RL=2/RS=3）并逐项绑定证据 →
保存人工审核为新版本 → 确认 Safety Review Pack → 出现「Safety Review Pack已由EHS确认；
Phase 1不会创建正式Permit」flash，无阻断项。
审核包证据引用覆盖真实 `Methanol_SDS_Honeywell_34860_EN.pdf`（第1/2/5/6页）与两个 Synthetic SOP，
Conflicts 区块为空（无未解决冲突）。

### P1 边界重申

- **Phase 1 不创建正式 Permit**；EHS 人工确认 Safety Review Pack 即为终点。
- 所有场景以 `auto_approve=False` 真实 HITL 路径回放。
- 页面无任何技术术语暴露（LangGraph / Tool Calling / JSON 等）。

## P2 实现记录（2026-09-15）：confirmed Pack → 正式 Permit

### 链路

新建高风险作业 → 风险识别 → SDS/SOP 证据 → JSA / Safety Review Pack → **EHS 人工确认**
→ **创建正式 Permit（本轮新增）** → 现有 Permit 审核/审批/开工/执行流程 → Hazard → 整改 → EHS 验证 → 关闭。
前半段 AI 提效，后半段工作流控责；**Pack 确认 ≠ Permit 批准**，未放宽任何 Phase 1 guardrail。

### 关键设计

1. **前置条件（全部满足才允许创建）**：Pack 已 confirmed、人工 L/S 已完成、
   无 unresolved `chemical_mismatch`、无 Evidence insufficient blocker、无其他阻断性 conflict；
   且操作者具备 `PERMIT_CREATE` 权限。不满足时明确报错原因（服务层与 UI 双层拦截）。
2. **初始状态**：新 Permit 进入现有 state machine 的 `draft`，完整走原有审核/审批流程，无任何跳级。
3. **数据继承**：复用 `permit_service.create_permit`，自动带入作业名称/类型/地点/计划时间/
   负责人/化学品/风险标签/已确认 JSA（带确认时间戳）/SDS 证据
   （citation `source_name` 即 .pdf 文件名，满足 state machine 对 sds 证据轨的要求）。
4. **来源追溯**：新增 `permit_sources` 表（permit_id ← draft_id + pack_id + pack_version +
   snapshot_json 冻结确认风险标签等 + confirmed_by/at + created_by/at）。
   Permit 详情页新增「安全准备来源」区：WD → Pack·vN → EHS 确认信息 → 一键打开作业准备记录。
5. **幂等**：`permit_sources.permit_id` 唯一锚点——同一 Pack 第二次点击不再创建，
   按钮变为「打开正式作业许可 PERMIT-xxxx」；服务层重复调用返回既有 Permit（`created=False`）。
6. **审计**：创建时写入 `pack_linked`（Pack 侧）与 `permit_created`（Permit 侧）双审计事件，
   含操作者、来源 WD/Pack 版本、生成 Permit ID；不修改旧审计记录。

### 验证结果（实际运行得出）

| 验收项 | 结果 |
| --- | --- |
| 定向测试 A–G（`tests/test_permit_linkage.py`） | **7 passed**（创建/禁止未确认/禁止 mismatch/幂等/溯源/原审批流/Demo 不破坏） |
| 定向 UI + 服务回归 | 69 passed + 77 subtests |
| 全量 pytest | **525 passed + 449 subtests passed** |
| 真实浏览器 smoke（临时库） | confirmed WD → 创建 PERMIT-001 → 跳转详情（h1 正确、无 Traceback）→ `permit_sources` 恰 1 行 → Demo permits 全部完好 |
| AppTest 补验（`.workbuddy/verify_p2_ui.py`） | 10/10：幂等按钮（无重复创建入口）、点击跳转详情、溯源区显示 WD/Pack·vN/风险标签、服务层幂等 |

### P2 边界重申

- 本轮止于「Pack → 正式 Permit 可进入后续工作流」；完整黄金流程（Permit→Hazard→整改→关闭）下一轮人工验收。
- 未改 SDS/RAG/Evidence Adapter/Phase 1 guardrail/Permit-Hazard 状态机/Dashboard；未新增 Agent 架构。

## P2 收口记录（2026-09-15）：人工验收 P1 修复

人工验收确认 4 个 P1 问题，本轮全部修复（最小改动，未动 Phase 1 guardrail / 状态机 / 权限体系）：

1. **已确认 JSA 未带入 Permit**：根因是详情页阶段门槛把 draft Permit 的 JSA 区块渲染成
   「该阶段尚未开始」。`ui/permits.py::_section_is_future` 现在对已有数据（chemicals/jsa_items）
   的区块照常渲染；服务层本就继承 pack 人工评分后的 JSA（带 confirmed_by/at）。
2. **WorkDraft 负责人未继承**：`safety_review_service._resolve_owner_id` —— 负责人姓名匹配
   Demo 用户 display_name 时映射为该用户 owner_id，否则回落到申请人；不动权限体系。
3. **Methanol/甲醇 两个化学品 + SDS 文件/状态为空**：`_permit_chemicals` 用现有别名表归一化为
   单一实体（其余写法变 aliases），并从 pack 已确认 SDS citation 挂接 `sds_file`（.pdf 文件名）
   与 `sds_status="confirmed"`；不重新下载、不改 Phase 1 Evidence/RAG。
4. **控制措施重复拼接**：`permit_service._dedup_segments` 对聚合文本按「；」分句去重（小修）。

### 验证结果（数字均由实际运行得出）

| 验证 | 结果 |
| --- | --- |
| 定向测试（linkage + permit_state + persistence） | **35 passed + 18 subtests**（含更新后的化学品归一/owner/去重断言） |
| 全量 pytest | **525 passed + 449 subtests passed**（与 P2 基线一致） |
| 端到端冒烟（新临时库金路径 + AppTest 渲染详情） | **11/11 通过**：owner=整改负责人甲、甲醇单实体+SDS 挂接、4 条人工确认 JSA、控制措施无重复、幂等保持、JSA/化学品区块正常渲染、无异常 |

### P2 已知限制（≤3，均为展示层/下轮范围）

1. 控制措施去重仅按「；」分句精确匹配；措辞略有差异的近似重复仍会保留。
2. 负责人映射为精确 display_name 匹配 + 申请人回落；自由文本无法模糊匹配真实用户。
3. Permit 详情页「该阶段尚未开始」占位对 approval/prestart/execution 区块仍按阶段显示（设计如此）。

**此后本仓库冻结，停止开发。**

## P2 变更文件索引

- `schema.py`：新增 `permit_sources` 表与索引（增量迁移，不动 `permits` 表）
- `services/safety_review_service.py`：`get_pack_permit_link` / `create_permit_from_pack`
- `ui/safety_review.py`：confirmed 只读页「转入正式作业许可」区（创建 + 幂等打开，`on_click` 回调导航）
- `ui/permits.py`：详情页「安全准备来源」expander + 打开作业准备记录回调；移除 Phase 1 时代的占位文案
- `tests/test_permit_linkage.py`：A–G 场景定向测试
- 备注：导航跳转一律通过 `on_click` 回调写 widget 绑定键（脚本体内写 NAV_KEY 会抛 StreamlitAPIException）

## 已知缺口与风险（修复前不要删除）

1. **HF SDS 已核验（2026-09-14 更新）**：已替换为 Thermo Fisher 官方渠道的
   Fisher Chemical「Hydrofluoric acid 40%」SDS（Revision 14，2023-10-18，13 页，
   来源 fishersci.fi 官方 SDS 端点），registry 标记 `verified`，
   `DEMO-DOC-HF-SDS-UNVERIFIED` 已退役删除。至此 P1 的两个真实 SDS（Methanol / HF）
   均来自 Thermo Fisher 官方渠道。详见 `data/demo_documents/README.md`。
2. **路由盲点（真实缺陷，非测试 bug）**：`今天有几个隐患要到期` 被规划为 `create_hazard`；
   `把 HZ-001 关掉`、`帮我评估一下这个作业的风险`、`检查一下配电箱`、`危险源辨识怎么做`
   识别不出任务。审批门可兜底，但计划是错的——详见 README「项目限制」。
3. **参数抽取退化**：`为该作业做 JSA` → JSA「作业名称」=「该作业」。
4. Routing / Tool Call 指标为 in-sample，不等于泛化能力（README 已写明）。

## P1 变更文件索引

- `evidence_adapter.py`：chemical_aliases / version_conflicts 结构化 / chemical_mismatch_conflicts /
  conflict_note / demo_document_registry / load_demo_resources
- `safety_review.py`：_WORK_TYPE_KEYWORDS / _RISK_KEYWORDS 扩充；pack_blockers 覆盖冲突文本
- `workflow/safety_review_graph.py`：conflict_note 进状态与包；_validated_llm_conflicts
  （LLM 冲突必须引用真实存在的 SDS+SOP/internal 证据，否则丢弃）；fallback JSA 绑定 document_controls
- `ui/safety_review.py`：第 3 步内置资料多选；第 4 步 Conflicts 区块；JSA 逐项证据绑定
- `data/demo_documents/`：registry.json + 真实 Methanol SDS PDF（本地）+ HF 镜像 PDF（本地，未核验）+ 2 份 Synthetic SOP
- `.gitignore`：排除 `data/demo_documents/*.pdf`
- `_p1_acceptance.py`：程序化验收脚本
- `evals/`：数据集与指标口径不变，回归 10/10
