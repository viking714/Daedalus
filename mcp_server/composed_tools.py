"""领域技能实现层（业务层）。

设计说明：
- Registry 模式：`@register(name, role, desc)` 注册技能，mcp_server.py
  仅按名分发，新增技能零侵入。
- 分层：本文件=业务逻辑；db/ = 数据访问(Repository)；embed/ = 嵌入(Strategy)；
  code/ = AST 解析。业务层只调用这些层的方法，不直接碰连接/SQL/Cypher。
- 不重复造轮子：pgvector/neo4j/meili/redis 用官方客户端；AST 用 tree-sitter；
  嵌入用 fastembed/OpenAI；混合检索用标准 RRF(Reciprocal Rank Fusion) 融合。
- 优雅降级：任何存储/依赖不可用时抛 DbUnavailable，技能捕获后返回结构化
  `{"status":"unavailable",...}`，服务整体不崩。
- 闭环阈值（设计硬指标）在 state_manager / task_router 强制：
  MAX_ROUND=3、MAX_FILES=5、TASK_TIMEOUT_MIN=30、TOKEN_BUDGET=100000。

架构说明（MCP Server 模式）：
- 本模块仅包含需要数据库/共享状态的技能（通过 MCP Server 暴露给 Worker）。
- 文件操作类技能（read_code / run_bash / write_reproduction / patch_generator /
  test_runner / multi_file_editor）已移至 Worker 容器内原生执行，
  不再注册于此。Worker 通过 AgentTeams 内置工具直接操作本地文件。
- Manager 负责 git clone 目标仓库并推送到 MinIO 共享存储（bash: tar + mc cp），
  Worker 从 MinIO 拉取（bash: mc cp + tar xzf），在容器内本地修改后推送回 MinIO，
  下一个 Worker 再拉取更新后的仓库/产物继续工作。

角色架构（对齐补充设计文档 v1.2 / 方案设计 v2.2）：
- Team Leader（Orchestrator）：task_router / state_manager / handoff_manager
- Architect：semantic_search / kg_query / module_lookup / repo_indexer / context_packer / root_cause_analyzer
  （注：hybrid_search 为 mcp_primitives 层的共享服务，由 semantic_search 复用，不在此重复注册）
- Developer：repair_planner / risk_gate
- Tester：（result_judge 已转为 prompt-only Skill，由 Worker runtime 直接执行，不在此注册）
- Reviewer：dep_graph_analyzer / contract_checker / knowledge_extraction
             / ocr_delegate_preview / ocr_delegate_rule（open-code-review 确定性层）
"""

import os
import re
import sys
import uuid
import shutil
import hashlib
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("skills")

from mcp_server.db.base import DbUnavailable, content_hash
from mcp_server.db.pgvector import PgVectorStore
from mcp_server.db.neo4jgraph import Neo4jStore
from mcp_server.db.meili import MeiliStore
from mcp_server.db.redis_cache import RedisCache
from mcp_server.db.schema import ensure_all
from mcp_server.db.lessons import LessonsStore
from mcp_server.embed.embeddings import EmbeddingService
from mcp_server.code.ast_parser import AstParser
from mcp_server.mcp_primitives import hybrid_search

# --------------------------------------------------------------------------- #
# Registry（注册表模式）
# --------------------------------------------------------------------------- #


@dataclass
class SkillDef:
    name: str
    owner_role: str
    description: str
    handler: Callable[[dict], dict]


_REGISTRY: Dict[str, SkillDef] = {}


