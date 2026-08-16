---
name: lesson-lookup
description: lesson-lookup Skill - lesson-lookup
---
# lesson-lookup

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/lesson_lookup.py`）

## 角色
Analyzer (主用) / Fixer (主用)

## 功能
按角色模式查询 `lessons` 历史经验并按相似度 score 分级。

## 使用场景
- Analyzer：按根因维度查询（`mode=analyzer`）
- Fixer：按改法维度查询（`mode=fixer`，仅 `success=true`）

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `query_text` | 是 | Issue 描述 / fix_pattern + error_signature |
| `mode` | 是 | `analyzer` 或 `fixer` |
| `repo` | 否 | 仓库过滤 |
| `top_k` | 否 | analyzer 默认 5，fixer 默认 3 |
| `success_only` | 否 | fixer 模式默认 true |

## 输出结果
匹配 lessons 列表（按 score 三级分流）：
| 级别 | score | 使用方式 |
|------|-------|----------|
| HIGH | ≥ 0.85 | 直接采纳历史策略，跳过部分步骤 |
| MEDIUM | 0.60 ~ 0.85 | 作为候选注入 prompt |
| LOW | < 0.60 | 忽略，按标准流程 |

## 调用条件
- Analyzer 在根因分析前/中调用（mode=analyzer）
- Fixer 在生成 patch 前调用（mode=fixer）

## 依赖
`mcp_server/db/lessons.py`（LessonsStore：语义检索）/ `embed_texts`（向量化）

## 执行方式
`lesson_lookup.py` 负责业务编排：向量化 query → 调用 `LessonsStore.search_similar` →
按相似度 score 三级分流（HIGH ≥ 0.85 / MEDIUM 0.60~0.85 / LOW < 0.60）。

> `lessons` 表由 `LessonsStore.ensure_schema` 幂等创建（`schema.ensure_all` 已接入）。
> 表不可用（连接失败/未建表）时返回空集并降级为"按标准流程执行"，不抛异常。

## 失败处理
- `lessons` 表不可用（未建表/连接失败）→ 返回空集 + 降级提示
- 召回为空 → 返回空集（high/medium/low 均为空）
- 向量化失败 → 返回 `status: error`

## 复用价值
**高**。Analyzer / Fixer 共用，是经验沉淀闭环的关键入口。
