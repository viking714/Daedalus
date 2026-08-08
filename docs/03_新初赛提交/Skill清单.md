# Skill 清单

> 版本：v1.0 | 日期：2026-08-05
>
> 本文件为「研发缺陷闭环协同系统」全部 Skill 的统一清单。依据 `方案设计.md` v2.0 §3.2 提炼，覆盖 **15 个 AgentTeams Skills**（6 个 prompt-only + 9 个 with-scripts）。
>
> **使用方式**：
> - 新增 Skill 时，复制「空白模板」章节，按字段填写完整。
> - 修改 Skill 行为时，同步更新本文件对应条目，并标注变更版本。
> - 跨角色协作时，本文件是「我手里有什么 Skill、能用来做什么、需要什么前置条件」的统一参考。

---

## 一、字段定义

下表为每个 Skill 条目必须包含的 10 个字段。**所有字段均为必填**，"无"或"暂未启用"需显式标注。

| 字段 | 填写说明 |
|------|----------|
| **Skill 名称** | Skill 的名称或代号。命名规则：小写英文短词 + 连字符（如 `code-search` / `lesson-lookup`），与 `deploy/packages/rd-defect-skills/skills/<name>/SKILL.md` 目录名保持一致。 |
| **Skill 类型** | 官方云 Skill / 自定义 Skill / 外部工具封装 / 企业系统集成能力等。本系统全部为 **AgentTeams 自定义 Skills**，再细分 `prompt-only`（仅封装使用规范）和 `with-scripts`（含 scripts/ 脚本）。 |
| **使用场景** | 说明该 Skill 适用于哪类任务或环节。具体到哪个角色在哪个阶段使用。 |
| **输入参数** | 说明调用该 Skill 需要哪些输入。逐条列出字段、来源、格式要求。 |
| **输出结果** | 说明该 Skill 输出哪些结构化或非结构化结果。明确格式（JSON / Markdown / diff / 字符串）。 |
| **调用条件** | 说明在什么条件下触发调用。包括状态机阶段、角色、前置 Skill 完成情况。 |
| **依赖工具/系统** | 说明该 Skill 依赖的云产品、企业系统、MCP 工具、数据库或外部 API。 |
| **失败处理** | 说明调用失败、结果异常或超时时的处理方式。明确降级策略与升级路径。 |
| **权限与安全** | 说明调用过程中的权限控制、密钥管理和风险边界。 |
| **复用价值** | 说明该 Skill 是否可在其他 Agent 或其他场景中复用。 |

---

## 二、Skill 分类总览

| 类型 | 数量 | Skill 列表 |
|------|:----:|------------|
| **prompt-only** | 6 | `code-read`、`bash-exec`、`test-run`、`patch-generate`、`result-judge`、`multi-file-edit` |
| **with-scripts** | 9 | `repo-index`、`code-search`、`impact-analysis`、`root-cause-analysis`、`repair-planning`、`pipeline-router`、`module-lookup`、`lesson-lookup`、`knowledge-extraction` |
| **合计** | **15** | — |

> 全部 Skill 打包在 `deploy/packages/rd-defect-skills-vX.Y.Z.zip` 中，通过 Worker YAML 的 `spec.package` 字段 pinned 版本引用。

---

## 三、prompt-only Skills（6 个）

> 由 Worker runtime 直接提供执行能力，Skill 仅封装使用规范（含 prompt 指令与 LLM 使用约定）。