def register(name: str, owner_role: str, description: str = ""):
    def _deco(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
        _REGISTRY[name] = SkillDef(name, owner_role, description, fn)
        return fn

    return _deco


def get_skill(name: str) -> Optional[SkillDef]:
    return _REGISTRY.get(name)


def list_skill_defs() -> List[SkillDef]:
    return sorted(_REGISTRY.values(), key=lambda d: d.name)


def is_registered(name: str) -> bool:
    """判断技能名是否已注册（供 server 路由分发做合法性校验）。"""
    return name in _REGISTRY


# --------------------------------------------------------------------------- #
# 闭环阈值（设计硬指标）
# --------------------------------------------------------------------------- #

MAX_ROUND = 3
MAX_FILES = 5
TASK_TIMEOUT_MIN = 30
TOKEN_BUDGET = 100000

# 灰度发布阈值（对齐方案设计 v2.2 §4.5 / §4.6）
REGRESSION_CYCLE_MAX = 3      # 回归闭环上限，超限转 escalated
CANARY_TIMEOUT_MIN = 24 * 60  # awaiting_release 超时哨兵 TTL（默认 24h）

# 从技能包路由脚本导入单一 truth；失败时回退到本地简化表
try:
    _skills_router_path = os.path.join(
        os.path.dirname(__file__), "..", "deploy", "packages", "rd-defect-skills",
        "skills", "pipeline-router", "scripts"
    )
    _skills_router_path = os.path.abspath(_skills_router_path)
    if _skills_router_path not in sys.path:
        sys.path.insert(0, _skills_router_path)
    from task_router import _PIPELINES as _SKILL_PIPELINES
except Exception:  # pragma: no cover
    _SKILL_PIPELINES = None


# 终态集合（任务仅在这些状态真正结束）
_TERMINAL_STAGES = {"resolved", "escalated"}


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rand(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- #
# Orchestrator 主控技能（task_router / state_manager / handoff_manager）
# --------------------------------------------------------------------------- #


@register("task_router", "Team Leader", "根据任务与当前阶段路由；达最大轮次转人工移交，awaiting_release 后由灰度事件驱动")
def skill_task_router(payload: dict) -> dict:
    src = payload.get("source") or {}
    current_stage = payload.get("current_stage") or (src.get("stage") if isinstance(src, dict) else None)
    round_ = payload.get("round")

    # 达最大轮次 → 人工介入（终态 escalated）
    if round_ is not None and round_ >= MAX_ROUND:
        return {"next_agent": None, "next_stage": "escalated", "reason": f"round {round_} >= max_round {MAX_ROUND}"}

    # 终态不推进
    if current_stage in _TERMINAL_STAGES:
        return {"next_agent": None, "next_stage": current_stage, "reason": "terminal stage"}

    # 选择流水线
    task_type = payload.get("task_type") or src.get("task_type") or "bug"
    greenfield = bool(payload.get("greenfield") or src.get("greenfield"))
    failure_class = payload.get("failure_class") or src.get("failure_class")

    # 回退仲裁
    from task_router import _FAILURE_ROLLBACK as _fb
    if failure_class and failure_class in _fb:
        target_stage, target_agent = _fb[failure_class]
        return {"next_agent": target_agent, "next_stage": target_stage, "reason": f"rollback by failure_class={failure_class}"}

    # 入口：received / 无状态 → analyzing
    if not current_stage or current_stage == "received":
        if task_type == "incident":
            return {"next_agent": "ops-analyst", "next_stage": "ops_diagnosing", "reason": "entry from received (incident)"}
        return {"next_agent": "architect", "next_stage": "analyzing", "reason": "entry from received"}

    # 常规流水线推进
    if _SKILL_PIPELINES:
        if task_type == "incident":
            pipeline = _SKILL_PIPELINES["incident"]
        elif task_type == "bug":
            pipeline = _SKILL_PIPELINES["bug"]
        else:
            pipeline = _SKILL_PIPELINES["greenfield"] if greenfield else _SKILL_PIPELINES["feature"]
    else:
        pipeline = [
            ("received", "manager"),
            ("analyzing", "architect"),
            ("fixing", "developer"),
            ("testing", "tester"),
            ("evaluating", "reviewer"),
            ("awaiting_release", "manager"),
        ]
    idx = next((i for i, (s, _) in enumerate(pipeline) if s == current_stage), None)
    if idx is not None and idx + 1 < len(pipeline):
        nxt_stage, nxt_agent = pipeline[idx + 1]
        return {"next_agent": nxt_agent, "next_stage": nxt_stage, "reason": "pipeline advance"}

    # awaiting_release 是流水线最后一段，之后由灰度结果事件驱动（release_decision），不自动推进
    return {"next_agent": None, "next_stage": "awaiting_release", "reason": "awaiting canary result; release decision required"}


class _StateStore:
    """TaskState 的轻量内存实现（状态机 + 闭环阈值闸门）。

    带版本号与阶段一致性校验；并记录 started_at 以支持超时判定。
    """

    def __init__(self) -> None:
        self._states: Dict[str, dict] = {}
        self._versions: Dict[str, int] = {}

    def transition(self, task_id, from_stage, to_stage, owner_agent, reason, extra=None) -> dict:
        if task_id not in self._states:
            if from_stage not in (None, "received"):
                return {"accepted": False, "reason": "unknown task; expected from_stage=received"}
        else:
            cur = self._states[task_id]["stage"]
            if from_stage and cur != from_stage:
                return {"accepted": False, "reason": f"stage mismatch: current={cur} expected={from_stage}"}
        state = {
            "stage": to_stage,
            "owner_agent": owner_agent,
            "reason": reason,
            "timestamp": _now_iso(),
        }
        if extra:
            state.update(extra)
        self._states[task_id] = state
        self._versions[task_id] = self._versions.get(task_id, 0) + 1
        return {"accepted": True, "state_version": self._versions[task_id], "state": state}

    def get(self, task_id):
        return self._states.get(task_id)


_STATE_STORE = _StateStore()


@register("state_manager", "Team Leader", "持久化 TaskState 迁移，并强制闭环阈值（轮次/文件数/Token/超时/回归次数）")
def skill_state_manager(payload: dict) -> dict:
    task_id = payload.get("task_id")
    if not task_id:
        return {"accepted": False, "reason": "task_id required"}

    # —— 闭环阈值闸门 ——
    r = payload.get("round")
    if r is not None and r > MAX_ROUND:
        return {"accepted": False, "decision": "escalated", "reason": f"round {r} exceeds max_round={MAX_ROUND}"}

    # 回归闭环闸门：regression_cycle_count 超限强制 escalated
    rc = payload.get("regression_cycle_count")
    if rc is not None and rc > REGRESSION_CYCLE_MAX:
        return {"accepted": False, "decision": "escalated", "reason": f"regression cycle {rc} exceeds max={REGRESSION_CYCLE_MAX}"}

    files = payload.get("modified_files_count")
    if files is not None and files > MAX_FILES:
        return {"accepted": False, "reason": f"modified files {files} exceeds budget {MAX_FILES}"}

    tokens = payload.get("tokens_used")
    compress = False
    if tokens is not None and tokens > TOKEN_BUDGET:
        compress = True  # 触发二次压缩，但仍接受迁移

    # 超时判定（基于首次进入时的 started_at）
    st = _STATE_STORE.get(task_id)
    extra = {}
    if st and st.get("started_at"):
        started = datetime.fromisoformat(st["started_at"])
        if (datetime.now(timezone.utc) - started).total_seconds() > TASK_TIMEOUT_MIN * 60:
            return {"accepted": False, "decision": "escalated", "reason": "task timeout"}
    if not st:
        extra["started_at"] = _now_iso()

    to_stage = payload.get("to_stage") or payload.get("stage")
    # awaiting_release 记录进入时间，供 canary_watchdog 判定 TTL
    if to_stage == "awaiting_release":
        extra["release_entered_at"] = _now_iso()
    # 回归次数回灌（由 release_decision 决策回滚时递增并携带）
    if rc is not None:
        extra["regression_cycle_count"] = rc

    result = _STATE_STORE.transition(
        task_id=task_id,
        from_stage=payload.get("from_stage"),
        to_stage=to_stage,
        owner_agent=payload.get("owner_agent") or payload.get("agent") or "manager",
        reason=payload.get("reason") or "",
        extra=extra,
    )
    result["compress"] = compress
    return result


@register("handoff_manager", "Team Leader", "生成人工移交包（达到阈值/高风险时）")
def skill_handoff_manager(payload: dict) -> dict:
    task_id = payload.get("task_id") or _rand("TASK")
    return {
        "handoff_id": _rand("HO"),
        "task_id": task_id,
        "status": "pending_human_review",
        "included": {
            "rounds": payload.get("rounds"),
            "last_context_pack": payload.get("last_context_pack"),
            "last_failure_reason": payload.get("last_failure_reason"),
            "generated_at": _now_iso(),
        },
    }


# --------------------------------------------------------------------------- #
# Release 灰度发布技能（release_plan_generator / release_decision / canary_watchdog）
# 对齐方案设计 v2.2 §4：Manager 生成 release_plan.json → awaiting_release →
# 事件唤醒读 confirmation_report.json → 关单 / 回滚 / escalated + 超时哨兵
# --------------------------------------------------------------------------- #


@register("release_plan_generator", "Team Leader", "生成灰度发布计划 release_plan.json（对齐方案设计 §4.2 意图声明）")
def skill_release_plan_generator(payload: dict) -> dict:
    """Evaluator 裁定通过后，Manager 生成 release_plan.json 作为灰度发布意图声明。

    本组合工具返回结构化 dict，由 Worker 持久化为 release_plan.json 写入 MinIO。
    关键字段（§4.2）：canary_scope / risk_level / rollback_point / promote_threshold。
    注意：PR 描述禁用 closes/fixes 关键字，避免 merge 即自动关单（§4.4）。
    """
    task_id = payload.get("task_id")
    if not task_id:
        return {"status": "error", "reason": "task_id required"}

    release_plan = {
        "task_id": task_id,
        "canary_scope": payload.get("canary_scope", "5% 流量 / region=default"),
        "risk_level": payload.get("risk_level", "L2"),
        "rollback_point": payload.get("rollback_point", f"git tag pre-fix-{task_id}"),
        "approver": payload.get("approver", "human"),
        "soak_window_min": int(payload.get("soak_window_min", 30)),
        "promote_threshold": payload.get("promote_threshold", {"error_rate_max": 0.01}),
        "pr_desc_note": "PR 描述禁用 closes/fixes 关键字，关单动作须由 Agent 显式执行",
        "created_at": _now_iso(),
        "status": "pending_approval",
    }
    return {
        "status": "ok",
        "release_plan": release_plan,
        "next_stage": "awaiting_release",
    }


@register("release_decision", "Team Leader", "读取 confirmation_report.json 决策关单/回滚（对齐方案设计 §4.4/§4.5）")
def skill_release_decision(payload: dict) -> dict:
    """灰度结果确认闭环：消费 confirmation_report.json 决策。

    canary OK   → resolved（调用 GitHub API 关单）
    canary FAIL → 回归闭环（regression_cycle_count++，未超限回滚到 analyzing，
                  超限 → escalated 人工介入）。回归不新建 Issue，复用同一 task_id。
    """
    task_id = payload.get("task_id")
    if not task_id:
        return {"status": "error", "reason": "task_id required"}

    confirmation = payload.get("confirmation_report") or payload.get("confirmation") or {}
    if isinstance(confirmation, dict):
        canary_result = str(confirmation.get("result") or confirmation.get("canary_result") or "").lower()
        canary_passed = confirmation.get("passed")
    else:
        canary_result = str(confirmation).lower()
        canary_passed = None

    if canary_passed is None:
        passed = canary_result in ("ok", "pass", "passed", "success", "succeeded", "true")
    else:
        passed = bool(canary_passed)

    regression_cycle = int(payload.get("regression_cycle_count", 0))

    if passed:
        return {
            "decision": "resolved",
            "action": "close_issue",
            "next_stage": "resolved",
            "reason": "canary OK; close issue",
        }

    # canary FAIL：回归闭环
    regression_cycle += 1
    if regression_cycle > REGRESSION_CYCLE_MAX:
        return {
            "decision": "escalated",
            "action": "escalate_to_human",
            "next_stage": "escalated",
            "regression_cycle_count": regression_cycle,
            "reason": f"canary FAIL and regression cycle {regression_cycle} exceeds max {REGRESSION_CYCLE_MAX}",
        }

    # canary FAIL：按 failure_class 路由回退目标
    failure_class = confirmation.get("failure_class") if isinstance(confirmation, dict) else None
    if failure_class in ("requirement",):
        target_stage, target_agent = "prd_drafting", "product-owner"
    elif failure_class in ("design",):
        target_stage, target_agent = "designing", "architect"
    elif failure_class in ("environment",):
        target_stage, target_agent = "ops_diagnosing", "ops-analyst"
    else:
        target_stage, target_agent = "analyzing", "architect"

    return {
        "decision": "rollback",
        "action": "feedback_to_target",
        "rollback_target": target_agent,
        "next_stage": target_stage,
        "regression_cycle_count": regression_cycle,
        "feedback": confirmation if isinstance(confirmation, dict) else {"result": confirmation},
        "reason": f"canary FAIL; rollback to {target_agent} (regression cycle {regression_cycle}/{REGRESSION_CYCLE_MAX})",
    }


@register("canary_watchdog", "Team Leader", "awaiting_release 超时哨兵（对齐方案设计 §4.6，默认 24h TTL）")
def skill_canary_watchdog(payload: dict) -> dict:
    """巡检 awaiting_release 是否超时（CI 静默失败 / webhook 丢失 / 人工忘 merge）。

    超时未收到 canary 结果 → 标 escalated 并通知人工（Matrix @admin）。
    """
    task_id = payload.get("task_id")
    if not task_id:
        return {"status": "error", "reason": "task_id required"}

    timeout_min = int(payload.get("canary_timeout_min", CANARY_TIMEOUT_MIN))
    st = _STATE_STORE.get(task_id)
    if not st:
        return {"status": "error", "reason": f"unknown task: {task_id}"}

    if st.get("stage") != "awaiting_release":
        return {"status": "not_waiting", "stage": st.get("stage"), "reason": "not in awaiting_release"}

    entered_at = st.get("release_entered_at") or st.get("timestamp")
    if not entered_at:
        return {"status": "waiting", "reason": "no release_entered_at; assume just entered"}

    entered = datetime.fromisoformat(entered_at)
    elapsed_min = (datetime.now(timezone.utc) - entered).total_seconds() / 60
    if elapsed_min > timeout_min:
        return {
            "status": "escalated",
            "action": "notify_human",
            "next_stage": "escalated",
            "reason": f"canary timeout: elapsed {elapsed_min:.0f}min > {timeout_min}min",
        }

    return {
        "status": "waiting",
        "elapsed_min": round(elapsed_min, 1),
        "timeout_min": timeout_min,
        "reason": "awaiting canary result",
    }


# --------------------------------------------------------------------------- #
# Retriever 检索技能（repo_indexer / context_packer）
# --------------------------------------------------------------------------- #


@register("repo_indexer", "Architect", "增量代码索引：tree-sitter 切分→嵌入→写入 pgvector/Neo4j/Meili（单命名空间 + 增量更新）")
def skill_repo_indexer(payload: dict) -> dict:
    """增量索引：单命名空间 per repo，Redis 追踪 file hash，只更新变更文件。

    流程：
    1. 从 Redis 获取当前索引状态（current_commit + file_hashes）
    2. 检查 pgvector / Neo4j / Meili 的实际数据状态
    3. 若 Redis 与数据库状态不一致，先 reset 当前 repo 命名空间
    4. 扫描新仓库，计算新 file_hashes
    5. Diff：removed / added / changed / unchanged
    6. 删除 removed + changed 文件的旧数据
    7. 只解析 added + changed 文件
    8. 嵌入（命中缓存则复用）+ 写入 DB
    9. 重建关系边
    10. 更新 Redis 状态
    """
    repo_path = payload.get("repo_path") or payload.get("repo")
    if not repo_path:
        return {"status": "error", "reason": "repo_path required"}
    commit = payload.get("commit", "")
    try:
        cache = RedisCache()
        repo_name = os.path.basename(os.path.abspath(repo_path))
        # 单命名空间 per repo（不再 per-commit），支持增量更新
        ns = repo_name
        pg = PgVectorStore()
        neo = Neo4jStore()
        meili = MeiliStore()
        ensure_all(ns=ns)

        # ---- Step 1: 获取当前索引状态 ----
        old_state = cache.get_repo_state(repo_name)
        old_file_hashes = old_state.get("file_hashes", {})
        old_commit = old_state.get("commit", "")

        # ---- Step 2: 检查三库实际状态 ----
        backend_state = {
            "pgvector": pg.has_namespace_data(ns=ns),
            "neo4j": neo.has_namespace_data(ns=ns),
            "meili": meili.has_namespace_data(ns=ns),
        }
        backend_has_any = any(backend_state.values())
        backend_ready = all(backend_state.values())

        if old_commit == commit and old_file_hashes and backend_ready:
            return {
                "status": "already_indexed",
                "repo": repo_name,
                "commit": commit[:8] or "HEAD",
                "ns": ns,
                "mode": "redis+db",
            }

        # ---- Step 3: Redis / 数据库状态不一致时，先 reset 当前 ns ----
        state_mismatch_reason = ""
        if old_file_hashes and not backend_ready:
            state_mismatch_reason = "redis_has_state_but_backend_missing_data"
        elif not old_file_hashes and backend_has_any:
            state_mismatch_reason = "backend_has_data_but_redis_missing_state"

        if state_mismatch_reason:
            logger.warning(
                "索引状态不一致 %s (%s): redis_commit=%s redis_files=%d backend_state=%s；"
                "这通常表示上一次索引中断后留下了部分后端数据，执行全量重建",
                repo_name,
                state_mismatch_reason,
                old_commit[:8] or "(none)",
                len(old_file_hashes),
                backend_state,
            )
            pg.delete_all(ns=ns)
            neo.delete_all(ns=ns)
            meili.delete_all(ns=ns)
            cache.clear_repo_state(repo_name)
            old_file_hashes = {}
            old_commit = ""

        # ---- Step 4: 扫描新仓库，计算 file hashes ----
        repo_path = os.path.abspath(repo_path)
        skip_dirs = {".git", "node_modules", "__pycache__", "venv", ".venv", "build", "dist"}
        ext_lang = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rb", ".rs", ".c", ".h", ".cpp", ".cc"}

        new_file_hashes = {}  # {rel_path: content_hash_of_full_file}
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in ext_lang:
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, repo_path)
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                    new_file_hashes[rel] = content_hash(data.decode("utf-8", "replace"))
                except Exception:
                    continue

        if not new_file_hashes:
            return {"status": "no_source", "repo": repo_name}

        # ---- Step 5: Diff ----
        old_paths = set(old_file_hashes.keys())
        new_paths = set(new_file_hashes.keys())
        removed_paths = old_paths - new_paths
        added_paths = new_paths - old_paths
        changed_paths = {p for p in (old_paths & new_paths) if old_file_hashes[p] != new_file_hashes[p]}
        unchanged_paths = (old_paths & new_paths) - changed_paths

        needs_update = bool(removed_paths or added_paths or changed_paths)
        if not needs_update and old_commit:
            # 文件完全相同，只更新 commit 标记
            cache.set_repo_state(repo_name, commit, new_file_hashes)
            cache.mark_indexed(repo_name, commit)
            return {"status": "already_indexed", "repo": repo_name, "commit": commit[:8] or "HEAD", "ns": ns,
                    "mode": "incremental", "unchanged": len(unchanged_paths)}

        logger.info(
            "增量索引 %s: removed=%d added=%d changed=%d unchanged=%d",
            repo_name, len(removed_paths), len(added_paths), len(changed_paths), len(unchanged_paths),
        )

        # ---- Step 6: 删除旧数据 ----
        delete_paths = list(removed_paths | changed_paths)
        if delete_paths:
            pg_deleted = pg.delete_by_paths(delete_paths, ns=ns)
            neo_deleted = neo.delete_by_paths(delete_paths, ns=ns)
            meili_deleted = meili.delete_by_paths(delete_paths, ns=ns)
            logger.info("删除旧数据: pg=%d neo=%d meili=%d", pg_deleted, neo_deleted, meili_deleted)

        # ---- Step 7: 只解析 added + changed 文件 ----
        parser = AstParser()
        new_chunks = []
        for rel in sorted(added_paths | changed_paths):
            full = os.path.join(repo_path, rel)
            try:
                chunks = parser.parse_file(full, repo=repo_name, display_path=rel)
                new_chunks.extend(chunks)
            except Exception:
                continue

        logger.info(
            "解析完成 %s: changed_files=%d new_chunks=%d",
            repo_name,
            len(added_paths | changed_paths),
            len(new_chunks),
        )

        if not new_chunks:
            # 只有删除，没有新增
            cache.set_repo_state(repo_name, commit, new_file_hashes)
            cache.mark_indexed(repo_name, commit)
            return {"status": "indexed", "repo": repo_name, "commit": commit[:8] or "HEAD", "ns": ns,
                    "mode": "incremental", "deleted_files": len(delete_paths), "new_chunks": 0}

        # ---- Step 8: 嵌入（命中缓存则复用）+ 写入 DB ----
        emb = EmbeddingService()
        vecs = [None] * len(new_chunks)
        cache_hits = 0
        uncached_indices = []
        uncached_texts = []
        chunk_hashes = [content_hash(c["content"]) for c in new_chunks]
        cached_embeddings = cache.get_embeddings(chunk_hashes)

        logger.info(
            "缓存探测完成 %s: lookup_keys=%d hits=%d misses=%d",
            repo_name,
            len(chunk_hashes),
            len(cached_embeddings),
            len(chunk_hashes) - len(cached_embeddings),
        )

        for idx, (c, ch) in enumerate(zip(new_chunks, chunk_hashes)):
            cached = cached_embeddings.get(ch)
            if cached is not None:
                vecs[idx] = cached
                cache_hits += 1
            else:
                uncached_indices.append(idx)
                uncached_texts.append(c["content"])

        logger.info(
            "开始嵌入 %s: total_chunks=%d cache_hits=%d uncached=%d",
            repo_name,
            len(new_chunks),
            cache_hits,
            len(uncached_indices),
        )

        if uncached_texts:
            uncached_vecs = emb.embed(uncached_texts)
            cache_updates = {}
            for idx, v in zip(uncached_indices, uncached_vecs):
                cache_updates[chunk_hashes[idx]] = v
                vecs[idx] = v
            cache.put_embeddings(cache_updates)

        logger.info(
            "嵌入完成 %s: total_chunks=%d cache_hits=%d embedded=%d",
            repo_name,
            len(new_chunks),
            cache_hits,
            len(uncached_indices),
        )

        for c in new_chunks:
            c["ns"] = ns
        # 批量写入（一次事务/请求，避免逐条 N+1 往返的开销）
        pg.batch_upsert_chunks(new_chunks, vecs, ns=ns)
        neo.batch_upsert_symbols(new_chunks, ns=ns)
        meili.batch_upsert(new_chunks, ns=ns)

        logger.info("写库完成 %s: pg+neo+meili upserts=%d", repo_name, len(new_chunks))

        # ---- Step 9: 重建关系边（只针对新 chunk）----
        edge_errors = 0
        call_rows, method_rows, import_rows = [], [], []
        seen_files = set()
        for c in new_chunks:
            if c.get("calls") and c.get("kind") in ("function", "method"):
                call_rows.append((c["chunk_id"], c["calls"], c.get("repo"), ns))
            if c.get("kind") == "method" and c.get("parent_class"):
                method_rows.append((c["chunk_id"], c["parent_class"], c["path"], c.get("repo"), ns))
            if c["path"] not in seen_files:
                seen_files.add(c["path"])
                import_rows.append((c["path"], c.get("file_imports") or [], ns))
        try:
            neo.batch_link_calls(call_rows)
        except Exception:  # noqa: BLE001
            edge_errors += len(call_rows)
        try:
            neo.batch_link_methods(method_rows)
        except Exception:  # noqa: BLE001
            edge_errors += len(method_rows)
        try:
            neo.batch_link_imports(import_rows)
        except Exception:  # noqa: BLE001
            edge_errors += len(import_rows)

        # ---- Step 10: 更新 Redis 状态 ----
        cache.set_repo_state(repo_name, commit, new_file_hashes)
        cache.mark_indexed(repo_name, commit)

        return {
            "status": "indexed",
            "repo": repo_name,
            "commit": commit[:8] or "HEAD",
            "ns": ns,
            "mode": "incremental",
            "old_commit": old_commit[:8] if old_commit else "(none)",
            "backend_state": backend_state,
            "state_mismatch": state_mismatch_reason or "",
            "removed": len(removed_paths),
            "added": len(added_paths),
            "changed": len(changed_paths),
            "unchanged": len(unchanged_paths),
            "new_chunks": len(new_chunks),
            "cache_hits": cache_hits,
            "edge_errors": edge_errors,
        }
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": str(e)}


@register("context_packer", "Architect", "将检索结果打包为上下文（拼接结构化块）")
def skill_context_packer(payload: dict) -> dict:
    chunks = payload.get("chunks", [])
    issue = payload.get("issue", {})
    lines = [f"# 缺陷上下文", f"标题: {issue.get('title', 'N/A')}", f"描述: {issue.get('description', 'N/A')}", ""]
    for i, c in enumerate(chunks):
        lines.append(f"## 候选片段 {i + 1} (score={c.get('score', '?')})")
        lines.append(f"来源: {c.get('path', '?')} :: {c.get('symbol', '')}")
        lines.append(c.get("content", ""))
        lines.append("")
    return {"context_pack": "\n".join(lines), "chunk_count": len(chunks)}


# --------------------------------------------------------------------------- #
# Reasoner 根因技能（root_cause_analyzer，图谱增强）
# --------------------------------------------------------------------------- #


@register("root_cause_analyzer", "Architect", "根因分析：结合 Neo4j 依赖图与代码上下文（降级为启发式）")
def skill_root_cause_analyzer(payload: dict) -> dict:
    ctx = payload.get("context_pack", "")
    suspect = payload.get("suspect_symbol") or ""
    ns = payload.get("ns", "")
    evidence: List[str] = []
    try:
        if suspect:
            neo = Neo4jStore()
            subs = neo.dependency_subgraph([suspect], depth=2, ns=ns)
            evidence.append(f"依赖子图调用方: {subs.get('direct_callers')}")
    except DbUnavailable:
        pass  # 无图库时忽略，降级为启发式

    rc = "（启发式）空指针/越界访问，源于上游未校验返回"
    if "None" in ctx or "null" in ctx.lower():
        rc = "（启发式）对可能为 null/None 的返回值缺少校验"
    return {
        "root_cause": rc,
        "evidence": evidence,
        "confidence": 0.6 if evidence else 0.4,
    }


# --------------------------------------------------------------------------- #
# Planner 规划技能（repair_planner / risk_gate）
# --------------------------------------------------------------------------- #


@register("repair_planner", "Developer", "生成修复计划：受 impact 影响面约束，单轮不超过 5 文件")
def skill_repair_planner(payload: dict) -> dict:
    root_cause = payload.get("root_cause", {})
    max_files = int(payload.get("max_files_per_round", MAX_FILES))
    impact = payload.get("impact")
    suspect = payload.get("suspect_files", []) or []
    if isinstance(impact, dict):
        cand = impact.get("impact_scope", {}).get("changed_files", []) or suspect
    else:
        cand = suspect
    cand = cand[:max_files]
    steps = [{"action": "guard_check", "target": f, "detail": "增加非空/边界校验"} for f in cand]
    return {
        "plan_id": _rand("PLAN"),
        "steps": steps,
        "files_budget": max_files,
        "rollback_plan": "git revert 改动并提交回滚评审",
        "based_on": root_cause,
    }


@register("risk_gate", "Developer", "风险闸门：默认拒绝原则，敏感模块/高危需人工审批")
def skill_risk_gate(payload: dict) -> dict:
    risk_level = (payload.get("risk_level") or "low").lower()
    touches = payload.get("touches", []) or []
    approval_required = bool(payload.get("approval_required", False))
    approved = bool(payload.get("approved", False))
    sensitive = {"auth", "payment", "db_schema", "security", "crypto"}
    touched_sensitive = [t for t in touches if t in sensitive]

    reasons: List[str] = []
    if approval_required and not approved:
        reasons.append("requires human approval but not approved")
    if touched_sensitive and risk_level in ("high", "critical"):
        reasons.append(f"sensitive modules touched with {risk_level} risk: {touched_sensitive}")

    return {
        "allowed": len(reasons) == 0,
        "reason": "; ".join(reasons) or "passed default-deny check",
        "touched_sensitive": touched_sensitive,
    }


# --------------------------------------------------------------------------- #
# Editor 编辑技能 — 已移除（Worker 容器内原生执行）
# patch_generator    → Worker 直接运行 git diff
# multi_file_editor  → Worker 直接编辑本地文件并校验
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Impact Analyst 影响面技能（dep_graph_analyzer / contract_checker）
# --------------------------------------------------------------------------- #

_SIGNATURE_RE = re.compile(r"^\s*(def |public |private |protected |function |=>|\w+\s*\([^)]*\)\s*\{?)\s*\w+")


@register("dep_graph_analyzer", "Reviewer", "依赖图影响分析：基于 Neo4j CALLS/IMPORTS 子图估算真实波及范围与风险等级")
def skill_dep_graph_analyzer(payload: dict) -> dict:
    changed_files = payload.get("changed_files", []) or []
    patch_text = payload.get("patch_text")
    ns = payload.get("ns", "")
    if patch_text:
        parsed = _parse_patch_text(patch_text)
        changed_files = [f for f, _, _ in parsed]
        changed_symbols = sum(len(added) + len(removed) for _, added, removed in parsed)
    else:
        changed_symbols = len(changed_files) * 3  # 启发式（无 diff 时）

    modules = {f.split("/")[0] for f in changed_files if "/" in f}
    cross_module = len(modules) > 1

    # 优先用真实 Neo4j 依赖图：被改文件定义符号的真实调用方数 + 跨文件 import 数。
    # 图库不可用 / 未索引时降级为启发式估算，保证技能始终可返回结果。
    direct_callers = None
    imported_files = None
    try:
        neo = Neo4jStore()
        stats = neo.impact_stats(changed_files, ns=ns)
        direct_callers = stats["direct_callers"]
        imported_files = stats["imported_files"]
    except Exception:  # noqa: BLE001 图库不可用/未索引时降级
        direct_callers = None

    if direct_callers is None:
        direct_callers = max(1, len(changed_files) * 2)
        imported_files = len(modules)
        note = "heuristic estimate; Neo4j 不可用，已降级"
    elif direct_callers == 0 and imported_files == 0:
        # 图库可用但无匹配（仓库可能未索引或路径不匹配）：启发式兜底避免空结果
        direct_callers = max(1, len(changed_files) * 2)
        imported_files = len(modules)
        note = "heuristic fallback; 依赖图未匹配到改动文件（可能未索引）"
    else:
        note = "real Neo4j dependency graph: CALLS caller count + cross-file IMPORTS"

    risk_level = "high" if (cross_module or direct_callers >= 10 or len(changed_files) >= 5) else "medium"

    return {
        "impact_scope": {
            "changed_files": changed_files,
            "direct_callers": direct_callers,
            "cross_module_edges": imported_files,
            "changed_symbols": changed_symbols,
        },
        "risk_level": risk_level,
        "need_extra_tests": True,
        "note": note,
    }


@register("contract_checker", "Reviewer", "契约核查：检测补丁是否改动既有接口/函数签名")
def skill_contract_checker(payload: dict) -> dict:
    patch_text = payload.get("patch_text")
    changed_files = payload.get("changed_files", []) or []
    violations: List[str] = []

    if patch_text:
        for f, added, removed in _parse_patch_text(patch_text):
            for line in added + removed:
                if _SIGNATURE_RE.match(line):
                    violations.append(f"{f}: signature line changed -> {line.strip()[:80]}")
    else:
        for f in changed_files:
            violations.append(f"{f}: changed without diff; assume potential contract impact")

    return {
        "contract_safe": len(violations) == 0,
        "violations": violations,
        "checked_files": changed_files,
    }


# --------------------------------------------------------------------------- #
# Verifier 验证技能（knowledge_extraction）
# result_judge — 已转为 prompt-only Skill，由 Tester/Evaluator Worker 在 soul 中引用
# test_runner — 已移除（Worker 容器内原生执行 pytest）
# --------------------------------------------------------------------------- #


def skill_result_judge(payload: dict) -> dict:
    """裁定结果逻辑（不再注册为 MCP 工具，转为 prompt-only Skill 的内置指令）。

    该函数保留供 Worker 本地调用或测试用，不再通过 @register 注册到 MCP Server。
    设计依据：方案设计 v2.2 §3.2.1——result-judge 为 prompt-only Skill，
    由 Worker runtime 直接提供执行能力，MCP 层不暴露。
    """
    test_result = payload.get("test_result", {}) or {}
    current_round = int(payload.get("current_round", 1))
    max_round = int(payload.get("max_round", MAX_ROUND))

    passed = bool(test_result.get("passed")) or (test_result.get("status") == "passed")
    failure_type = test_result.get("failure_type")

    if passed:
        return {"decision": "success", "failure_type": None, "round": current_round}
    if current_round >= max_round:
        return {"decision": "handoff", "failure_type": failure_type, "round": current_round, "reason": f"reached max_round={max_round}"}
    return {
        "decision": "retry",
        "failure_type": failure_type,
        "round": current_round,
        "reason": f"test failed, will retry (round {current_round} < {max_round})",
    }


@register("knowledge_extraction", "Reviewer", "知识沉淀：从根因/裁定中抽取经验并写入 lessons 表（带去重合并）")
def skill_knowledge_extraction(payload: dict) -> dict:
    """经验抽取技能——对齐方案设计 v2.2 §5.3，Evaluator 裁定完成后调用。

    从 payload 抽取结构化字段，向量化 root_cause 后调用
    LessonsStore.upsert_with_dedup 完成写入前去重（MERGE/SIMILAR/NEW）。

    注：本组合工具为 MCP 路径入口（字段由调用方通过 payload 提供）；
    完整字段抽取（从 fix_diff/test_report/verdict 自动提取）见
    AgentTeams Skill 脚本 knowledge-extraction/scripts/extract.py。
    """
    root_cause = payload.get("root_cause") or ""
    if not root_cause:
        return {"status": "error", "reason": "root_cause required"}

    decision = str(payload.get("final_decision") or payload.get("decision") or "").lower()
    success = decision == "pass"

    lesson = {
        "repo": payload.get("repo") or "",
        "task_id": payload.get("task_id") or "",
        "root_cause": root_cause,
        "fix_pattern": payload.get("fix_pattern"),
        "error_signature": payload.get("error_signature"),
        "fix_strategy": payload.get("fix_strategy"),
        "affected_modules": payload.get("affected_modules") or [],
        "tags": payload.get("tags") or [],
        "diff_summary": payload.get("diff_summary"),
        "test_changes": payload.get("test_changes"),
        "edge_cases": payload.get("edge_cases") or [],
        "success": success,
        "resolution_summary": payload.get("resolution_summary"),
        "retry_count": int(payload.get("retry_count") or 0),
        "merge_count": 1,
    }

    try:
        emb = EmbeddingService()
        vec = emb.embed([root_cause])[0]
        result = LessonsStore().upsert_with_dedup(lesson, vec)
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e), "lesson": lesson}

    return {
        "status": "ok",
        "decision": result["decision"],
        "lesson_id": result["lesson_id"],
        "knowledge_id": result["lesson_id"],
        "related_to": result["related_to"],
        "score": result["score"],
        "success": success,
    }


