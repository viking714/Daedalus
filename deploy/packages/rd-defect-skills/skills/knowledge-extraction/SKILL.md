---
name: knowledge-extraction
description: knowledge-extraction Skill - knowledge-extraction
---
# knowledge-extraction

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/extract.py`）

## 角色
Reviewer (主用)

## 功能
从修复结果提取模式与经验。Reviewer 裁定完成后，将本次修复的结构化经验写入 `lessons` 表。

> **v0.1.1 变更**：原名 `knowledge_miner`，统一为设计文档命名 `knowledge-extraction`。

## 使用场景
Reviewer 产出 `verdict.json` 后，将修复经验结构化沉淀。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `fix_diff` | 是 | 修复 diff |
| `test_report` | 是 | 测试报告 |
| `verdict` | 是 | 评估裁定 |
| `root_cause_report` | 是 | 根因报告 |
| `task_id` | 是 | 任务 ID |
| `repo` | 是 | 仓库名 |
| `retry_count` | 否 | 重试次数 |

## 输出结果
新写入的 `lessons` 记录：
- `root_cause` / `fix_pattern` / `error_signature` / `fix_strategy`
- `affected_modules` / `tags` / `diff_summary` / `test_changes` / `edge_cases`
- `success` / `resolution_summary` / `retry_count` / `merge_count` / `related_to`

## 调用条件
Reviewer 产出 `verdict.json` 后立即调用；不论 pass/reject 均触发。

## 依赖
`mcp_server/db/lessons.py`（LessonsStore：去重合并写入）/ `embed_texts`（向量化）

## 写入前去重策略
```
root_cause embedding → pgvector cosine similarity (top_k=1, ns=repo)

score >= 0.95  → MERGE（更新已有记录）
score [0.85, 0.95) → SIMILAR（插入新行，记录 related_to）
score < 0.85  → NEW（全新案例）
```

> `lessons` 表由 `LessonsStore.ensure_schema` 幂等创建（`schema.ensure_all` 已接入）。
> 写入前经 `LessonsStore.upsert_with_dedup` 自动去重合并，完整落地 MERGE/SIMILAR/NEW 三级决策。

## 失败处理
- `lessons` 表不可用（未建表/连接失败）→ 返回 `unavailable`，携带已抽取字段（不阻塞主流程）
- `root_cause` 为空 → 返回 `error`（无法向量化）
- 写入前去重比对失败 → 由 LessonsStore 抛 DbUnavailable，上层保守降级

## 复用价值
**中**。仅 Reviewer 使用；可作为"经验沉淀引擎"独立推广。
