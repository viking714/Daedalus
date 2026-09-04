---
name: ocr-delegate-review
version: 0.2.1
description: Reviewer 专用：以 open-code-review delegate 模式做确定性审查范围界定与规则解析，保证不漏审、结论可定位。
type: mcp-tool
roles: [reviewer]
---

# ocr-delegate-review

## 类型
AgentTeams Skill — MCP 组合工具（`ocr_delegate_preview` / `ocr_delegate_rule`，由宿主侧 `ocr` CLI 提供确定性能力）

## 角色
Reviewer（独占，不给其他角色）

## 功能
把「审哪些文件、每个文件按什么标准审」这两件事交给确定性工具，把「审出什么结论」留给 Reviewer 自身模型。

> **设计边界（不可违反）**：`ocr` 在本链路中**不调用任何 LLM**，也不得在 ocr 侧配置模型端点。
> 它是"确定性约束工具"，不是第二个审查者。一旦让它用自己的模型跑完整审查，就会出现
> 双份 API 账单 + 双份 token 预算，且两个模型的结论冲突时 `verdict` 权威性受损。

| 半边 | 承担者 | 具体职责 |
|------|--------|----------|
| 确定性 | `ocr` delegate | 文件选择（必审 / 排除 + 排除原因）、按语言/路径解析规则清单 |
| 智能 | Reviewer (GLM-5.2) | 逐文件读 diff、判断规则是否成立、定 `failure_class`、裁定 PASS/REJECT |

## 使用场景
- **仅 `evaluating` 阶段**。双闸门（`prd_review` / `design_review`）审的是 PRD 与 ADD 文档，
  diff 审查工具对其无用 —— 因此本 Skill 不进 Reviewer 的通用工具表。
- Bug / incident 修复审查：需要"每一行都被看过"的可证明证据时。
- feature / greenfield 独立验收：作为代码侧证据来源之一（文档侧验收仍走 `result-judge`）。

## 调用顺序（两步，不可跳步）

### Step 1 — `ocr_delegate_preview`：划定必审范围
| 参数 | 必填 | 说明 |
|------|------|------|
| `patch_text` | 是 | Developer 的 `fix.diff` **全文**（上限 `OCR_MAX_PATCH_BYTES`，默认 2 MiB） |
| `repo_path` | 是 | **宿主机**上的 git 仓库路径，必须落在 `OCR_ALLOWED_REPO_ROOTS`（默认 = `SWE_REPO_CACHE`，即 `/tmp/swe-repos`）内 |
| `base_commit` | 是 | 补丁基线；工具在 scratch 内 `checkout base_commit` 后 `git apply patch_text` |
| `task_id` | 否 | 幂等键的一部分；同任务重复调用复用 scratch |
| `exclude` | 否 | 追加排除的 glob（逗号分隔），与 `ocr-rule.json` 的 `exclude` 合并 |
| `background` | 否 | 业务背景文本（≤8000 字符），帮助 ocr 判相关性；**不含**模型推理 |
| `rule_file` | 否 | 覆盖默认团队规约路径 |
| `use_team_rules` | 否 | 传 `false` 关闭规约注入（默认开，走 `OCR_RULE_PATH`） |
| `with_diff` | 否 | 是否附带各文件 diff（默认 `true`，受 `OCR_DIFF_BUDGET_BYTES` 预算约束） |
| `paths` | 否 | 只取指定文件的 diff（预算不足时分批取，避免一次撑爆上下文） |

关键出参（均按 `ocr delegate preview` 的真实 schema 得出）：

```
{ "status": "ok", "scratch_dir": "...", "ocr_mode": "workspace|range|commit",
  "file_list_source": "ocr|git-fallback", "delegate_format": "json|text",
  "total_files": N, "reviewable_count": M, "excluded_count": K,
  "reviewable_files": [{"path","status","insertions","deletions","diff","diff_omitted"}],
  "excluded_files": [{"path","exclude_reason",...}],
  "coverage_contract": {...}, "team_rules_injected": true }
```

### Step 2 — `ocr_delegate_rule`：取回逐文件审查清单
| 参数 | 必填 | 说明 |
|------|------|------|
| `scratch_dir` | 是 | Step 1 返回值，必须位于 `OCR_SCRATCH_DIR` 内 |
| `paths` | 是 | `reviewable_files[].path` 列表（仓内相对路径；绝对路径/越界路径会被拒绝并列入 `rejected_paths`） |
| `rule_file` / `use_team_rules` | 否 | 同 Step 1 |

出参 `rule_groups` 为 delegateRulesJSON 原文：
`{"schema_version":"1","groups":[{"group_id","source":"custom|project|global|system","pattern","files":[],"rule"}]}`。
`source=custom` 表示团队规约生效；`merge_system_rule: true` 时 `rule` 文本已含合并后的系统规则。

**规则清单是 checklist，不是结论。** 每条规则都要给出"成立 / 不成立 / 不适用"及理由。

## 覆盖率硬门（coverage hard gate）
- 分母 = `reviewable_count`。`reviewable_files` 每一项最终必须落在 `reviewed_files`，
  或落在 `skipped_files` 并附**具体理由**；禁止静默省略文件。
- `excluded_files` 是 ocr 的确定性排除（已带 `exclude_reason`），无需逐个审查，
  但必须在 verdict 中原样引用，作为"为何没审"的凭据。