# 向后兼容别名（Worker 若通过旧名调用仍可找到入口）
skill_knowledge_miner = skill_knowledge_extraction


# --------------------------------------------------------------------------- #
# 内部工具（unidiff 解析 / 占位 diff 生成，供 Editor/Impact 复用）
# --------------------------------------------------------------------------- #


def _parse_patch_text(patch_text: str):
    try:
        from unidiff import PatchSet
    except ImportError:
        files = []
        cur_file = "<unknown>"
        added, removed = [], []
        for line in (patch_text or "").splitlines():
            if line.startswith("+++ b/"):
                if cur_file != "<unknown>":
                    files.append((cur_file, added, removed))
                cur_file = line[len("+++ b/") :].strip()
                added, removed = [], []
            elif line.startswith("+") and not line.startswith("+++"):
                added.append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                removed.append(line[1:])
        if cur_file != "<unknown>":
            files.append((cur_file, added, removed))
        return files
    ps = PatchSet.from_string(patch_text or "")
    return [(p.path, [l.value for h in p.hunks for l in h.target_lines()],
             [l.value for h in p.hunks for l in h.source_lines()]) for p in ps]


# --------------------------------------------------------------------------- #
# 已移除的技能（Worker 容器内原生执行，不再通过 MCP Server）：
#
# - read_code          → Worker 直接读取本地文件
# - run_bash           → Worker 直接在容器内执行命令
# - write_reproduction → Worker 直接写文件到本地工作区
# - patch_generator    → Worker 直接运行 git diff
# - test_runner        → Worker 直接运行 pytest
# - multi_file_editor  → Worker 直接编辑本地文件
#
# 这些技能需要文件系统访问，由 Worker 在其容器沙箱内原生处理，
# 避免了路径映射和权限问题。
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 新增技能：对齐补充设计文档 v1.2 四角色架构
# semantic_search / kg_query / module_lookup
# --------------------------------------------------------------------------- #