### 3.1 code-read

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `code-read` |
| **Skill 类型** | AgentTeams Skills - prompt-only |
| **使用场景** | 读取代码文件，支持指定行范围与上下文窗口。Analyzer 阅读源码、Fixer 读取待修改文件、Evaluator 审查 diff 时使用。 |
| **输入参数** | ① `file_path`（必填，仓库内相对路径）<br>② `start_line` / `end_line`（可选，行号范围）<br>③ `context_window`（可选，默认 ±10 行） |
| **输出结果** | 文件内容（含行号标记的字符串），超出范围时按窗口截断并提示。 |
| **调用条件** | 任何需要读取源码的环节均可调用；Fixer 调用时需先通过 `pipeline-router` 确认本轮编辑范围。 |
| **依赖工具/系统** | Worker 原生工具 `file r/w`（只读模式）。 |
| **失败处理** | 文件不存在 → 返回错误码 + 建议路径；权限拒绝 → escalate 给 Manager；编码异常 → 自动尝试 UTF-8 / GBK 降级。 |
| **权限与安全** | 沙箱路径限制（仅仓库工作区内）；只读访问；不返回 `.env` / `secrets/` 等敏感目录。 |
| **复用价值** | **高**。Manager/Analyzer/Fixer/Evaluator 四个角色均使用，可推广到任何"阅读代码"场景。 |

### 3.2 bash-exec

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `bash-exec` |
| **Skill 类型** | AgentTeams Skills - prompt-only |
| **使用场景** | 安全执行 bash 命令。Tester 执行 pytest、Fixer 跑 git diff、Manager 调外部脚本、Analyzer 运行复现脚本。 |
| **输入参数** | ① `command`（必填，shell 命令字符串）<br>② `cwd`（可选，工作目录）<br>③ `timeout_sec`（可选，默认 60s，上限 600s）<br>④ `env_overrides`（可选，临时环境变量） |
| **输出结果** | `stdout` / `stderr` / `exit_code` / `duration_ms` 四个字段。 |
| **调用条件** | 任何需要执行 shell 命令时；白名单外的命令直接拒绝。 |
| **依赖工具/系统** | Worker 原生工具 `bash`（带白名单 + 超时）。 |
| **失败处理** | 白名单拦截 → 返回拒绝原因；超时 → SIGTERM → 强制 kill 进程；非零退出码 → 返回完整 stderr 供调用方判断。 |
| **权限与安全** | **白名单机制**（仅允许安全命令如 `git` / `pytest` / `pip` / `python` 等）；超时强制终止；禁止 `rm -rf` / `sudo` / 反弹 shell 等高危命令；不继承宿主机环境变量。 |
| **复用价值** | **高**。五个角色均使用，是事实上的"通用执行入口"。 |

### 3.3 test-run

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `test-run` |
| **Skill 类型** | AgentTeams Skills - prompt-only |
| **使用场景** | 执行测试并解析 pytest 输出。Tester 真实测试执行阶段。 |
| **输入参数** | ① `test_cmd`（默认 `pytest -v --tb=short`）<br>② `target_files`（可选，限定测试文件）<br>③ `extra_args`（可选，如 `-k pattern`） |
| **输出结果** | `test_report.json`：含 `passed_cases` / `failed_cases` / `error_type` / `traceback` / `failing_line` / `duration_sec`。 |
| **调用条件** | Tester 进入 `testing` 状态、已应用 `fix.diff`、venv 已搭建完成后触发。 |
| **依赖工具/系统** | `bash-exec`、Python venv、pytest。 |
| **失败处理** | pytest 解析失败 → 返回原始 stdout/stderr；环境异常（缺包/版本冲突）→ escalate；同一 diff 连续 3 轮失败 → escalate。 |
| **权限与安全** | 在隔离 venv 内执行，不污染主环境；超时 10 分钟强制终止；不联网下载未声明依赖。 |
| **复用价值** | **中**。主要 Tester 使用；扩展多语言时复用同一执行框架（Java/JS/Go 各实现一层）。 |

### 3.4 patch-generate

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `patch-generate` |
| **Skill 类型** | AgentTeams Skills - prompt-only |
| **使用场景** | 生成统一格式 diff。Fixer 完成多文件编辑后，汇总产出 `fix.diff`。 |
| **输入参数** | ① `repo_path`（仓库根路径）<br>② `base_commit`（基准 commit SHA）<br>③ `target_files`（修改文件列表，可选，默认全部） |
| **输出结果** | unified diff 格式字符串（`--- a/...` / `+++ b/...` 头），写入 `fix.diff` 文件。 |
| **调用条件** | Fixer 完成所有 `edit_file` 调用后、`pipeline-router` 状态推进到 `testing` 之前调用。 |
| **依赖工具/系统** | Worker 原生工具 `git diff`。 |
| **失败处理** | 无 git 仓库 → 返回错误；非 UTF-8 文件 → 提示二进制文件单独处理；编码异常 → 回退到文件级 diff。 |
| **权限与安全** | 只读访问 git 历史；不修改任何文件；不暴露敏感信息（如密钥、token）。 |
| **复用价值** | **中**。主要 Fixer 使用；任何需要产出"代码变更"产物的场景可复用。 |

