---
name: root-cause-analysis
description: root-cause-analysis Skill - root-cause-analysis
---
# root-cause-analysis

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/root_cause.py`）

## 角色
Analyzer (主用)

## 功能
根因推断与复现脚本生成。整合 `code-search` / `impact-analysis` / `lesson-lookup` 的多源信息后，产出结构化根因报告。

## 使用场景
Analyzer 在 `analyzing` 阶段整合多源信息后。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `issue_text` | 否 | Issue 描述 |
| `context_pack` | 否 | 来自 code-search 的上下文 |
| `suspect_symbol` | 否 | 嫌疑符号名 |
| `ns` | 否 | 命名空间 |

## 输出结果
`analysis_report.json`（结构化根因 + 修复策略）+ `root_cause_report.md`（人类可读）+ 可选复现脚本。

## 调用条件
Analyzer 完成 `code-search` / `impact-analysis` / `lesson-lookup` 三个前置 Skill 后调用。

## 依赖 MCP 原语
`neo4j_dep_subgraph`（获取依赖子图，辅助根因推断）

## 执行方式
`root_cause.py` 整合上下文，结合 Neo4j 依赖子图做启发式分析；Neo4j 不可用时降级为纯启发式。

## 失败处理
- 根因不明确 → 输出 2~3 个候选根因 + 置信度
- 复现脚本生成失败 → 仅输出分析报告
- 置信度 < 0.5 → escalate

## 复用价值
**中**。主要 Analyzer 使用；可作为"故障根因分析"通用能力。