@register("semantic_search", "Architect", "语义检索：自然语言查询代码库，返回最相关的代码片段（三库融合工具接口）")
def skill_semantic_search(payload: dict) -> dict:
    """语义搜索技能——本质上是 hybrid_search 的别名，但使用设计文档标准命名。

    Use when: issue 提到概念但没提函数名时用 semantic-search。
    内部实现：向量召回 + 关键词召回 + RRF 融合 + Neo4j 图扩展。
    """
    query = payload.get("query", "")
    top_k = int(payload.get("top_k", 5))
    file_filter = payload.get("file_filter", "")
    ns = payload.get("ns", "")
    if not query:
        return {"status": "error", "reason": "query required"}
    # 复用 mcp_primitives.hybrid_search 共享服务（唯一实现，避免重复造轮子）
    result = hybrid_search(query=query, top_k=top_k, ns=ns)
    if file_filter and result.get("results"):
        result["results"] = [r for r in result["results"] if r.get("path", "").startswith(file_filter)]
    # 转换为设计文档标准输出格式
    results = []
    for r in result.get("results", []):
        results.append({
            "file": r.get("path", ""),
            "start_line": r.get("start_line", 1),
            "end_line": r.get("end_line", r.get("start_line", 1) + 10),
            "content": r.get("content", ""),
            "score": r.get("score", 0),
            "language": r.get("language", ""),
        })
    return {"results": results, "graph_expansion": result.get("graph_expansion", [])}