### 3.5 result-judge

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `result-judge` |
| **Skill 类型** | AgentTeams Skills - prompt-only |
| **使用场景** | 基于结果裁定通过/驳回。Tester 反馈失败时识别"是否值得重试"、Evaluator 四维度审查。 |
| **输入参数** | ① `artifact`（待审查产物，如 `fix.diff` + `test_report.json`）<br>② `criteria`（评分维度：正确性/完整性/一致性/质量）<br>③ `threshold`（通过阈值） |
| **输出结果** | `verdict.json`：`decision` (pass/reject) / `scores` (四维度明细) / `reasons` (驳回原因)。 |
| **调用条件** | Tester 完成一轮测试后、Evaluator 进入 `evaluating` 阶段时触发。 |
| **依赖工具/系统** | 无外部依赖（纯 LLM 推理）。 |
| **失败处理** | 四维度评分内部矛盾（如正确性高分但质量低分）→ escalate；评分置信度 < 0.6 → escalate。 |
| **权限与安全** | 仅生成判定，不修改任何数据；不读取敏感文件。 |
| **复用价值** | **高**。Tester/Evaluator 共用，可推广到任何"质量评估"环节（如 PR 审查、文档质量检查）。 |

### 3.6 multi-file-edit

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `multi-file-edit` |
| **Skill 类型** | AgentTeams Skills - prompt-only |
| **使用场景** | 多文件协调编辑。Fixer 单轮需修改多文件时，按 `repair-planning` 输出的方案顺序编辑。 |
| **输入参数** | ① `edit_plan`（文件路径 + 目标修改的列表）<br>② `atomic`（是否原子化，默认 true） |
| **输出结果** | 各文件编辑结果（成功/失败/行号变化）、整体一致性报告。 |
| **调用条件** | Fixer 处于 `fixing` 状态、`repair-planning` 已输出方案、文件数 ≤ 5。 |
| **依赖工具/系统** | Worker 原生工具 `edit_file`、`git`。 |
| **失败处理** | 单文件失败 → `atomic=true` 时回滚所有文件；记录失败文件供下一轮 `repair-planning` 调整方案。 |
| **权限与安全** | 沙箱路径限制；禁止修改 `.git/` / `.env` / `secrets/`；编辑前自动备份（`*.bak`）。 |
| **复用价值** | **中**。主要 Fixer 使用；任何"批量修改代码"场景可复用。 |

---

## 四、with-scripts Skills（9 个）

> Skill 内部通过 `scripts/*.py` 脚本编排 MCP 原语，完成特定工作流。脚本可独立测试、可显式埋 OTel Span。

### 4.1 repo-index

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `repo-index` |
| **Skill 类型** | AgentTeams Skills - with-scripts（核心脚本：`index.py`） |
| **使用场景** | 增量索引：分块 → 嵌入 → 三库写入（pgvector + Neo4j + Meilisearch）。Manager 首次全量索引 + 后续增量更新。 |
| **输入参数** | ① `repo_path`<br>② `repo_name`<br>③ `commit_sha`<br>④ `full_reindex`（布尔，默认 false） |
| **输出结果** | 索引统计（`chunk_count` / `file_count` / `duration_sec`）、Redis 中更新 `repo_state` hash、嵌入缓存命中率。 |
| **调用条件** | Manager 收到新任务且 Redis 中 `repo_state` 与目标 commit 不一致时触发。 |
| **依赖工具/系统** | MCP 原语：`pgvector_upsert_chunk` / `meili_keyword_search`（写入端）/ `neo4j_*`（图谱写入）/ `embed_texts` / `redis_get_repo_state` / `redis_set_repo_state` / `redis_fetch_embedding` / `ast_parse_file`。<br>数据库：pgvector、Neo4j、Meilisearch、Redis。 |
| **失败处理** | 单文件嵌入失败 → 跳过该文件、记录 `failed_files`、下次增量补充；三库部分失败 → 按库独立重试，最终一致性；P99 目标 ≤ 60s，超时则降级为分批处理。 |
| **权限与安全** | 数据库连接凭证通过 `deploy/install/agentteams.env` 注入；输入校验（路径防穿越）；审计日志记录每次索引操作。 |
| **复用价值** | **中**。仅 Manager 在调度初期使用；可作为独立"仓库索引"服务推广到其他需要代码检索的场景。 |

