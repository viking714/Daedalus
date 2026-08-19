---
name: pipeline-router
description: pipeline-router Skill - pipeline-router
---
# pipeline-router

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/task_router.py` / `scripts/state_manager.py` / `scripts/handoff.py` / `scripts/loop_judge.py` / `scripts/release_plan.py` / `scripts/release_decision.py`）

## 角色
Manager (主用)

## 功能
任务路由、状态推进、人工交接、灰度发布计划与结果确认。Manager 在每个阶段过渡时调用，决定下游 Agent 与状态切换。

## 使用场景
Manager 每次收到 Worker 产出后、推进状态机前调用；Evaluator 裁定通过后生成发布计划；灰度完成后决策关单/回滚。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `task_id` | 是 | 任务标识 |
| `current_state` | 是 | 当前 TaskState |
| `current_stage` | 否 | 当前阶段（received/analyzing/fixing/testing/evaluating/awaiting_release） |
| `round` | 否 | 当前轮次 |
| `from_stage` | 否 | 来源阶段 |
| `to_stage` | 否 | 目标阶段 |
| `owner_agent` | 否 | 负责 Agent |
| `regression_cycle_count` | 否 | 灰度回归次数（超限转 escalated） |
| `confirmation_report` | 否 | 外部 CI/CD 灰度结果报告（canary 结果） |

## 输出结果
派单指令（`next_agent` + `next_stage`）+ 状态推进结果 + 发布计划（release_plan.json）+ 灰度决策 + 人工交接消息（可选）。

## 调用条件
Manager 每次收到 Worker 产出后调用。

## 依赖 MCP 原语
`redis_set_repo_state`（状态快照）

## 状态机
```
received → analyzing → fixing → testing → evaluating → awaiting_release → resolved / escalated
```

`awaiting_release` 为灰度发布等待态：Manager 生成 `release_plan.json`、创建 PR 后进入 yield，由 canary 结果事件唤醒并决策关单（resolved）或回滚 Analyzer（analyzing）。

## 闭环阈值
- `MAX_ROUND = 3`：超过则 escalated
- `MAX_FILES = 5`：单轮修改文件数上限
- `TOKEN_BUDGET = 100000`：超预算触发二次压缩
- `TASK_TIMEOUT_MIN = 30`：超时转 escalated
- `REGRESSION_CYCLE_MAX = 3`：灰度回归次数上限，超限转 escalated
- `CANARY_TIMEOUT_MIN = 1440`（24h）：awaiting_release 超时哨兵 TTL

## 执行方式
- `task_router.py`：路由决策（流水线推进 / 达阈值转 escalated / awaiting_release 后等事件驱动）
- `state_manager.py`：状态机管理（版本号 + 阶段一致性校验 + 闭环阈值闸门 + 回归次数/TTL 记录）
- `handoff.py`：人工交接包生成
- `loop_judge.py`：循环判定（检测重复失败时触发人工交接）
- `release_plan.py`：生成灰度发布计划 `release_plan.json`（canary_scope / risk_level / rollback_point / promote_threshold）
- `release_decision.py`：灰度结果确认（`decide_release` 决策关单/回滚/escalated + `check_canary_timeout` 超时哨兵）

## 失败处理
- 状态机冲突 → 回滚到上一稳定态
- Matrix 通信失败 → 重试 3 次后 escalate
- `regression_cycle_count >= 3` → 强制 `escalated`
- canary 超时未收到结果 → `escalated` 并 `@admin` 通知人工

## 复用价值
**中**。仅 Manager 使用；可作为"多阶段任务调度 + 灰度发布"通用模式。
