# Demo Documents（P1 内置资料库）

本目录是 Safety Review Pack 第 3 步「内置资料库」的数据源，由 `registry.json` 登记来源与核验状态。
**原则：绝不伪造 SDS；无法核验的来源必须显式标注。**

## 文档清单与来源状态

| document_id | 内容 | 来源性质 | 核验状态 |
| --- | --- | --- | --- |
| `DEMO-DOC-METHANOL-SDS` | Methanol SDS（Honeywell，产品 34860，v1.3，16 页 REACH 格式） | 厂商原始文件，取自 Thermo Fisher 官方资产服务器（assets.fishersci.com） | **verified** |
| `DEMO-DOC-HF-SDS` | Hydrofluoric acid 40% SDS（Fisher Chemical / Thermo Fisher Scientific，Revision 14，13 页） | Thermo Fisher 官方域名 fishersci.fi 的官方 SDS 端点（服务端直出 PDF） | **verified** |
| `DEMO-DOC-SOP-CONFINED-SPACE` | Demo_有限空间作业安全管理规定 | 本仓库整理的 **Synthetic Demo SOP**（引用公开法规 GB 30871-2022 等，不代表任何真实企业） | synthetic |
| `DEMO-DOC-SOP-LOTO` | Demo_高风险检维修与能量隔离作业指导书 | 本仓库整理的 **Synthetic Demo SOP**（引用 GB 30871-2022 / 应急管理部令13号 / OSHA 1910.147） | synthetic |

## PDF 文件入库策略

- 所有厂商 SDS PDF（`*.pdf`）由 `.gitignore` 排除，**只在本地保留、不提交仓库**。
- `registry.json` 与两份 Synthetic SOP 为文本文件，正常入库。
- 克隆仓库后需按 `registry.json` 中的 `source_url` 自行获取 PDF 并放到本目录，才能复现含真实 SDS 的演示。

## HF 来源核验记录（2026-09-14）

早期版本使用 Purdue Primelab 镜像的 Honeywell HF 49% SDS（unverified），已于本轮替换。
现文件 `HF_SDS_FisherChemical_H1400_40pct_OFFICIAL.pdf` 直接下载自 Thermo Fisher 官方域名
`fishersci.fi` 的官方 SDS 端点 `/store/msds`（服务端返回 PDF，无需 JS 渲染），
核验要点：13 页、SECTION 1-16（UK REACH 格式）、CAS 7664-39-3、Revision Date 2023-10-18
（Revision Number 14）、Cat No. H/1400/08; H/1400/15; H/1400/17; H/1400/21、
REACH 注册号 01-2119458860-33。与 Methanol SDS（assets.fishersci.com）同属 Thermo Fisher 官方渠道。
**备注**：该端点为官方页面 URL 而非静态资产直链，若未来失效，可在
fishersci.com 搜索产品 `H/1400/15`（Hydrofluoric acid 40%）重新获取 SDS。

## 使用方式

- 演示金路径：甲醇储罐内部阀门检维修 → 勾选 Methanol SDS + 有限空间 SOP + 能量隔离 SOP。
- 错配演示：HF 作业错选 Methanol SDS → 生成 `chemical_mismatch` 冲突并阻断确认，
  提示选择/上传 HF SDS。