@register("kg_query", "Architect", "知识图谱查询：查询代码结构关系（调用方/被调方/模块伙伴/测试映射）")
def skill_kg_query(payload: dict) -> dict:
    """查询代码知识图谱的结构关系。

    Use when: 已定位到函数后，需要了解其影响面和依赖关系。
    """
    operation = payload.get("operation", "")
    function_name = payload.get("function_name", "")
    file_path = payload.get("file", "")
    depth = int(payload.get("depth", 1))
    ns = payload.get("ns", "")
    if not operation or not function_name:
        return {"status": "error", "reason": "operation and function_name required"}
    try:
        neo = Neo4jStore()
        if operation == "get_callers":
            sub = neo.dependency_subgraph([function_name], depth=depth, ns=ns)
            return {"nodes": [{"name": n, "relationship": "called_by"} for n in sub.get("direct_callers", [])], "edges": []}
        elif operation == "get_callees":
            sub = neo.dependency_subgraph([function_name], depth=depth, ns=ns)
            return {"nodes": [{"name": n, "relationship": "calls"} for n in sub.get("direct_callers", [])], "edges": []}
        elif operation == "get_module_peers":
            if file_path:
                stats = neo.impact_stats([file_path], ns=ns)
                return {"nodes": [{"name": file_path, "type": "file", "imported_files": stats.get("imported_files", 0)}], "edges": []}
            return {"nodes": [], "edges": []}
        elif operation == "find_tests_for":
            results = neo.symbol_lookup(f"test_{function_name}", ns=ns)
            return {"nodes": [{"name": r["chunk_id"], "type": "test"} for r in results], "edges": []}
        elif operation == "get_file_structure":
            if file_path:
                stats = neo.impact_stats([file_path], ns=ns)
                return {"nodes": [{"name": file_path, "type": "file", "direct_callers": stats.get("direct_callers", 0)}], "edges": []}
            return {"nodes": [], "edges": []}
        else:
            return {"status": "error", "reason": f"unknown operation: {operation}"}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": str(e)}


