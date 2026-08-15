---
name: test-run
description: test-run Skill - test-run
---
# test-run

## 类型
AgentTeams Skill — prompt-only

## 角色
Tester (主用)

## 功能
执行测试并解析 pytest 输出。

## 使用场景
Tester 真实测试执行阶段。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `test_cmd` | 否 | 默认 `pytest -v --tb=short` |
| `target_files` | 否 | 限定测试文件 |
| `extra_args` | 否 | 如 `-k pattern` |

## 输出结果
`test_report.json`：含 `passed_cases` / `failed_cases` / `error_type` / `traceback` / `failing_line` / `duration_sec`。

## 调用条件
Tester 进入 `testing` 状态、已应用 `fix.diff`、venv 已搭建完成后触发。

## 依赖工具/系统
`bash-exec`、Python venv、pytest。

## 执行方式
由 Worker runtime (OpenClaw) 直接提供，通过 bash-exec 能力执行。

## 失败处理
- pytest 解析失败 → 返回原始 stdout/stderr
- 环境异常（缺包/版本冲突）→ escalate
- 同一 diff 连续 3 轮失败 → escalate

## 权限与安全
- 在隔离 venv 内执行，不污染主环境
- 超时 10 分钟强制终止
- 不联网下载未声明依赖

## 复用价值
**中**。主要 Tester 使用；扩展多语言时复用同一执行框架。
