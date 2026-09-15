# STATUS_TRACKER — EHS Copilot

> 项目定位（不可含糊）：面向真实 EHS 业务流程设计的 **AI 工作流原型**，**未在真实企业生产环境部署**。
> 所有 SDS / SOP / 隐患数据中，仅 Methanol SDS 为真实厂商文件（本地保留、不入库）；两个 SOP 为标注清楚的 Synthetic Demo；其余均为模拟数据。

## 阶段状态总览

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | 核心工作流（LangGraph 固定编排 + HITL 审批门） | ✅ 完成 |
| P0C | 面向业务的 5 页产品界面（今日工作台 / 作业许可 / 隐患与整改 / 风险看板 / 资料与审计） | ✅ 完成 |
| P1 | Safety Review Pack 可演示闭环（真实资料 + 冲突结构化 + 关键词扩充 + UI 选资料） | ✅ **本轮收口（2026-09-14）** |
| P2（未来） | 正式 Permit 创建、真实身份体系、生产化部署 | ⛔ 未开始 |

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