@register("module_lookup", "Architect", "模块查找：将领域概念映射到负责模块和关键文件")
def skill_module_lookup(payload: dict) -> dict:
    """将领域概念映射到负责模块和关键文件。

    Use when: issue 提到功能区域但没提具体代码位置时，作为第一步使用。
    """
    concept = payload.get("concept", "")
    if not concept:
        return {"status": "error", "reason": "concept required"}
    # 使用向量搜索找到与概念最相关的模块
    try:
        emb = EmbeddingService()
        qv = emb.embed([concept])[0]
        pg = PgVectorStore()
        results = pg.vector_search(qv, 5, ns=payload.get("ns", ""))
        if results:
            # 从搜索结果推断模块信息
            top = results[0]
            module = top.get("path", "").split("/")[0] if "/" in top.get("path", "") else "unknown"
            files = list({r.get("path", "") for r in results[:5]})
            return {
                "module": module,
                "files": files,
                "key_functions": [r.get("symbol", "") for r in results[:3]],
                "description": f"Module related to '{concept}'",
                "warnings": [],
            }
        return {"module": "unknown", "files": [], "key_functions": [], "description": "No matching module found", "warnings": []}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": str(e)}


# --------------------------------------------------------------------------- #
# Reviewer 确定性审查层（open-code-review delegate 模式封装）
#
# 接入定位（对齐详细设计 v3.1 §2.3）：
# - ocr 只承担「确定性一半」：文件选择（保证不漏审）+ 规则解析（按语言匹配审查清单）。
#   审查裁定权 100% 保留在 Reviewer 自身模型，因此绝不在 ocr 侧配置 LLM provider
#   —— 否则变成"第二个 Reviewer"：多一份 API 账单，且两边结论冲突时 verdict 权威性受损。
# - 仅在 evaluating 阶段暴露；双闸门（prd_review / design_review）审的是 PRD/ADD 文档，
#   diff 审查工具对其无用，故不列入 Reviewer 通用工具表，避免角色任务面膨胀。
# - 只读契约保持：Reviewer 不写业务仓库。补丁在宿主机 scratch 目录内重建
#   （clone 缓存 -> checkout base_commit -> git apply），Worker 仅通过 payload 收发。
# - 优雅降级：ocr 未安装 / 版本过低 / git 失败时返回 status=unavailable|error，
#   Reviewer 降级为纯 LLM 审查并显式声明 coverage 不可证，服务整体不崩。
# - 安全边界：MCP 端点对网络开放且本工具会读写宿主机文件系统，因此
#   repo_path 必须落在白名单根目录内，patch 大小与 diff 回传量均设上限。
# --------------------------------------------------------------------------- #

_MCP_PKG_DIR = os.path.dirname(os.path.abspath(__file__))

OCR_BIN = os.getenv("OCR_BIN", "ocr")
OCR_SCRATCH_ROOT = os.getenv("OCR_SCRATCH_DIR", "/tmp/dd-ocr")
# 仓库克隆缓存根：与 swe_bench_runner 的 REPO_CACHE_DIR 同源，避免两套路径
_OCR_REPO_CACHE = os.getenv("SWE_REPO_CACHE", "/tmp/swe-repos")
OCR_ALLOW_REPO_ROOTS = [p for p in re.split(r"[,;]", os.getenv("OCR_ALLOWED_REPO_ROOTS", "")) if p] or [_OCR_REPO_CACHE]
OCR_MAX_PATCH_BYTES = int(os.getenv("OCR_MAX_PATCH_BYTES", str(2 * 1024 * 1024)))
OCR_DIFF_BUDGET_BYTES = int(os.getenv("OCR_DIFF_BUDGET_BYTES", "120000"))
OCR_TIMEOUT_SEC = int(os.getenv("OCR_TIMEOUT_SEC", "300"))
# 团队规约注入（把工程约定从提示词升级为确定性检查）；设 OCR_RULE_PATH="" 可关闭
OCR_RULE_PATH = os.getenv("OCR_RULE_PATH", os.path.join(_MCP_PKG_DIR, "..", "deploy", "rules", "ocr-rule.json"))
# `ocr review/scan --format json` 自 1.9.0 起提供；delegate 子命令直接尝试 json，
# 不区分版本（失败时按 unknown flag 重试），此常量仅用于上报探测结论
OCR_JSON_MIN_VER = (1, 9, 0)


class _OcrScratchError(Exception):
    """scratch 工作树构建失败（克隆/检出/补丁应用）。"""


def _ocr_ver_tuple(text: str):
    """从 `ocr --version` 输出提取版本三元组；无法解析返回 None。"""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _ocr_probe() -> dict:
    """探测 ocr 可用性与 --format json 支持度（结果进程内缓存）。"""
    global _OCR_PROBE_CACHE
    if _OCR_PROBE_CACHE is not None:
        return _OCR_PROBE_CACHE
    info = {"installed": False, "version": "", "json_capable": False, "reason": ""}
    try:
        r = _ocr_run(["--version"], cwd=os.path.dirname(_MCP_PKG_DIR))
        out = (r["stdout"] or "") + (r["stderr"] or "")
        if r["rc"] == 0:
            tup = _ocr_ver_tuple(out)
            info.update({
                "installed": True,
                "version": ".".join(str(x) for x in tup) if tup else out.strip()[:40],
                "json_capable": bool(tup and tup >= OCR_JSON_MIN_VER),
            })
        else:
            info["reason"] = f"`{OCR_BIN} --version` 退出码 {r['rc']}: {out.strip()[:200]}"
    except Exception as e:  # noqa: BLE001
        info["reason"] = f"版本探测失败: {e}"
    _OCR_PROBE_CACHE = info
    return info


_OCR_PROBE_CACHE = None


def _ocr_unavailable(info: dict) -> dict:
    """统一降级载荷：明确告知 Reviewer 本次审查的完整性无法由工具证明。"""
    return {
        "status": "unavailable",
        "reason": info.get("reason") or "ocr CLI 不可用",
        "ocr_version": info.get("version", ""),
        "degradation": "Reviewer 降级为纯 LLM 审查：coverage_rate 不可证，"
                       "必须在 verdict 中声明未使用确定性文件清单",
        "recovery": "重跑 make install（deploy/scripts/lib/common.sh: ensure_ocr），或手工安装 "
                    "open-code-review 并确保 `ocr --version` 可执行",
    }


def _ocr_run(args: List[str], cwd: str) -> dict:
    """执行 ocr 子命令（所有 ocr 调用的唯一出口）；永不抛异常。

    返回 {rc, stdout, stderr}；rc=124 超时，rc=125 未找到可执行文件或启动失败。
    """
    try:
        r = subprocess.run([OCR_BIN] + list(args), cwd=cwd, capture_output=True, text=True,
                           timeout=OCR_TIMEOUT_SEC)
        return {"rc": r.returncode, "stdout": r.stdout or "", "stderr": r.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "stdout": "", "stderr": f"ocr 超时（>{OCR_TIMEOUT_SEC}s）"}
    except FileNotFoundError:
        return {"rc": 125, "stdout": "", "stderr": f"未找到 `{OCR_BIN}` 可执行文件"
                "（make install 会自动安装；或设 OCR_BIN 指定路径）"}
    except Exception as e:  # noqa: BLE001
        return {"rc": 125, "stdout": "", "stderr": str(e)}


def _ocr_check_repo_path(repo_path: str):
    """白名单校验：repo_path 必须位于克隆缓存根内，且自身是 git 仓库。"""
    if not repo_path:
        return None, "repo_path required（宿主机上的 git 仓库路径）"
    real = os.path.realpath(repo_path)
    if not any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r).rstrip("/") + os.sep)
               for r in OCR_ALLOW_REPO_ROOTS):
        return None, (f"repo_path 不在允许的克隆缓存根内: {real}; "
                      f"允许根: {OCR_ALLOW_REPO_ROOTS}（可用 OCR_ALLOWED_REPO_ROOTS 扩展）")
    if not os.path.isdir(os.path.join(real, ".git")) and not os.path.isfile(os.path.join(real, "HEAD")):
        return None, f"repo_path 不是 git 仓库（缺 .git）: {real}"
    return real, None