### 4.2 code-search

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `code-search` |
| **Skill 类型** | AgentTeams Skills - with-scripts（核心脚本：`context_packer.py`） |
| **使用场景** | 语义+关键词搜索+结果打包。Analyzer 根因分析、Manager 初步检索时使用。 |
| **输入参数** | ① `query_text`<br>② `top_k`（默认 10）<br>③ `repo_filter`（可选）<br>④ `strategy`（semantic / keyword / hybrid，默认 hybrid） |
| **输出结果** | 结构化 Top-K 结果：`file_path` / `line_range` / `score` / `snippet` / `context_window`。 |
| **调用条件** | Analyzer 进入 `analyzing` 阶段、Manager 调度初期需对 Issue 做初步匹配时触发。 |
| **依赖工具/系统** | MCP 原语：`hybrid_search`（共享服务，RRF 融合）/ `pgvector_search` / `meili_keyword_search` / `neo4j_expand_chunks`（对融合后 chunks 做调用链扩展）/ `embed_texts`。<br>脚本：`context_packer.py`（打包为带上下文的结构化结果）。 |
| **失败处理** | 召回为空 → 降级为纯关键词搜索；RRF 融合超时 → 返回单路 Top-K；Neo4j 扩展失败 → 仅返回融合结果，标记"无调用链上下文"。 |
| **权限与安全** | 只读数据库查询；输入 query 长度限制（≤ 2KB）；审计日志记录每次查询的 `query_hash` 与命中数。 |
| **复用价值** | **高**。Analyzer / Fixer / Manager 均可调用；可作为通用"代码语义搜索"服务暴露给其他系统。 |

### 4.3 impact-analysis

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `impact-analysis` |
| **Skill 类型** | AgentTeams Skills - with-scripts（核心脚本：`dep_graph.py` / `contract_check.py`） |
| **使用场景** | 依赖图分析与契约检查。Analyzer 评估影响面、Evaluator 审查一致性时使用。 |
| **输入参数** | ① `target_symbols`（函数/类/模块列表）<br>② `depth`（图遍历深度，默认 2）<br>③ `check_contract`（布尔，是否检查 API 契约） |
| **输出结果** | 影响面统计（`affected_files` / `affected_functions` / `affected_callers`）、契约违规列表（破坏的接口签名、不兼容的返回类型）。 |
| **调用条件** | Analyzer 完成根因定位后、Fixer 制定修复方案前；Evaluator 审查 patch 时再次确认影响面。 |
| **依赖工具/系统** | MCP 原语：`neo4j_impact_stats` / `neo4j_dep_subgraph` / `neo4j_symbol_lookup` / `ast_parse_file`。<br>数据库：Neo4j 图谱。 |
| **失败处理** | 图谱数据缺失 → 返回空集 + 提示"需先 `repo-index`"；契约解析失败 → 跳过该模块并记录警告。 |
| **权限与安全** | 只读 Neo4j；输入符号白名单（仅仓库内符号）；超时 30s。 |
| **复用价值** | **高**。Analyzer / Evaluator 共用；可推广到"代码变更风险评估"通用场景。 |