- `coverage_rate = len(reviewed_files) / reviewable_count`；
  **`coverage_rate < 1.0` ⇒ 本次审查无效**，Team Leader 应要求 Reviewer 重跑，而不是接受一个结论。
- 工具不可用（见下）⇒ 覆盖率不可证，属另一类问题，处理方式不同。

## verdict 输出适配
ocr 的行级证据不得绕过 verdict schema 直传，统一归并进 `failure_class.code_review_evidence`：

```json
{
  "failure_class": "code",
  "evidence": "…",
  "code_review_evidence": {
    "tool": "ocr-delegate-review",
    "ocr_version": "1.11.2",
    "delegate_format": "json",
    "file_list_source": "ocr",
    "total_files": 7, "reviewable_count": 6, "excluded_count": 1,
    "reviewed_files": ["app.py", "..."],
    "skipped_files": [],
    "coverage_rate": 1.0,
    "team_rules_injected": true,
    "findings": [
      {"path": "app.py", "line": 128, "category": "bug", "severity": "High",
       "rule": "…（来自 delegate rule 的清单条目）…", "observation": "…",
       "disposition": "confirmed | dismissed | not_applicable", "reason": "…"}
    ]
  }
}
```

`findings` 只收录 Reviewer 判定 `confirmed` 的条目作为驳回依据；`dismissed` / `not_applicable`
需保留并附理由（这是"我看过并否掉了"的证据，比只报问题更有审计价值）。

## 严重度处置约定
| ocr 侧严重度 | 处置 |
|--------------|------|
| Critical / High | **必须逐条明确回应**（confirmed → 计入驳回；dismissed → 写出为什么是误报） |
| Medium | 结合上下文判断，报告时附上下文 |
| Low | 静默丢弃，不占 verdict 篇幅 |

Critical/High 的"必须回应"是**流程强制**，不是**自动驳回**：裁定权始终在 Reviewer。

## 优雅降级契约
`status = "unavailable"`（ocr 未安装 / 版本过低 / git 失败）时：
- 服务不崩，Reviewer 降级为纯 LLM 审查；
- **必须**在 verdict 中显式声明「本次未使用确定性文件清单，coverage 不可证」，
  使 Team Leader 能区分"审得干净"与"没法证明审干净"；
- 恢复动作见返回体的 `recovery` 字段（重跑 `make install` → `ensure_ocr`，或手工装 `ocr`）。

`status = "error"` 且 `reason` 含"补丁应用失败"时，该失败**本身就是高价值审查信号**
（基线错位 / 过度修改），应作为 `failure_class=code` 的证据写入 verdict，而不是重试到超时。

## 依赖
- 宿主侧：`ocr` CLI（单 Go 二进制，另需 Git ≥ 2.41）、Git、仓库克隆缓存 `SWE_REPO_CACHE`
- MCP 工具：`ocr_delegate_preview` → `ocr_delegate_rule`
- 团队规约：`deploy/rules/ocr-rule.json`（`OCR_RULE_PATH` 可覆盖；设为 `none` 关闭注入）
- 环境变量：`OCR_BIN` / `OCR_SCRATCH_DIR` / `OCR_ALLOWED_REPO_ROOTS` / `OCR_MAX_PATCH_BYTES`
  / `OCR_DIFF_BUDGET_BYTES` / `OCR_TIMEOUT_SEC`

## 前置步骤（Worker 侧，必须写进 workflow）
Reviewer 容器从 MinIO 拿到的是 `fix.diff` 文本，而 `ocr` 需要 git 工作树。
**不要**在容器内 `git apply` —— 直接把 diff 文本作为 `patch_text` 传给 MCP 工具，
重建动作由宿主机 scratch 完成。若自行在容器里 apply，会污染只读工作目录并破坏基线一致性。

## 权限与安全
- 只读契约保持：本工具链**不写业务仓库**，scratch 建在 `OCR_SCRATCH_DIR` 下的哈希隔离目录。
- `repo_path` 白名单 + `paths` 越界/绝对路径拒绝 —— MCP 端点对网络开放，Worker 输入按不可信处理。
- 就绪标记落在工作树**之外**，避免被 `git ls-files --others` 当成未跟踪文件计入审查分母。
- 不产生独立模型账单：delegate 模式下 token 全部归入 Reviewer 自身预算。

## 失败处理
| 情况 | 行为 |
|------|------|
| ocr 未安装 / 不可执行 | `status=unavailable`，降级纯 LLM + 声明 coverage 不可证 |
| `--format json` 不被支持 | 自动去掉该 flag 重跑一次，`delegate_format=text`，文件清单由 git 兜底（`file_list_source=git-fallback`） |
| JSON 结构变化 | 不臆造字段，回退 git 兜底并附 `raw_output`；`schema_version != "1"` 时给 `schema_note` |
| 团队规约文件损坏 | **跳过注入**并回 `team_rules_note`（ocr 对坏 `--rule` 文件是硬失败，宁可无规约也不能整次报错） |
| diff 回传超预算 | 标记 `diff_omitted` + `diff_budget_note`，用 `paths` 分批取 |
| 补丁应用失败 | `status=error` + `hint`，转成驳回证据 |

## 复用价值
**高**。任何"diff 级质量门禁"场景都可复用；把 `deploy/rules/ocr-rule.json` 换掉即可迁移团队规约。
对 SWE-bench 评测尤其实用：行级证据回灌 Developer 时比纯文字 feedback 更可执行。