def _ocr_safe_rel(p: str):
    """拒绝绝对路径与越界跳转——paths 参数来自 Worker（不可信输入）。"""
    if not p or os.path.isabs(p) or ".." in p.replace("\\", "/").split("/"):
        return None, f"非法路径: {p!r}"
    return p.replace("\\", "/"), None


def _ocr_rule_file(payload: dict):
    """校验并返回团队规约文件绝对路径；不可注入时返回 ("", 原因)。

    ocr 对 `--rule` 指向的坏文件是硬失败（loadRuleFile 直接返回 error），
    所以先自行解析校验：宁可跳过规约注入，也不能让整次审查报错。
    绝对路径在 ocr 侧直用（relative 才会拼到 repo root 下）。
    """
    if not payload.get("use_team_rules", True):
        return "", "payload 显式关闭了团队规约注入"
    path = payload.get("rule_file") or OCR_RULE_PATH
    if not path:
        return "", "未配置 OCR_RULE_PATH"
    real = os.path.abspath(path)
    if not os.path.isfile(real):
        return "", f"团队规约文件不存在: {real}"
    try:
        import json as _json
        with open(real, "r", encoding="utf-8") as f:
            spec = _json.load(f)
        entries = spec.get("rules")
        if not isinstance(entries, list) or not entries:
            return "", f"团队规约缺少非空 rules 数组: {real}"
        bad = [i for i, e in enumerate(entries)
               if not isinstance(e, dict) or not isinstance(e.get("path"), str)
               or not isinstance(e.get("rule"), str)]
        if bad:
            return "", f"团队规约第 {bad} 条缺 path/rule 字段: {real}"
        return real, ""
    except Exception as e:  # noqa: BLE001
        return "", f"团队规约不可解析，已跳过注入: {real}: {e}"