### 4.4 root-cause-analysis

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `root-cause-analysis` |
| **Skill 类型** | AgentTeams Skills - with-scripts（核心脚本：`root_cause.py`） |
| **使用场景** | 根因推断与复现脚本生成。Analyzer 在 `analyzing` 阶段整合多源信息后，产出结构化根因报告。 |
| **输入参数** | ① `issue_text`<br>② `search_results`（来自 `code-search`）<br>③ `impact_analysis`（来自 `impact-analysis`）<br>④ `historical_lessons`（来自 `lesson-lookup`） |
| **输出结果** | `analysis_report.json`（结构化根因 + 修复策略）+ `root_cause_report.md`（人类可读）+ 可选 `reproduce.py`（复现脚本）。 |
| **调用条件** | Analyzer 完成 `code-search` / `impact-analysis` / `lesson-lookup` 三个前置 Skill 后调用。 |
| **依赖工具/系统** | 编排 `code-search` / `impact-analysis` / `lesson-lookup` Skill；MCP 原语：`embed_texts`（用于根因向量化）。 |
| **失败处理** | 根因不明确 → 输出 2~3 个候选根因 + 置信度；复现脚本生成失败 → 仅输出分析报告；置信度 < 0.5 → escalate。 |
| **权限与安全** | 仅生成报告，无副作用；不修改任何文件；LLM 输出经格式校验。 |
| **复用价值** | **中**。主要 Analyzer 使用；可作为"故障根因分析"通用能力推广到 SRE / 运维场景。 |

### 4.5 repair-planning

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `repair-planning` |
| **Skill 类型** | AgentTeams Skills - with-scripts（核心脚本：`repair_plan.py` / `risk_gate.py`） |
| **使用场景** | 修复方案规划与风险闸门。Fixer 在编辑前制定具体方案，并通过风险闸门决定是否执行。 |
| **输入参数** | ① `root_cause_report`<br>② `impact_analysis`<br>③ `historical_lessons`（fix_pattern） |
| **输出结果** | 修复方案（`file_edits` 列表，每项含文件路径、目标函数、修改类型、改动摘要）+ 风险等级（L0–L3）+ 闸门决策（pass / warn / block）。 |
| **调用条件** | Fixer 进入 `fixing` 状态、Analyzer 已输出根因报告、Fixer 未做实际编辑之前。 |
| **依赖工具/系统** | MCP 原语：`neo4j_impact_stats`（再确认影响面）。<br>脚本：`repair_plan.py`（LLM 推理生成方案）/ `risk_gate.py`（规则化风险评估）。 |
| **失败处理** | 风险等级 ≥ L3 → block 并 escalate；方案涉及 >5 文件 → 拆分多轮；`risk_gate` 与 `impact-analysis` 结论矛盾 → 保守侧生效。 |
| **权限与安全** | 仅生成方案，无副作用；风险等级评估由 `risk_gate.py` 规则化（不依赖 LLM 主观判断）；不读取敏感文件。 |
| **复用价值** | **中**。主要 Fixer 使用；可推广到"代码变更方案设计"通用场景。 |

### 4.6 pipeline-router

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `pipeline-router` |
| **Skill 类型** | AgentTeams Skills - with-scripts（核心脚本：`task_router.py` / `state_manager.py` / `handoff.py` / `loop_judge.py`） |
| **使用场景** | 任务路由、状态推进、人工交接。Manager 在每个阶段过渡时调用，决定下游 Agent 与状态切换。 |
| **输入参数** | ① `task_id`<br>② `current_state`（TaskState）<br>③ `current_artifact`（当前阶段产物） |
| **输出结果** | 派单指令（`assignee` + `payload`）+ 状态推进指令（`next_state`）+ 人工交接消息（`matrix_message`，可选）。 |
| **调用条件** | Manager 每次收到 Worker 产出后、推进状态机前调用；`loop_judge` 检测到重复失败时触发人工交接。 |
| **依赖工具/系统** | Matrix 联邦通信；TaskState 持久化（MinIO / Redis）；MCP 原语：`redis_set_repo_state`（状态快照）。<br>脚本：`task_router.py`（路由决策）/ `state_manager.py`（状态机管理）/ `handoff.py`（人工交接）/ `loop_judge.py`（循环判定）。 |
| **失败处理** | 状态机冲突 → 回滚到上一稳定态；Matrix 通信失败 → 重试 3 次后 escalate；`regression_cycle_count >= 3` → 强制 `escalated`。 |
| **权限与安全** | 状态变更幂等（同一 `task_id` + 同一 `current_state` 不重复派单）；人工交接走 Matrix 加密通道；不暴露内部状态机细节给外部。 |
| **复用价值** | **中**。仅 Manager 使用；可作为"多阶段任务调度"通用模式推广到其他多 Agent 协作场景。 |

