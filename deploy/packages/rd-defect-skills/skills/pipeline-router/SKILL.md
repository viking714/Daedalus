---
name: pipeline-router
version: 0.2.0
description: 任务路由与状态机：支持 incident/bug/feature 三类任务，含回退仲裁与双闸门计数。
type: with-scripts
roles: [team_leader, coordinator]
---
# pipeline-router

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/task_router.py` / `scripts/state_manager.py` / `scripts/handoff.py` / `scripts/loop_judge.py` / `scripts/release_plan.py` / `scripts/release_decision.py`）

## 角色
Team Leader (主用)

## 功能
任务路由、状态推进、回退仲裁、人工交接、灰度发布计划与结果确认。Team Leader 在每个阶段过渡时调用，决定下游 Agent 与状态切换。

## 使用场景
Team Leader 每次收到 Worker 产出后、推进状态机前调用；Reviewer 裁定通过后生成发布计划；灰度完成后决策关单/回滚；回退时按 failure_class 仲裁目标角色。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `task_id` | 是 | 任务标识 |
| `task_type` | 否 | incident / bug / feature |
| `current_state` | 是 | 当前 TaskState |
| `current_stage` | 否 | 当前阶段 |
| `round` | 否 | 当前轮次 |
| `from_stage` | 否 | 来源阶段 |
| `to_stage` | 否 | 目标阶段 |
| `owner_agent` | 否 | 负责 Agent |
| `failure_class` | 否 | code/design/requirement/environment/visual |
| `regression_cycle_count` | 否 | 灰度回归次数（超限转 escalated） |
| `confirmation_report` | 否 | 外部 CI/CD 灰度结果报告（canary 结果） |

## 输出结果
派单指令（`next_agent` + `next_stage`）+ 状态推进结果 + 发布计划（release_plan.json）+ 灰度决策 + 人工交接消息（可选）。

## 调用条件
Team Leader 每次收到 Worker 产出后调用；回退时由 Team Leader 仲裁。

## 依赖 MCP 原语
`redis_set_repo_state`（状态快照）

## 状态机

### incident
```
received → triaging → ops_diagnosing → ops_remediation → resolved
                            ↓
                         analyzing (转 bug)
```

### bug
```
received → triaging → analyzing → fixing → testing → evaluating → awaiting_release → resolved / escalated
        ↑_________________________________________|     (failure_class=environment → ops_diagnosing)
```

### feature / greenfield
```
received → triaging → clarifying → prd_drafting → prd_review → designing → design_review → developing → test_designing → test_executing → awaiting_release → resolved / escalated
        ↑_________________________________________________|     (failure_class 分类回退，Reviewer sign-off 后放行)
```

## 回退协议
| failure_class | 回退目标 |
|---|---|
| code | developer |
| design | architect |
| requirement | po |
| environment | ops-analyst |
| visual | developer |

双闸门：单阶段重试 <=2；全局 PO 回退 <=1；MAX_ROUND=3 超限 escalated。

## 闭环阈值
- `MAX_ROUND = 3`：超过则 escalated
- `MAX_FILES = 5`：单轮修改文件数上限
- `TOKEN_BUDGET = 100000`：超预算触发二次压缩
- `TASK_TIMEOUT_MIN = 30`：超时转 escalated
- `REGRESSION_CYCLE_MAX = 3`：灰度回归次数上限，超限转 escalated
- `CANARY_TIMEOUT_MIN = 1440`（24h）：awaiting_release 超时哨兵 TTL
- `INCIDENT_TIMEOUT_MIN = 15`：incident 诊断阶段超时

## 执行方式
- `task_router.py`：按 task_type 路由，支持回退边
- `state_manager.py`：状态机管理 + 双闸门计数
- `handoff.py`：人工交接包生成
- `loop_judge.py`：循环判定
- `release_plan.py` / `release_decision.py`：灰度发布

## 失败处理
- 状态机冲突 → 回滚到上一稳定态
- Matrix 通信失败 → 重试 3 次后 escalate
- `regression_cycle_count >= 3` → 强制 `escalated`
- canary 超时未收到结果 → `escalated` 并 `@admin` 通知人工
- failure_class=environment 时回退 Ops Analyst 复查

## 复用价值
**高**。作为"多阶段任务调度 + 回退仲裁 + 灰度发布"通用模式。
