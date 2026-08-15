---
name: module-lookup
description: module-lookup Skill - module-lookup
---
# module-lookup

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/module_lookup.py`）

## 角色
Analyzer (主用)

## 功能
模块定位与入口点发现。快速定位"Issue 涉及哪个模块、入口在哪"。

## 使用场景
Analyzer 在调用 `code-search` 之前做"模块级预筛"时触发。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `concept` | 是 | 模块名/关键词/Issue 摘要 |
| `ns` | 否 | 命名空间 |

## 输出结果
候选模块列表：`module_path` / `entry_points` / `related_symbols` / `score`。

## 调用条件
Analyzer 在初步分析时快速定位涉及模块。

## 依赖 MCP 原语
`neo4j_symbol_lookup` / `neo4j_dep_subgraph` / `pgvector_search`

## 执行方式
`module_lookup.py` 使用向量搜索找到与概念最相关的模块，从搜索结果推断模块信息。

## 失败处理
- 未找到 → 返回相邻模块候选 + 提示手动指定
- 图谱缺失 → 降级为纯语义搜索

## 复用价值
**中**。主要 Analyzer 使用；可推广到"代码导航"通用工具。