### 4.7 module-lookup

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `module-lookup` |
| **Skill 类型** | AgentTeams Skills - with-scripts（核心脚本：`module_lookup.py`） |
| **使用场景** | 模块定位与入口点发现。Analyzer 在初步分析时快速定位"Issue 涉及哪个模块、入口在哪"。 |
| **输入参数** | ① `keyword`（模块名/关键词/Issue 摘要）<br>② `repo_filter`（可选）<br>③ `top_k`（默认 5） |
| **输出结果** | 候选模块列表：`module_path` / `entry_points`（入口函数/类）/ `related_symbols` / `score`。 |
| **调用条件** | Analyzer 在调用 `code-search` 之前做"模块级预筛"时触发；Manager 调度初期也可用。 |
| **依赖工具/系统** | MCP 原语：`neo4j_symbol_lookup` / `neo4j_dep_subgraph` / `pgvector_search`（语义匹配）。<br>数据库：Neo4j + pgvector。 |
| **失败处理** | 未找到 → 返回相邻模块候选 + 提示手动指定；图谱缺失 → 降级为纯语义搜索。 |
| **权限与安全** | 只读 Neo4j + pgvector；输入 query 长度限制（≤ 1KB）；超时 15s。 |
| **复用价值** | **中**。主要 Analyzer 使用；可推广到"代码导航"通用工具。 |

### 4.8 lesson-lookup

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `lesson-lookup` |
| **Skill 类型** | AgentTeams Skills - with-scripts（核心脚本：`lesson_lookup.py`，v1.9 新增） |
| **使用场景** | 按角色模式查询 `lessons` 历史经验并按相似度 score 分级。Analyzer 按根因维度查询（mode=analyzer）、Fixer 按改法维度查询（mode=fixer）。 |
| **输入参数** | ① `query_text`（Issue 描述 或 `fix_pattern` + `error_signature`）<br>② `mode`（`analyzer` / `fixer`）<br>③ `repo_filter`<br>④ `top_k`（analyzer 默认 5，fixer 默认 3）<br>⑤ `success_only`（fixer 模式默认 true） |
| **输出结果** | 匹配 lessons 列表（按 score 三级分流）：`HIGH` (≥0.85) / `MEDIUM` (0.60~0.85) / `LOW` (<0.60)，每条含 `lesson_id` / `root_cause` / `fix_pattern` / `diff_summary` / `edge_cases` / `merge_count`。 |
| **调用条件** | Analyzer 在根因分析前/中调用（mode=analyzer）；Fixer 在生成 patch 前调用（mode=fixer，仅 `success = true`）。 |
| **依赖工具/系统** | MCP 原语：`pgvector_search` / `embed_texts`（query 向量化）。<br>数据库：PostgreSQL `lessons` 表（pgvector 存储 `root_cause_vec`）。<br>脚本：`lesson_lookup.py`（业务编排：相似度计算 + 分级 + 结果格式化）。 |
| **失败处理** | 召回为空 → 返回 `LOW` 级别空集，提示"按标准流程执行"；向量检索超时 → 降级为标签匹配；score 计算异常 → 标记为 `LOW` 保守处理。 |
| **权限与安全** | 只读 `lessons` 表；输入 query 长度限制（≤ 2KB）；不返回 `merge_count < 1` 的低可信度记录；不暴露 `success = false` 的失败案例给 Fixer。 |
| **复用价值** | **高**。Analyzer / Fixer 共用，是经验沉淀闭环的关键入口；可推广到任何"基于历史经验做决策"的场景（如客服 FAQ 推荐、运维故障处理）。 |

