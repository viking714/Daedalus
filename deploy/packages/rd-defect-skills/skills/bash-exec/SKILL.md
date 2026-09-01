---
name: bash-exec
description: bash-exec Skill - bash-exec
---
# bash-exec

## 类型
AgentTeams Skill — prompt-only

## 角色
Manager (主用) / Architect (主用) / Developer (主用) / Tester (主用) / Reviewer (主用)

## 功能
安全执行 bash 命令，白名单机制。

## 使用场景
- Tester 执行 pytest
- Developer 跑 git diff
- Manager 调外部脚本
- Architect 运行复现脚本

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `command` | 是 | shell 命令字符串 |
| `cwd` | 否 | 工作目录 |
| `timeout_sec` | 否 | 超时秒数，默认 60s，上限 600s |
| `env_overrides` | 否 | 临时环境变量 |

## 输出结果
`stdout` / `stderr` / `exit_code` / `duration_ms` 四个字段。

## 调用条件
任何需要执行 shell 命令时；白名单外的命令直接拒绝。

## 依赖工具/系统
Worker 原生工具 `bash`（带白名单 + 超时）。

## 执行方式
由 Worker runtime (OpenClaw) 直接提供，不经过 MCP Server。

## 失败处理
- 白名单拦截 → 返回拒绝原因
- 超时 → SIGTERM → 强制 kill 进程
- 非零退出码 → 返回完整 stderr 供调用方判断

## 权限与安全
- **白名单机制**（仅允许安全命令如 `git` / `pytest` / `pip` / `python` 等）
- 超时强制终止
- 禁止 `rm -rf` / `sudo` / 反弹 shell 等高危命令
- 不继承宿主机环境变量

## 复用价值
**高**。五个角色均使用，是事实上的"通用执行入口"。
