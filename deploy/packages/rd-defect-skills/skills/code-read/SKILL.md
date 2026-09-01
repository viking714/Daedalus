---
name: code-read
description: code-read Skill - code-read
---
# code-read

## 类型
AgentTeams Skill — prompt-only

## 角色
Manager (备用) / Architect (主用) / Developer (主用) / Reviewer (主用)

## 功能
读取代码文件，支持指定行范围与上下文窗口。

## 使用场景
- Architect 阅读源码确认根因假设
- Developer 读取待修改文件
- Reviewer 审查 diff 时对照原始代码

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `file_path` | 是 | 仓库内相对路径 |
| `start_line` | 否 | 起始行号 |
| `end_line` | 否 | 结束行号 |
| `context_window` | 否 | 上下文窗口大小，默认 ±10 行 |

## 输出结果
文件内容（含行号标记的字符串），超出范围时按窗口截断并提示。

## 调用条件
任何需要读取源码的环节均可调用。

## 依赖工具/系统
Worker 原生工具 `file r/w`（只读模式）。

## 执行方式
由 Worker runtime (OpenClaw) 直接提供，不经过 MCP Server。

## 失败处理
- 文件不存在 → 返回错误码 + 建议路径
- 权限拒绝 → escalate 给 Manager
- 编码异常 → 自动尝试 UTF-8 / GBK 降级

## 权限与安全
- 沙箱路径限制（仅仓库工作区内）
- 只读访问
- 不返回 `.env` / `secrets/` 等敏感目录

## 复用价值
**高**。Manager/Architect/Developer/Reviewer 四个角色均使用。
