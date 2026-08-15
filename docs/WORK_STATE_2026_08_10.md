# 2026-08-10 工作状态与遗留问题

## 已完成的核心修复

### 1. Controller 网络问题
- **问题**: `agentteams-controller` 使用 `--network host` 导致 IPv6 冲突
- **修复**: 改用 bridge 网络 `hiclaw-net`，加 `--network-alias` 解析内部服务

### 2. Team CRD 格式
- **问题**: 自创 `leader.runtime` 字段不匹配 API
- **修复**: 用官方格式 `leader.name` + `workerMembers`（含 team_leader role）

### 3. Worker 命名冲突
- **问题**: Worker 叫 `manager` 与 Manager CRD 共用 `@manager` Matrix 用户
- **修复**: 重命名为 `coordinator`，避免 Matrix 身份冲突

### 4. Matrix 消息 mention 格式
- **问题**: Runner 只发 `m.mentions` 没有 `formatted_body`，coordinator 不响应
- **修复**: 添加 `format: org.matrix.custom.html` + HTML `<a>` mention link

### 5. Verdict 检测
- **问题**: Runner 只查 `verdict.json` 文件，不扫 Matrix 消息
- **修复**: 添加 `_scan_matrix_verdict()` 函数扫描 @coordinator 消息中的 "Verdict: SUCCESS/FAIL"

### 6. MinIO Artifact 拉取
- **问题**: Runner 用 `mc cp` 拉取失败
- **修复**: 改用 `mc cat` + 本地写入

### 7. Team Room 创建
- **问题**: Controller 无法自动创建 Team Room（Tuwunel 403）
- **修复**: 手动通过 Matrix API 创建 `!XuzjwZZcSiPmXGgHQJ` 并邀请所有 Worker

### 8. Worker 跨通信权限
- **问题**: Worker 的 `groupAllowFrom` 只允许 `@manager`/`@admin`
- **修复**: 更新所有 Worker 的 `openclaw.json`，`groupAllowFrom` 包含全部 7 个团队成员

### 9. Worker SOUL.md 委派模式
- **问题**: Coordinator 自己做所有工作，不委派
- **修复**: 
  - Coordinator SOUL: 用 Matrix @mention 委派，不用 sessions_spawn
  - 子 Worker SOUL: 完成后必须 @mention @coordinator 触发下一阶段

### 10. Runner Room 发现
- **问题**: Runner 发送到旧房间，coordinator 不在
- **修复**: 优先找所有 Worker 都在的 Team Room

## 遗留问题

### P0 - 超时导致 Verdict 未捕获
- **现象**: Pipeline 走完 analyzer→fixer→tester，tester 报告 "All 19 tests passed"，但 runner 600s 超时
- **根因**: Coordinator 收到 tester 结果后，正在委派给 evaluator 时 runner 已超时
- **修复方向**: 
  1. 增加 runner timeout 到 1200s
  2. 或优化 Coordinator 的等待逻辑，收到 tester 结果后直接输出 Verdict

### P1 - Worker 配置重启被覆盖
- **现象**: 重启 Worker 后，`groupAllowFrom` 被 MinIO 同步覆盖回默认值
- **临时修复**: 每次重启后手动修改本地 `openclaw.json` + MinIO
- **彻底修复**: 需要修改 controller 的 worker provisioning 逻辑，或在 Worker YAML 中覆盖

### P2 - Streaming 导致重复消息
- **现象**: analyzer 一次响应发了 28 条重复消息
- **临时修复**: 设置 `streaming: off` + `blockStreaming: true`
- **注意**: 重启后可能被覆盖（同 P1）

### P3 - SOUL.md 被 MinIO 覆盖
- **现象**: 重启 Worker 后，SOUL.md 被同步回 MinIO 中的旧版本
- **临时修复**: 同时更新本地和 MinIO
- **彻底修复**: 需要在 Worker YAML 的 `files.soul` 中定义

### P4 - Team CRD Reconcile 403
- **现象**: Controller 无法自动创建 Team Room 和邀请 Worker
- **临时修复**: 手动创建 Team Room + 手动邀请
- **彻底修复**: 升级 controller 版本或用 K8s 原生部署

## 当前运行环境

### 容器拓扑
- `hiclaw-controller` (embedded, bridge network, hiclaw-net)
- `hiclaw-worker-coordinator` (openclaw, copaw runtime)
- `hiclaw-worker-analyzer` (openclaw)
- `hiclaw-worker-fixer` (openclaw)
- `hiclaw-worker-tester` (openclaw)
- `hiclaw-worker-evaluator` (openclaw)
- `minio-proxy` (alpine/socat, 转发 19000→9001)

### 关键配置
- Matrix Domain: `matrix-local.hiclaw.io:18080`
- Team Room ID: `!XuzjwZZcSiPmXGgHQJ:matrix-local.hiclaw.io:18080`
- MinIO Web UI: `http://127.0.0.1:19000` (admin / Transformer123$)
- Higress Gateway: `http://127.0.0.1:18080`

### 关键文件路径
- Controller secrets: `/data/worker-creds/*.env` (容器内)
- Agent configs: `hiclaw/hiclaw-storage/agents/{worker}/openclaw.json` (MinIO)
- Agent SOUL: `hiclaw/hiclaw-storage/agents/{worker}/SOUL.md` (MinIO)
- Task artifacts: `hiclaw/hiclaw-storage/shared/tasks/{instance_id}/` (MinIO)
