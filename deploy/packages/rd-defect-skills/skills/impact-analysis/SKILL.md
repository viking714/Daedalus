---
name: impact-analysis
description: impact-analysis Skill - impact-analysis
---
# impact-analysis

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/dep_graph.py` / `scripts/contract_check.py`）

## 角色
Architect (主用) / Reviewer (主用)

## 功能
依赖图分析与契约检查。Architect 评估影响面、Reviewer 审查一致性时使用。

## 使用场景
- Architect 完成根因定位后评估影响面
- Reviewer 审查 patch 时确认波及范围

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `target_symbols` | 否 | 函数/类/模块列表 |
| `changed_files` | 是 | 修改的文件路径列表 |
| `patch_text` | 否 | diff 文本 |
| `depth` | 否 | 图遍历深度，默认 2 |
| `check_contract` | 否 | 是否检查 API 契约 |
| `ns` | 否 | 命名空间 |

## 输出结果
影响面统计（`affected_files` / `affected_functions` / `affected_callers`）、契约违规列表、风险等级（L0–L3）。

## 调用条件
Architect 完成根因定位后、Developer 制定修复方案前；Reviewer 审查 patch 时再次确认影响面。

## 依赖 MCP 原语
`neo4j_impact_stats` / `neo4j_dep_subgraph` / `neo4j_symbol_lookup` / `ast_parse_file`

## 执行方式
- `dep_graph.py`：调用 Neo4j 原语获取真实调用方数和跨文件导入数，降级时使用启发式估算
- `contract_check.py`：解析 patch 文本检测函数签名变更

## 风险等级
- L0: 无外部依赖
- L1: 影响 1-2 个调用方
- L2: 影响 3-10 个调用方或跨模块
- L3: 影响 ≥10 调用方或核心模块 → 需人工审批

## 失败处理
- 图谱数据缺失 → 返回空集 + 提示"需先 `repo-index`"
- 契约解析失败 → 跳过该模块并记录警告

## 复用价值
**高**。Architect / Reviewer 共用；可推广到"代码变更风险评估"通用场景。