def _ocr_prepare_scratch(task_id: str, repo_path: str, base_commit: str, patch_text: str) -> str:
    """构建/复用 scratch 工作树：clone 缓存库 -> checkout base_commit -> git apply patch。

    以 (task_id, repo, commit, patch 内容) 的哈希做幂等键，同一任务重复调用不重建。
    """
    key = hashlib.sha1(
        "|".join([task_id or "-", repo_path, base_commit, patch_text]).encode("utf-8")
    ).hexdigest()[:16]
    entry = os.path.join(OCR_SCRATCH_ROOT, key)
    # 工作树单独一层：就绪标记若落在工作树内会被 git 当成未跟踪文件计入审查范围
    work = os.path.join(entry, "work")
    marker = os.path.join(entry, ".ready")
    if os.path.isfile(marker):
        try:
            with open(marker, "r", encoding="utf-8") as f:
                if f.read().strip() == key:
                    return work
        except OSError:
            pass

    os.makedirs(entry, exist_ok=True)
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)

    r = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", repo_path, work],
                       capture_output=True, text=True, timeout=OCR_TIMEOUT_SEC)
    if r.returncode != 0:
        raise _OcrScratchError(f"克隆审查缓存失败: {(r.stderr or r.stdout).strip()[:300]}")

    if base_commit:
        r = subprocess.run(["git", "checkout", "--quiet", "--detach", base_commit],
                           cwd=work, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise _OcrScratchError(
                f"checkout base_commit 失败（{base_commit[:12]}）: {(r.stderr or r.stdout).strip()[:300]}")

    patch_file = os.path.join(work, ".dd-review.patch")
    with open(patch_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(patch_text)
    # 与 swe_bench_runner 的补丁应用策略一致：先常规应用，失败再 --recount 重试
    last = None
    for extra in ([], ["--recount"]):
        last = subprocess.run(["git", "apply"] + extra + [".dd-review.patch"],
                              cwd=work, capture_output=True, text=True, timeout=120)
        if last.returncode == 0:
            break
    os.remove(patch_file)
    if last is None or last.returncode != 0:
        raise _OcrScratchError(
            "补丁无法应用到 base_commit（基线错位或补丁损坏）: "
            f"{(last.stderr if last else '').strip()[:400]}")

    with open(marker, "w", encoding="utf-8") as f:
        f.write(key)
    return work


def _ocr_file_diff(work: str, path: str, status: str) -> str:
    """取单文件变更内容：tracked 走 git diff HEAD，新增未跟踪文件直读全文。"""
    if (status or "").startswith("?") or "untracked" in (status or "").lower():
        try:
            with open(os.path.join(work, path), "r", encoding="utf-8", errors="replace") as f:
                return f"[new untracked file, full content]\n{f.read()[:20000]}"
        except OSError:
            return ""
    r = subprocess.run(["git", "diff", "HEAD", "--", path], cwd=work,
                       capture_output=True, text=True, timeout=120)
    return r.stdout or ""


def _ocr_pick(entry: dict, *keys):
    for k in keys:
        if entry.get(k) not in (None, ""):
            return entry.get(k)
    return None


def _ocr_extract_lists(data: dict):
    """解析 delegate preview 的 JSON 载荷。

    字段名对齐 open-code-review 源码 cmd/opencodereview/delegate_cmd.go 的
    delegatePreviewJSON / delegatePreviewFileJSON：
      {schema_version, mode, repository, from/to/commit/merge_base, background,
       total_files, reviewable_count, excluded_count, total_insertions, total_deletions,
       reviewable_files[{path,status,insertions,deletions}],
       excluded_files[+exclude_reason]}
    候选名兜底只为兼容旧版本；顶层 key 对不上时返回 matched=False，
    由调用方转 git 兜底并显式标注来源，绝不臆造结构。
    """
    raw_files = (_ocr_pick(data, "reviewable_files", "reviewable", "files") or [])
    files = []
    for e in raw_files:
        if isinstance(e, str):
            e = {"path": e}
        p = _ocr_pick(e, "path", "file", "name")
        if not p:
            continue
        files.append({
            "path": p,
            "status": _ocr_pick(e, "status", "state") or "",
            "insertions": _ocr_pick(e, "insertions", "added") or 0,
            "deletions": _ocr_pick(e, "deletions", "deleted") or 0,
        })
    raw_excl = (_ocr_pick(data, "excluded_files", "excluded", "skipped") or [])
    excluded = []
    for e in raw_excl:
        if isinstance(e, str):
            e = {"path": e}
        p = _ocr_pick(e, "path", "file", "name")
        if p:
            excluded.append({"path": p, "reason": _ocr_pick(e, "exclude_reason", "reason", "why") or ""})
    matched = "reviewable_files" in data or "files" in data or bool(raw_files)
    meta = {k: data.get(k) for k in
            ("schema_version", "mode", "merge_base", "total_files", "reviewable_count",
             "excluded_count", "total_insertions", "total_deletions")}
    return files, excluded, matched, meta


@register("ocr_delegate_preview", "Reviewer",
          "确定性审查范围界定：在宿主机 scratch 重建 base+patch 后由 ocr 选出必审文件（附各文件 diff 与覆盖率契约）")
def skill_ocr_delegate_preview(payload: dict) -> dict:
    """delegate 模式 Step 1：只让 ocr 做文件选择与排除判定，不做任何 LLM 调用。

    入参：patch_text(必填) / repo_path(必填，须在白名单根内) / base_commit(必填)
          task_id / exclude / background / background_file(宿主机文件) / rule_file /
          use_team_rules / with_diff / paths(按需取 diff)
    出参：reviewable_files(含 diff) / excluded_files / total_files / reviewable_count /
          coverage_contract（均按 ocr delegate preview 真实 schema 得出）
    """
    probe = _ocr_probe()
    if not probe["installed"]:
        return _ocr_unavailable(probe)

    patch_text = payload.get("patch_text") or payload.get("patch") or ""
    if not patch_text.strip():
        return {"status": "error", "reason": "patch_text required（Developer 的 fix.diff 全文）"}
    if len(patch_text.encode("utf-8")) > OCR_MAX_PATCH_BYTES:
        return {"status": "error",
                "reason": f"patch 超过上限 {OCR_MAX_PATCH_BYTES} 字节；请分批送审或提高 OCR_MAX_PATCH_BYTES"}

    repo_path, err = _ocr_check_repo_path(payload.get("repo_path") or "")
    if err:
        return {"status": "error", "reason": err}
    base_commit = payload.get("base_commit") or ""
    if not base_commit:
        return {"status": "error", "reason": "base_commit required（用于把补丁重建到正确基线上）"}

    try:
        work = _ocr_prepare_scratch(payload.get("task_id") or "", repo_path, base_commit, patch_text)
    except _OcrScratchError as e:
        # 补丁应用不上本身就是高价值审查信号（基线错位 / 过度修改）
        return {"status": "error", "reason": str(e),
                "hint": "该失败应作为 failure_class=code 的证据记入 verdict"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "reason": f"scratch 构建异常: {e}"}

    args = ["delegate", "preview", "--repo", work, "--format", "json"]
    if payload.get("exclude"):
        args += ["--exclude", str(payload["exclude"])]
    rule_file, rule_note = _ocr_rule_file(payload)
    if rule_file:
        args += ["--rule", rule_file]
    bg_file = payload.get("background_file")
    if bg_file:
        if not os.path.isfile(str(bg_file)):
            return {"status": "error",
                    "reason": f"background_file 必须是宿主机上存在的文件: {bg_file}"
                              "（Worker 容器内路径对本工具不可用，请改用 background 传文本）"}
        args += ["--background-file", str(bg_file)]
    elif payload.get("background"):
        args += ["--background", str(payload["background"])[:8000]]

    res = _ocr_run(args, cwd=work)
    used_json = True
    # 官方降级路径：低版本报 `unknown flag: --format` 时去掉该 flag 重跑一次（仅此一种错误重试）
    if res["rc"] != 0 and "--format" in res["stderr"] + res["stdout"] and "flag" in res["stderr"] + res["stdout"]:
        res = _ocr_run([a for a in args if a not in ("--format", "json")], cwd=work)
        used_json = False
    if res["rc"] != 0:
        return {"status": "error", "reason": f"ocr delegate preview 退出码 {res['rc']}",
                "stderr": res["stderr"].strip()[:800], "scratch_dir": work}

    files, excluded, parsed, meta = [], [], False, {}
    text_mode = ""
    if used_json:
        try:
            import json as _json
            data = _json.loads(res["stdout"][res["stdout"].index("{"):])
            files, excluded, parsed, meta = _ocr_extract_lists(data)
        except Exception:  # noqa: BLE001 解析失败时不臆造结构，转 git 兜底
            parsed = False
    if not parsed:
        # text 模式或 key 不匹配：文件清单改由 git 兜底得出（来源已标注，不冒充 ocr 输出）
        text_mode = "git-fallback"
        r = subprocess.run(["git", "diff", "--name-status", "HEAD"], cwd=work,
                           capture_output=True, text=True, timeout=120)
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                files.append({"path": parts[-1], "status": parts[0], "insertions": 0, "deletions": 0})
        r = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=work,
                           capture_output=True, text=True, timeout=120)
        for line in (r.stdout or "").splitlines():
            if line.strip():
                files.append({"path": line.strip(), "status": "untracked", "insertions": 0, "deletions": 0})

    # diff 回传：按预算附带，超限的文件标记 diff_omitted，由 Reviewer 用 paths 参数分批取
    want = payload.get("paths")
    rejected = []
    if want:
        cleaned, bad = [], []
        for p in want:
            rel, e = _ocr_safe_rel(str(p))
            (cleaned if rel else bad).append(rel or p)
        targets = [f for f in files if f["path"] in cleaned]
        rejected = bad
    else:
        targets = files if payload.get("with_diff", True) else []
    budget = OCR_DIFF_BUDGET_BYTES
    for f in files:
        f["diff"] = ""
        f["diff_omitted"] = False
    for f in targets:
        if not want and budget <= 0:
            f["diff_omitted"] = True
            continue
        d = _ocr_file_diff(work, f["path"], f.get("status", ""))
        if len(d) > budget and not want:
            f["diff_omitted"] = True
            continue
        f["diff"] = d
        budget -= len(d)

    must_review = len(files)
    ocr_total = meta.get("total_files")
    out = {
        "status": "ok",
        "ocr_version": probe["version"],
        "json_capable": probe["json_capable"],
        "delegate_format": "json" if used_json else "text",
        "file_list_source": "ocr" if parsed else text_mode,
        "scratch_dir": work,
        "total_files": (ocr_total if isinstance(ocr_total, int) and ocr_total
                        else must_review + len(excluded)),
        "reviewable_count": must_review,
        "excluded_count": len(excluded),
        "reviewable_files": files,
        "excluded_files": excluded,
        "coverage_contract": {
            "rule": "分母 = reviewable_count。每个 reviewable_files 条目最终必须是 reviewed"
                    "或有具体理由的 skipped；不得静默省略文件。excluded_files 是 ocr 的"
                    "确定性排除（已附 reason），不需逐个审查，但需在 verdict 中原样引用。",
            "required_verdict_fields": ["total_files", "reviewed_files", "skipped_files", "coverage_rate"],
            "hard_gate": "coverage_rate = len(reviewed_files) / reviewable_count；"
                         "小于 1.0（存在未交代文件）时本次审查无效，Team Leader 应要求重跑",
        },
        "team_rules_injected": bool(rule_file),
        "note": "ocr 在本链路中只做文件选择与规则解析（无 LLM 调用）；"
                "审查结论与 failure_class 判定由 Reviewer 自身模型负责。",
    }
    if rule_note:
        out["team_rules_note"] = rule_note
    if rejected:
        out["rejected_paths"] = rejected
        out["rejected_reason"] = "绝对路径或越界路径已拒绝"
    if parsed:
        out["ocr_mode"] = meta.get("mode") or ""
        if meta.get("merge_base"):
            out["merge_base"] = meta["merge_base"]
        for k in ("total_insertions", "total_deletions"):
            if meta.get(k) is not None:
                out[k] = meta[k]
        if str(meta.get("schema_version") or "") not in ("", "1"):
            out["schema_note"] = (f"delegate JSON schema_version={meta.get('schema_version')} "
                                  f"与已知契约（1）不同，字段解析可能不完整")
    else:
        out["parse_note"] = "未能按 ocr JSON 结构解析，文件清单已由 git 兜底；原始输出见 raw_output"
        out["raw_output"] = res["stdout"][:4000]
    if any(f.get("diff_omitted") for f in files):
        out["diff_budget_note"] = (f"部分文件 diff 因预算（OCR_DIFF_BUDGET_BYTES="
                                  f"{OCR_DIFF_BUDGET_BYTES}）未附带，请用 paths 参数分批取")
    return out


@register("ocr_delegate_rule", "Reviewer",
          "确定性审查清单：按文件解析 ocr 规则集并按规则内容分组（同规则文件归并，避免重复）")
def skill_ocr_delegate_rule(payload: dict) -> dict:
    """delegate 模式 Step 2：取回每个文件的审查规则清单（ocr 侧零 LLM 调用）。

    入参：scratch_dir(必填，来自 ocr_delegate_preview) / paths(必填，仓内相对路径列表)
          rule_file / use_team_rules（规约注入，默认用 OCR_RULE_PATH）
    出参：按官方 delegateRulesJSON 回传 {groups:[{group_id,source,pattern,files,rule}]}
          source 为 custom|project|global|system；merge_system_rule 生效时
          rule 已含合并后的文本。
    """
    probe = _ocr_probe()
    if not probe["installed"]:
        return _ocr_unavailable(probe)

    work = payload.get("scratch_dir") or ""
    real_scratch = os.path.realpath(OCR_SCRATCH_ROOT)
    real_work = os.path.realpath(work) if work else ""
    if not work or not real_work.startswith(real_scratch.rstrip("/") + os.sep) or not os.path.isdir(real_work):
        return {"status": "error",
                "reason": f"scratch_dir 必须位于审查 scratch 根目录内（先调 ocr_delegate_preview 获取）: {OCR_SCRATCH_ROOT}"}

    paths = payload.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    safe, bad = [], []
    for p in paths:
        rel, e = _ocr_safe_rel(str(p))
        (safe if rel else bad).append(rel or p)
    if not safe:
        return {"status": "error", "reason": "paths required（来自 preview 的 reviewable 文件路径列表）"}

    args = ["delegate", "rule", "--repo", real_work, "--format", "json"]
    rule_file, rule_note = _ocr_rule_file(payload)
    if rule_file:
        args += ["--rule", rule_file]
    args += safe

    res = _ocr_run(args, cwd=real_work)
    used_json = True
    if res["rc"] != 0 and "--format" in res["stderr"] + res["stdout"] and "flag" in res["stderr"] + res["stdout"]:
        res = _ocr_run([a for a in args if a not in ("--format", "json")], cwd=real_work)
        used_json = False
    if res["rc"] != 0:
        return {"status": "error", "reason": f"ocr delegate rule 退出码 {res['rc']}",
                "stderr": res["stderr"].strip()[:800]}

    out = {"status": "ok", "ocr_version": probe["version"],
           "delegate_format": "json" if used_json else "text",
           "team_rules_injected": bool(rule_file),
           "requested_paths": safe}
    if bad:
        out["rejected_paths"] = bad
        out["rejected_reason"] = "绝对路径或越界路径已拒绝"
    if rule_note:
        out["team_rules_note"] = rule_note
    try:
        import json as _json
        out["rule_groups"] = _json.loads(res["stdout"][res["stdout"].index("{"):])
    except Exception:  # noqa: BLE001
        out["rule_groups_raw"] = res["stdout"][:8000]
        out["parse_note"] = "规则输出非 JSON，按原文回传；请直接把该清单作为审查 checklist 使用"
    out["note"] = "规则清单是审查 checklist，不是结论；是否成立由 Reviewer 判断。"
    return out