### 4.9 knowledge-extraction

| 字段 | 内容 |
|------|------|
| **Skill 名称** | `knowledge-extraction` |
| **Skill 类型** | AgentTeams Skills - with-scripts（核心脚本：`extract.py`） |
| **使用场景** | 从修复结果提取模式与经验。Evaluator 裁定完成后，将本次修复的结构化经验写入 `lessons` 表。 |
| **输入参数** | ① `fix_diff`<br>② `test_report`<br>③ `verdict`<br>④ `root_cause_report`<br>⑤ `task_id` / `repo` / `retry_count`（来自 TaskState） |
| **输出结果** | 新写入的 `lessons` 记录（含 `id` / `root_cause` / `fix_pattern` / `error_signature` / `fix_strategy` / `affected_modules` / `tags` / `diff_summary` / `test_changes` / `edge_cases` / `success` / `resolution_summary` / `retry_count` / `merge_count` / `related_to`）。 |
| **调用条件** | Evaluator 产出 `verdict.json` 后立即调用；不论 pass/reject 均触发（失败经验同样有价值）。 |
| **依赖工具/系统** | MCP 原语：`pgvector_search`（去重比对）/ `pgvector_upsert_chunk`（写入）/ `embed_texts`（向量化）。<br>数据库：PostgreSQL `lessons` 表。<br>脚本：`extract.py`（业务编排：字段抽取 + 写入前去重 + MERGE/SIMILAR/NEW 决策 + 合并规则）。 |
| **失败处理** | 字段抽取失败 → 跳过该字段、记录 `incomplete_fields`；写入前去重比对失败 → 保守标记为 `NEW`；数据库写入失败 → 重试 3 次、最终失败时记录日志（不阻塞主流程）。 |
| **权限与安全** | 写 `lessons` 表权限受最小化控制（仅 Evaluator 角色持有写凭证）；输入校验（防注入）；审计日志记录每次写入；写入前自动去重避免经验库膨胀。 |
| **复用价值** | **中**。仅 Evaluator 使用；可作为"经验沉淀引擎"独立推广到其他需要"案例 → 知识"转化的场景。 |

---

## 五、Skill 依赖与调用矩阵

### 5.1 按角色 × Skill 矩阵

> 符号：● 主用 / ○ 备用 / — 不使用

| Skill | Manager | Analyzer | Fixer | Tester | Evaluator |
|-------|:------:|:--------:|:-----:|:------:|:---------:|
| `code-read` | ○ | ● | ● | — | ● |
| `bash-exec` | ● | ● | ● | ● | ● |
| `test-run` | — | — | — | ● | — |
| `patch-generate` | — | — | ● | — | — |
| `result-judge` | — | — | — | ● | ● |
| `multi-file-edit` | — | — | ● | — | — |
| `repo-index` | ● | — | — | — | — |
| `code-search` | ○ | ● | ○ | — | — |
| `impact-analysis` | — | ● | — | — | ● |
| `root-cause-analysis` | — | ● | — | — | — |
| `repair-planning` | — | — | ● | — | — |
| `pipeline-router` | ● | — | — | — | — |
| `module-lookup` | — | ● | — | — | — |
| `lesson-lookup` | — | ● | ● | — | — |
| `knowledge-extraction` | — | — | — | — | ● |

### 5.2 Skill 编排关系（with-scripts 之间）

```text
root-cause-analysis
    ├── code-search（前置）
    ├── impact-analysis（前置）
    └── lesson-lookup（前置，mode=analyzer）

repair-planning
    ├── impact-analysis（前置，再确认）
    └── lesson-lookup（前置，mode=fixer）

knowledge-extraction（Evaluator 内部）
    └── 直接调用 MCP 原语（pgvector_search / pgvector_upsert_chunk / embed_texts）
```

---

## 六、变更记录

| 版本 | 日期 | 变更内容 | 作者 |
|:----:|------|----------|------|
| v1.0 | 2026-08-05 | 初版：基于方案设计 v2.0 §3.2 提炼全部 15 个 Skill（6 prompt-only + 9 with-scripts）的完整清单 | — |
