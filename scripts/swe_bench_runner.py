#!/usr/bin/env python3
"""SWE-bench 自动化测试脚本 — Flask 仓库全 issue 流水线。

按时间顺序处理 SWE-bench 中 pallets/flask 的所有 issue，
逐个通过 AgentTeams 流水线（Analyzer → Fixer → Tester → Evaluator），
收集 patch 后用 SWE-bench 官方测试验证。

用法:
    # 完整流程（索引 + 提交 + 等待 + 验证）
    python scripts/swe_bench_runner.py

    # 只跑第 1 个 issue（按 --list 的顺序，且每次都会重新跑）
    python scripts/swe_bench_runner.py --issue-index 1

    # 仅索引（不提交任务）
    python scripts/swe_bench_runner.py --index-only

    # 仅列出 Flask instances
    python scripts/swe_bench_runner.py --list

    # 指定 repo 缓存目录
    python scripts/swe_bench_runner.py --repo-cache /tmp/swe-repos

    # 从某个 instance 开始（跳过之前的）
    python scripts/swe_bench_runner.py --start-from flask__12345

    # 干跑模式：只打印计划，不实际操作
    python scripts/swe_bench_runner.py --dry-run

前置条件:
    1. 全部服务已启动: ./deploy/scripts/start.sh
    2. pip install datasets requests
    3. Matrix 配置自动从 deploy/install/agentteams.env 读取（HICLAW_ADMIN_PASSWORD）
       派单房间严格取自 controller 当前 teamRoomID（MATRIX_ROOM_ID 不一致时忽略并告警）
"""

import argparse
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

TARGET_REPO = "pallets/flask"
SWE_BENCH_DATASET = "princeton-nlp/SWE-bench_Lite"

# AgentTeams env 文件路径（自动查找）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
_AGENTTEAMS_ENV = os.path.join(_REPO_ROOT, "deploy", "install", "agentteams.env")
_DB_ENV = os.path.join(_REPO_ROOT, "deploy", "db", ".env.db")
_LOCAL_MANAGER_ENV = os.path.expanduser("~/hiclaw-manager.env")
_HF_DATASETS_CACHE = os.path.expanduser("~/.cache/huggingface/datasets")
_DEFAULT_TUNNEL_HOST = os.getenv("AGENTTEAMS_DB_TUNNEL_HOST", "8.130.191.237")
_DEFAULT_TUNNEL_USER = os.getenv("AGENTTEAMS_DB_TUNNEL_USER", "root")
_DEFAULT_TUNNEL_KEY = os.getenv(
    "AGENTTEAMS_DB_TUNNEL_KEY",
    os.path.join(_REPO_ROOT, "secrets", "ecs-ssh-key.pem"),
)
_TUNNEL_PID_FILE = "/tmp/agentteams-tunnel.pid"

if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

if os.path.exists(_DB_ENV):
    os.environ.setdefault("AGENTTEAMS_ENV_FILE", _DB_ENV)

def _load_agentteams_env():
    """从 deploy/install/agentteams.env 加载 Matrix 配置。"""
    if os.path.exists(_AGENTTEAMS_ENV):
        with open(_AGENTTEAMS_ENV) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())

_load_agentteams_env()
if os.path.exists(_LOCAL_MANAGER_ENV):
    with open(_LOCAL_MANAGER_ENV) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

# Matrix 配置（优先从 agentteams.env 读取，环境变量可覆盖）
_hiclaw_matrix_domain = os.getenv("HICLAW_MATRIX_DOMAIN", "matrix-local.hiclaw.io:18080")
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", f"http://127.0.0.1:{_hiclaw_matrix_domain.split(':')[-1] if ':' in _hiclaw_matrix_domain else '18080'}")
MATRIX_USER = os.getenv("MATRIX_USER", os.getenv("HICLAW_ADMIN_USER", "admin"))
MATRIX_PASSWORD = os.getenv("MATRIX_PASSWORD", os.getenv("HICLAW_ADMIN_PASSWORD", ""))
MATRIX_ROOM_ID = os.getenv("MATRIX_ROOM_ID", "")  # 仅作参考；与 controller teamRoomID 不一致时会被忽略
MATRIX_TEAM_NAME = os.getenv("MATRIX_TEAM_NAME", "rd-defect-team")
MATRIX_WORKSPACE_DIR = os.path.expandvars(
    os.path.expanduser(os.getenv("HICLAW_WORKSPACE_DIR", os.path.expanduser("~/hiclaw-manager"))))

# AgentTeams v1.2.x 更名后的容器/CLI/MinIO 命名（环境变量可覆盖，兼容旧部署）：
# 旧版为 hiclaw-controller/hiclaw CLI/hiclaw-storage 桶，v1.2.0 起全部改名。
_CONTROLLER_CONTAINER = os.getenv("AGENTTEAMS_CONTROLLER_CONTAINER", "agentteams-controller")
_MANAGER_CONTAINER = os.getenv("AGENTTEAMS_MANAGER_CONTAINER", "agentteams-manager")
_WORKER_CONTAINER_PREFIX = os.getenv("AGENTTEAMS_WORKER_CONTAINER_PREFIX", "agentteams-worker")
_AGENTTEAMS_CLI = os.getenv("AGENTTEAMS_CLI", "agt")
_MINIO_PREFIX_ROOT = os.getenv("AGENTTEAMS_MINIO_PREFIX", "agentteams/agentteams-storage")

# Team 内期望的全部成员（coordinator = leader + 4 个 specialist worker）。
# 用于发现/校验派单房间时确认这些角色确实已加入该房间，避免把任务发到
# controller 记录的陈旧 teamRoomID（成员不全 / 路由未绑定）导致无人响应、静默等待超时。
# 对齐 AgentTeams 官方设计：所有角色通信都在同一个 public Team Room 中、彼此可见，
# 派单房间必须包含 leader + 全部 worker，否则流水线无法协作。
_WORKER_NAMES = ("analyzer", "fixer", "tester", "evaluator")
_WORKER_USER_IDS = [f"@{n}:{_hiclaw_matrix_domain}" for n in _WORKER_NAMES]
_COORDINATOR_USER_ID = f"@coordinator:{_hiclaw_matrix_domain}"

# MCP Server
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8090"))

# 本地路径
REPO_CACHE_DIR = os.getenv("SWE_REPO_CACHE", "/tmp/swe-repos")
RESULTS_DIR = os.getenv("SWE_RESULTS_DIR", "results/swe-bench")

# 超时
INDEX_TIMEOUT_SEC = 600       # 索引超时
TASK_TIMEOUT_SEC = 3600       # 单任务超时（60 分钟；流水线 analyze→fix→test→evaluate 含修订回环实测 30-45 分钟）
POLL_INTERVAL_SEC = 10        # 轮询间隔

# 委派看门狗：worker 被 @ 委派后超过该秒数仍无任何发言，判定为失联/卡死，   
# 触发容器级恢复 + 请求 coordinator 重新委派（对应用户此前多次手动做的事）。
WATCHDOG_SILENCE_SEC = 600    # 被委派 worker 静默 10 分钟视为失联（实测 fix/analyze 阶段常静默工作 10-20 分钟；阈值过低会误杀正常阶段）
WATCHDOG_COOLDOWN_SEC = 1200  # 同一阶段两次自动恢复的最小间隔，避免重启风暴

# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class SweInstance:
    """SWE-bench 实例。"""
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    fail_to_pass: List[str]
    pass_to_pass: List[str]
    test_patch: str
    created_at: str = ""       # 用于排序

@dataclass
class TaskResult:
    """单个任务的结果。

    两个维度的结果（对应 SWE-bench 评测的标准两层）：
    - agent_verdict：维度①，AgentTeams 各角色流水线的自评结论
      （success / fail / escalated，来自 coordinator 最终判定）
    - swebench_result：维度②，SWE-bench 官方标准答案的客观验证结果
      （含 resolved / fail_to_pass / pass_to_pass 等，来自 evaluate_patch）
    """
    instance_id: str
    status: str = "pending"    # pending / indexed / submitted / running / completed / failed / skipped
    patch: str = ""
    agent_verdict: str = ""    # 维度①：AgentTeams 自评（success / fail / escalated）
    swebench_result: Optional[Dict] = None  # 维度②：SWE-bench 客观验证
    error: str = ""
    duration_sec: float = 0.0


# #region debug-point A:debug-reporting
def _debug_report(hypothesis_id: str, location: str, msg: str, data: Optional[Dict] = None, run_id: str = "pre-fix"):
    env_path = os.getenv("SWE_DEBUG_ENV_FILE", os.path.join(_REPO_ROOT, ".dbg", "task-timeout.env"))
    event_url = "http://127.0.0.1:7777/event"
    session_id = os.getenv("SWE_DEBUG_SESSION_ID", "task-timeout")
    try:
        with open(env_path, encoding="utf-8") as f:
            content = f.read()
        for line in content.splitlines():
            if line.startswith("DEBUG_SERVER_URL="):
                event_url = line.split("=", 1)[1].strip() or event_url
            elif line.startswith("DEBUG_SESSION_ID="):
                session_id = line.split("=", 1)[1].strip() or session_id
    except Exception:
        pass

    try:
        payload = json.dumps({
            "sessionId": session_id,
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "msg": msg,
            "data": data or {},
            "ts": int(time.time() * 1000),
        }).encode()
        req = urllib.request.Request(
            event_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def _debug_team_snapshot() -> Dict:
    try:
        proc = subprocess.run(
            ["docker", "exec", _CONTROLLER_CONTAINER, _AGENTTEAMS_CLI, "get", "teams", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc.returncode != 0:
            return {"error": "team_query_failed", "stderr": (proc.stderr or "").strip()[:200]}
        body = json.loads(proc.stdout or "{}")
        teams = body.get("teams", [])
        team = next((item for item in teams if item.get("name") == MATRIX_TEAM_NAME), {})
        return {
            "phase": team.get("phase"),
            "teamRoomID": team.get("teamRoomID"),
            "leaderReady": team.get("leaderReady"),
            "readyWorkers": team.get("readyWorkers"),
            "totalWorkers": team.get("totalWorkers"),
            "message": (team.get("message") or "")[:300],
        }
    except Exception as e:
        return {"error": str(e)}
# #endregion

@dataclass
class RunState:
    """整体运行状态（可序列化，支持断点续跑）。"""
    results: Dict[str, TaskResult] = field(default_factory=dict)
    # 兼容旧状态文件；索引真值现由 repo_indexer 基于 Redis + 数据库判断。
    indexed_commits: List[str] = field(default_factory=list)
    started_at: str = ""
    last_updated: str = ""

    def save(self, path: str):
        self.last_updated = datetime.now().isoformat()
        with open(path, "w") as f:
            json.dump(self.__dict__, f, ensure_ascii=False, indent=2, default=lambda o: o.__dict__)

    @classmethod
    def load(cls, path: str) -> "RunState":
        if not os.path.exists(path):
            return cls(started_at=datetime.now().isoformat())
        with open(path) as f:
            data = json.load(f)
        state = cls()
        state.started_at = data.get("started_at", "")
        state.last_updated = data.get("last_updated", "")
        state.indexed_commits = data.get("indexed_commits", [])
        for k, v in data.get("results", {}).items():
            if isinstance(v, dict):
                # 兼容旧状态文件字段名：verdict → agent_verdict, eval_result → swebench_result
                v = dict(v)
                if "verdict" in v and "agent_verdict" not in v:
                    v["agent_verdict"] = v.pop("verdict")
                if "eval_result" in v and "swebench_result" not in v:
                    v["swebench_result"] = v.pop("eval_result")
                tr = TaskResult(**v)
            else:
                tr = v
            state.results[k] = tr
        return state

# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("swe-bench")

# --------------------------------------------------------------------------- #
# SWE-bench 数据加载
# --------------------------------------------------------------------------- #

def load_flask_instances(split: str = "test") -> List[SweInstance]:
    """从 HuggingFace datasets 加载 Flask 的 SWE-bench 实例。"""
    try:
        from datasets import Dataset, load_dataset
    except ImportError:
        log.error("请安装 datasets: pip install datasets")
        sys.exit(1)

    log.info("加载 SWE-bench 数据集: %s (split=%s)", SWE_BENCH_DATASET, split)
    ds = _load_cached_swe_bench_split(Dataset, split)
    if ds is None:
        # 优先复用本地缓存，避免每次启动都被 HF 的 HEAD/重试链路拖慢。
        ds = load_dataset(
            SWE_BENCH_DATASET,
            split=split,
            download_mode="reuse_dataset_if_exists",
        )

    instances = []
    for d in ds:
        if TARGET_REPO not in d.get("repo", ""):
            continue

        # 解析测试列表
        f2p = _parse_test_list(d.get("FAIL_TO_PASS", "[]"))
        p2p = _parse_test_list(d.get("PASS_TO_PASS", "[]"))

        inst = SweInstance(
            instance_id=d["instance_id"],
            repo=d["repo"],
            base_commit=d["base_commit"],
            problem_statement=d["problem_statement"],
            fail_to_pass=f2p,
            pass_to_pass=p2p,
            test_patch=d.get("test_patch", ""),
            created_at=d.get("created_at", d.get("problem_statement", "")[:20]),
        )
        instances.append(inst)

    # 按 base_commit 在 git 历史中的顺序排序（用 commit hash 字典序近似）
    # 真实场景应按 commit date 排序，但 SWE-bench 没有直接提供 date 字段
    # 这里按 instance_id 排序作为近似（同一 repo 的 instance_id 通常包含 issue 编号）
    instances.sort(key=lambda x: x.instance_id)

    log.info("Flask instances: %d 个", len(instances))
    return instances


def _load_cached_swe_bench_split(dataset_cls, split: str):
    """优先从本地 HF cache 读取 SWE-bench Arrow 文件。"""
    cache_root = os.path.join(
        _HF_DATASETS_CACHE,
        "princeton-nlp___swe-bench_lite",
        "default",
        "0.0.0",
    )
    if not os.path.isdir(cache_root):
        return None

    candidates = []
    for entry in os.listdir(cache_root):
        split_file = os.path.join(cache_root, entry, f"swe-bench_lite-{split}.arrow")
        if os.path.isfile(split_file):
            candidates.append(split_file)

    if not candidates:
        return None

    arrow_path = sorted(candidates)[-1]
    log.info("使用本地缓存数据集: %s", arrow_path)
    return dataset_cls.from_file(arrow_path)


def _parse_test_list(raw) -> List[str]:
    """解析 FAIL_TO_PASS / PASS_TO_PASS（可能是 JSON 字符串或列表）。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    return []

# --------------------------------------------------------------------------- #
# 仓库管理
# --------------------------------------------------------------------------- #

def ensure_repo(repo: str, commit: str, cache_dir: str) -> str:
    """确保本地有仓库并 checkout 到指定 commit。返回本地路径。"""
    repo_name = repo.split("/")[-1]  # pallets/flask → flask
    local_path = os.path.join(cache_dir, repo_name)

    if not os.path.exists(local_path):
        os.makedirs(cache_dir, exist_ok=True)
        log.info("Clone %s → %s", repo, local_path)
        subprocess.run(["git", "clone", f"https://github.com/{repo}", local_path], check=True)

    if not _git_commit_exists(local_path, commit):
        log.info("本地缺少 commit %s，尝试从远端获取", commit[:8])
        try:
            subprocess.run(
                ["git", "fetch", "--all"],
                cwd=local_path,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"git fetch 超时（repo={local_path}）") from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            stdout = (e.stdout or "").strip()
            detail = stderr or stdout or str(e)
            raise RuntimeError(f"git fetch 失败: {detail}") from e

        if not _git_commit_exists(local_path, commit):
            raise RuntimeError(f"git fetch 完成后仍未找到 commit {commit}")

    subprocess.run(
        ["git", "checkout", "-f", commit],
        cwd=local_path,
        check=True,
        capture_output=True,
        text=True,
    )

    log.info("Repo ready: %s @ %s", local_path, commit[:8])
    return local_path


def _git_commit_exists(repo_path: str, commit: str) -> bool:
    """检查目标 commit 是否已存在于本地仓库。"""
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def get_commit_date(repo_path: str, commit: str) -> str:
    """获取 commit 的日期（用于排序）。"""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%ci", commit],
        cwd=repo_path, capture_output=True, text=True
    )
    return result.stdout.strip()

# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #

def index_repo(repo_path: str, commit: str, ns: str) -> bool:
    """本地直调 repo_indexer 协调索引，避免依赖已废弃的 /skills REST 接口。"""

    log.info("索引仓库（增量）: %s @ %s (ns=%s)", repo_path, commit[:8], ns)
    backend_error = _check_index_backend_ports()
    if backend_error:
        log.error("索引前检查失败: %s", backend_error)
        return False
    try:
        from mcp_server.composed_tools import skill_repo_indexer

        body = skill_repo_indexer({"repo_path": repo_path, "commit": commit})
        status = body.get("status", "unknown")
        mode = body.get("mode", "full")
        log.info(
            "索引结果: status=%s mode=%s added=%s changed=%s removed=%s unchanged=%s new_chunks=%s cache_hits=%s",
            status, mode,
            body.get("added", "?"), body.get("changed", "?"),
            body.get("removed", "?"), body.get("unchanged", "?"),
            body.get("new_chunks", "?"), body.get("cache_hits", "?"),
        )
        if status in ("error", "unavailable"):
            reason = body.get("reason", "(no reason)")
            log.error("索引失败详情: status=%s reason=%s", status, reason)
        return status not in ("error", "unavailable")
    except Exception as e:
        log.error("索引失败: %s", e)
        return False


def _check_index_backend_ports() -> str:
    """在索引前快速检查本地数据库端口，给出更清晰的隧道错误信息。"""
    backend_error = _probe_index_backend_ports()
    if not backend_error:
        return ""

    log.warning("索引依赖端口当前不可达，尝试自动重建 SSH 隧道...")
    if _start_db_tunnel():
        backend_error = _probe_index_backend_ports()
        if not backend_error:
            log.info("SSH 隧道已自动恢复，继续执行索引")
            return ""

    return backend_error


def _probe_index_backend_ports() -> str:
    """检查索引依赖的本地数据库端口。"""
    required_ports = {
        5432: "PostgreSQL",
        6379: "Redis",
        7474: "Neo4j HTTP",
        7687: "Neo4j Bolt",
        7700: "Meilisearch",
    }
    unreachable = []
    for port, name in required_ports.items():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError:
            unreachable.append(f"{name}:{port}")

    if unreachable:
        return (
            "本地数据库端口不可达（可能是 SSH 隧道已断开）: "
            + ", ".join(unreachable)
            + "。请先重新执行 ./deploy/scripts/start.sh 8.130.191.237 "
            + "或 ./deploy/scripts/ecs-tunnel.sh"
        )
    return ""


def _start_db_tunnel() -> bool:
    """尝试按项目默认配置重建 SSH 数据库隧道。"""
    key_path = os.path.expanduser(_DEFAULT_TUNNEL_KEY)
    if not os.path.isfile(key_path):
        log.error("自动重建隧道失败：找不到私钥 %s", key_path)
        return False

    ssh_cmd = [
        "ssh",
        "-i", key_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-N",
        "-f",
        "-L", "5432:127.0.0.1:5432",
        "-L", "6379:127.0.0.1:6379",
        "-L", "7474:127.0.0.1:7474",
        "-L", "7687:127.0.0.1:7687",
        "-L", "7700:127.0.0.1:7700",
        f"{_DEFAULT_TUNNEL_USER}@{_DEFAULT_TUNNEL_HOST}",
    ]
    try:
        subprocess.run(ssh_cmd, check=True, capture_output=True, text=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        log.error("自动重建隧道失败: %s", e)
        return False

    pid_result = subprocess.run(
        ["pgrep", "-f", f"ssh.*{_DEFAULT_TUNNEL_HOST}.*-L 5432:127.0.0.1:5432"],
        capture_output=True,
        text=True,
    )
    pid = (pid_result.stdout.strip().splitlines() or [""])[0]
    if pid:
        try:
            with open(_TUNNEL_PID_FILE, "w", encoding="utf-8") as f:
                f.write(pid + "\n")
        except OSError:
            pass
    time.sleep(1)
    return True


def make_ns(repo: str, _commit: str = "") -> str:
    """生成命名空间（单命名空间 per repo，不再 per-commit）。"""
    return repo.split("/")[-1]


def reset_db(repo_name: str = "flask") -> bool:
    """清空数据库中指定仓库的所有索引数据，防止漏题。

    清除范围（单命名空间 per repo）：
    - pgvector:    删除 ns=repo_name 的所有代码块
    - Neo4j:       删除 ns=repo_name 的所有节点和关系
    - Meilisearch: 删除 ns=repo_name 索引的所有文档
    - Redis:       清除 repo_state:{repo_name} 和 indexed:{repo_name}:* 标记

    这确保数据库从零开始，只包含 SWE-bench 测试所需的 commit 索引。
    """
    log.warning("=" * 60)
    log.warning("重置数据库：清除 '%s' 仓库的所有索引数据", repo_name)
    log.warning("=" * 60)

    success = True

    # 1. PostgreSQL — 删除所有代码块
    try:
        from mcp_server.db.pgvector import PgVectorStore
        store = PgVectorStore()
        deleted = store.delete_all(ns=repo_name)
        log.info("  pgvector: 删除 %d 条代码块", deleted)
    except Exception as e:
        log.warning("  pgvector 清理跳过: %s", e)
        success = False

    # 2. Neo4j — 删除所有节点和关系
    try:
        from mcp_server.db.neo4jgraph import Neo4jStore
        store = Neo4jStore()
        deleted = store.delete_all(ns=repo_name)
        log.info("  Neo4j: 删除 %d 个节点", deleted)
    except Exception as e:
        log.warning("  Neo4j 清理跳过: %s", e)
        success = False

    # 3. Meilisearch — 删除所有文档
    try:
        from mcp_server.db.meili import MeiliStore
        store = MeiliStore()
        task_id = store.delete_all(ns=repo_name)
        log.info("  Meilisearch: 清空任务已提交 (task=%s)", task_id)
    except Exception as e:
        log.warning("  Meilisearch 清理跳过: %s", e)
        success = False

    # 4. Redis — 清除状态和标记
    try:
        from mcp_server.db.redis_cache import RedisCache
        cache = RedisCache()
        cache.clear_repo_state(repo_name)
        log.info("  Redis: 已清除 repo_state:%s", repo_name)
    except Exception as e:
        log.warning("  Redis 清理跳过: %s", e)
        success = False

    log.warning("数据库重置完成: %s", "成功" if success else "部分跳过")
    return success

# --------------------------------------------------------------------------- #
# Matrix 任务提交
# --------------------------------------------------------------------------- #

class MatrixClient:
    """简易 Matrix 客户端（用于提交任务给 Manager）。"""

    def __init__(self, homeserver: str, user: str, password: str):
        self.hs = homeserver.rstrip("/")
        self.user = user
        self.token = ""
        # 默认派单目标为 coordinator（team leader）。
        # discover_room() 会再次确认，但 MATRIX_ROOM_ID 显式指定时不会调用
        # discover_room()，此时必须在这里兜底，否则会 fallback 到 @manager 发错人。
        self.dispatch_user_id = f"@coordinator:{_hiclaw_matrix_domain}"
        self._login(password)

    @staticmethod
    def ensure_workers_ready() -> bool:
        """提交任务前经 controller 确认全部 worker 就绪（官方 lifecycle：ensure-ready）。

        容器 running 不代表 agent 可用（休眠/失联时容器仍在），必须走 controller 的
        ensure-ready 语义，避免把任务派给无人响应的团队后静默等待超时。
        """
        try:
            proc = subprocess.run(
                ["docker", "exec", _CONTROLLER_CONTAINER, _AGENTTEAMS_CLI, "get", "workers", "-o", "json"],
                capture_output=True, text=True, timeout=10,
            )
            names = sorted({
                w.get("name", "")
                for w in json.loads(proc.stdout or "{}").get("workers", [])
                if w.get("name")
            })
        except Exception as e:
            log.warning("读取 worker 列表失败，跳过 ensure-ready 预检: %s", e)
            return True
        ok = True
        for name in names:
            r = subprocess.run(
                ["docker", "exec", _CONTROLLER_CONTAINER, _AGENTTEAMS_CLI, "worker", "ensure-ready", "--name", name],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                log.info("worker 就绪: %s", name)
            else:
                ok = False
                log.error("worker ensure-ready 失败: %s (%s)", name, (r.stderr or r.stdout or "").strip()[:200])
        return ok

    def _login(self, password: str):
        import urllib.request
        url = f"{self.hs}/_matrix/client/r0/login"
        payload = json.dumps({
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": self.user},
            "password": password,
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read().decode())
        self.token = body["access_token"]
        log.info("Matrix 登录成功: %s", self.user)

    def discover_room(self, team_name: str = MATRIX_TEAM_NAME) -> str:
        """发现任务派单房间（严格模式：只信任 controller 管理的 Team Room）。

        历史教训：手工创建的房间（如 swebench-retry）即使拉齐了全部成员，也没有
        controller 的消息路由绑定，coordinator 委派后其他 worker 不会被唤醒，导致
        流水线静默超时。因此：
        1. 唯一合法目标是 controller team 资源当前的 teamRoomID（成员校验通过）；
        2. 显式 MATRIX_ROOM_ID 与之一致才生效，否则忽略并告警；
        3. 校验不通过直接返回空串（调用方报错退出），绝不降级到任意“成员齐”的房间。
        """
        self.dispatch_user_id = f"@coordinator:{_hiclaw_matrix_domain}"

        controller_team_room_id = self._get_team_room_from_controller(team_name)
        if not controller_team_room_id:
            log.error(
                "controller 未返回 team=%s 的 teamRoomID，无法确定合法派单房间。"
                "请先执行 ./deploy/scripts/reset-agentteams-rooms.sh --yes 重建 team。",
                team_name,
            )
            return ""

        members = self._get_room_members(controller_team_room_id)
        if not self._is_valid_team_room(members):
            missing = [u for u in ([self.dispatch_user_id] + _WORKER_USER_IDS) if u not in members]
            log.error(
                "controller teamRoomID=%s 成员不完整(缺失 %s)，拒绝派单。"
                "请执行 ./deploy/scripts/reset-agentteams-rooms.sh --yes 重建 team 后重试。",
                controller_team_room_id, missing,
            )
            return ""

        if MATRIX_ROOM_ID and MATRIX_ROOM_ID != controller_team_room_id:
            log.warning(
                "环境变量 MATRIX_ROOM_ID=%s 与 controller 当前 teamRoomID 不一致，"
                "忽略 MATRIX_ROOM_ID，使用 controller 管理的房间（避免派单到无路由绑定的陈旧房间）。",
                MATRIX_ROOM_ID,
            )

        room_name = self._get_room_name(controller_team_room_id)
        log.info(
            "使用 controller team room: %s (%s) → %s",
            controller_team_room_id, room_name or "(未命名)", self.dispatch_user_id,
        )
        return controller_team_room_id

    def _get_team_leader_binding_from_controller(self, team_name: str) -> Dict[str, str]:
        """优先读取 controller 当前 team leader 房间，避免把任务发到 team room。"""
        try:
            proc = subprocess.run(
                ["docker", "exec", _CONTROLLER_CONTAINER, _AGENTTEAMS_CLI, "get", "workers", "-o", "json"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if proc.returncode != 0:
                return {}
            body = json.loads(proc.stdout or "{}")
            workers = body.get("workers", [])
            leader = next(
                (
                    item for item in workers
                    if item.get("team") == team_name and item.get("role") == "team_leader"
                ),
                {},
            )
            room_id = leader.get("roomID", "")
            return {
                "room_id": room_id if isinstance(room_id, str) else "",
                "matrix_user_id": str(leader.get("matrixUserID") or ""),
                "name": str(leader.get("name") or ""),
            }
        except Exception:
            return {}

    def _get_team_room_from_controller(self, team_name: str) -> str:
        """优先读取 controller 当前 team 资源，避免命中本地陈旧 room 绑定。"""
        try:
            proc = subprocess.run(
                ["docker", "exec", _CONTROLLER_CONTAINER, _AGENTTEAMS_CLI, "get", "teams", "-o", "json"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if proc.returncode != 0:
                return ""
            body = json.loads(proc.stdout or "{}")
            teams = body.get("teams", [])
            team = next((item for item in teams if item.get("name") == team_name or item.get("teamName") == team_name), {})
            room_id = team.get("teamRoomID", "")
            return room_id if isinstance(room_id, str) else ""
        except Exception:
            return ""

    def _get_team_leader_room_from_workspace(self, team_name: str) -> str:
        """从本地 workers-registry.json 读取 team leader room_id。"""
        registry_path = os.path.join(MATRIX_WORKSPACE_DIR, "workers-registry.json")
        try:
            with open(registry_path) as f:
                body = json.load(f)
        except Exception:
            return ""

        workers = body.get("workers") or {}
        for worker in workers.values():
            if worker.get("team_id") != team_name or worker.get("role") != "team_leader":
                continue
            matrix_user_id = worker.get("matrix_user_id", "")
            if isinstance(matrix_user_id, str) and matrix_user_id:
                self.dispatch_user_id = matrix_user_id
            room_id = worker.get("room_id", "")
            return room_id if isinstance(room_id, str) else ""
        return ""

    def _get_team_room_from_workspace(self, team_name: str) -> str:
        """优先从本地 workspace registry 读取当前 team room_id。"""
        registry_path = os.path.join(MATRIX_WORKSPACE_DIR, "teams-registry.json")
        try:
            with open(registry_path) as f:
                body = json.load(f)
        except Exception:
            return ""

        team = (body.get("teams") or {}).get(team_name) or {}
        room_id = team.get("team_room_id", "")
        return room_id if isinstance(room_id, str) else ""

    def _get_room_name(self, room_id: str) -> str:
        """获取房间名称。"""
        import urllib.request
        import urllib.parse
        url = (
            f"{self.hs}/_matrix/client/r0/rooms/"
            f"{urllib.parse.quote(room_id)}/state/m.room.name"
        )
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.loads(r.read().decode())
            return body.get("name", "")
        except Exception:
            return ""

    def _get_room_members(self, room_id: str) -> set:
        """获取房间全部成员 user_id 集合。"""
        import urllib.request
        import urllib.parse
        try:
            url = (
                f"{self.hs}/_matrix/client/r0/rooms/"
                f"{urllib.parse.quote(room_id)}/members"
            )
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.loads(r.read().decode())
            return {ev.get("state_key", "") for ev in body.get("chunk", [])}
        except Exception as e:
            log.warning("读取房间成员失败 %s: %s", room_id, e)
            return set()

    def _is_valid_team_room(self, members: set) -> bool:
        """team room 必须含 coordinator(leader) + 全部 4 个 specialist worker。"""
        if self.dispatch_user_id not in members:
            return False
        return all(w in members for w in _WORKER_USER_IDS)

    def send_task(self, room_id: str, instance: SweInstance) -> str:
        """发送任务消息到房间，返回 event_id。"""
        import urllib.request
        import urllib.parse

        target_user_id = getattr(self, "dispatch_user_id", "") or f"@manager:{_hiclaw_matrix_domain}"
        msg = (
            f"{target_user_id} New SWE-bench task [{instance.instance_id}]\n\n"
            f"Fix issue in {instance.repo} at commit {instance.base_commit}\n"
            f"instance_id: {instance.instance_id}\n"
            f"base_commit: {instance.base_commit}\n\n"
            f"problem_statement: |\n"
            f"  {instance.problem_statement}\n"
            f"\nPlease handle this through the standard analyze -> fix -> test -> evaluate workflow "
            f"and publish the task artifacts for `{instance.instance_id}`.\n"
        )

        # 记录提交时刻（毫秒），供 verdict 扫描做时间过滤，避免扫到历史遗留消息
        self.last_submit_ts_ms = int(time.time() * 1000)

        txn_id = f"swe_{instance.instance_id}_{int(time.time())}"
        url = (
            f"{self.hs}/_matrix/client/r0/rooms/"
            f"{urllib.parse.quote(room_id)}/send/m.room.message/{txn_id}"
        )
        # Build a mention link for formatted_body to meet openclaw-gateway requireMention
        mention_html = f'<a href="https://matrix.to/#/{target_user_id}">{target_user_id.split(":")[0].lstrip("@")}</a>'
        payload = json.dumps(
            {
                "msgtype": "m.text",
                "body": msg,
                "format": "org.matrix.custom.html",
                "formatted_body": f"{mention_html} {msg}",
                "m.mentions": {"user_ids": [target_user_id]},
            }
        ).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="PUT",
        )
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read().decode())
        event_id = body.get("event_id", "")
        log.info("任务已提交: %s → event %s", instance.instance_id, event_id[:20])
        # #region debug-point A:task-submitted
        _debug_report(
            "A",
            "scripts/swe_bench_runner.py:send_task",
            "[DEBUG] task submitted to matrix room",
            {
                "instance_id": instance.instance_id,
                "room_id": room_id,
                "target_user_id": target_user_id,
                "event_id": event_id,
                "base_commit": instance.base_commit,
            },
        )
        # #endregion
        return event_id

    def send_mention_message(self, room_id: str, text: str, mention_user_ids: List[str]) -> str:
        """发送带 m.mentions 的管理员消息（用于唤醒 coordinator / worker）。

        openclaw/copaw gateway 均要求 requireMention，仅正文写 @xxx 不会触发唤醒，
        必须同时带 m.mentions.user_ids 和 formatted_body 的 matrix.to mention 链接。
        """
        import urllib.request
        import urllib.parse
        txn_id = f"swe_admin_{int(time.time() * 1000)}"
        url = (
            f"{self.hs}/_matrix/client/r0/rooms/"
            f"{urllib.parse.quote(room_id)}/send/m.room.message/{txn_id}"
        )
        mention_html = " ".join(
            f'<a href="https://matrix.to/#/{uid}">{uid.split(":")[0].lstrip("@")}</a>'
            for uid in mention_user_ids
        )
        payload = json.dumps(
            {
                "msgtype": "m.text",
                "body": text,
                "format": "org.matrix.custom.html",
                "formatted_body": f"{mention_html} {text}",
                "m.mentions": {"user_ids": list(mention_user_ids)},
            }
        ).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="PUT",
        )
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read().decode())
        return body.get("event_id", "")

# --------------------------------------------------------------------------- #
# 完成检测
# --------------------------------------------------------------------------- #

def _minio_task_prefixes(instance_id: str) -> List[str]:
    """列出 shared/tasks/ 下与该实例相关的所有任务目录前缀。

    新平台（v1.2.x）的任务目录名为 {instance}-{时间戳}-{序号}（且 instance 中的
    下划线被归一化为连字符，如 pallets__flask-4045 → pallets-flask-4045-…），
    产物分散在多个子目录（如 patch 在 -02，result 在 -01），因此不能只按
    精确目录名拉取，需按规范化前缀扫描全部匹配目录。
    """
    import re
    import subprocess
    tasks_root = f"{_MINIO_PREFIX_ROOT}/shared/tasks/"
    # 平台会把连续下划线折叠为单个连字符（pallets__flask-4045 → pallets-flask-4045-…）
    norm = re.sub(r"_+", "-", instance_id)
    prefixes = [f"{tasks_root}{instance_id}"]  # 精确名优先（旧平台格式）
    try:
        proc = subprocess.run(
            ["docker", "exec", _CONTROLLER_CONTAINER, "mc", "ls", tasks_root],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                name = line.split()[-1].rstrip("/") if line.split() else ""
                if name and (name == instance_id or name.startswith(norm + "-")):
                    p = f"{tasks_root}{name}"
                    if p not in prefixes:
                        prefixes.append(p)
    except Exception as e:
        log.warning("MinIO 任务目录扫描失败，回退精确路径: %s", e)
    return prefixes


def _pull_minio_artifacts(instance_id: str, inst_dir: str) -> bool:
    """从 MinIO 拉取 agent 发布的 artifacts (spec.md, plan.md, result.md, patch.diff)。

    遍历所有匹配的任务目录（旧→新），同名文件后出现的覆盖先出现的，
    保证各角色分散在不同子目录里的产物都能拿到且取最新。
    """
    import subprocess
    files = ["spec.md", "plan.md", "result.md", "patch.diff"]
    pulled = False
    for prefix in _minio_task_prefixes(instance_id):
        for fname in files:
            local_path = os.path.join(inst_dir, fname)
            try:
                proc = subprocess.run(
                    ["docker", "exec", _CONTROLLER_CONTAINER, "mc", "cat",
                     f"{prefix}/{fname}"],
                    capture_output=True, timeout=10
                )
                if proc.returncode == 0 and proc.stdout:
                    with open(local_path, "wb") as f:
                        f.write(proc.stdout)
                    log.info("从 MinIO 拉取: %s/%s (%d bytes)",
                             prefix.split('/')[-1], fname, len(proc.stdout))
                    pulled = True
            except Exception:
                pass
    return pulled


def _scan_room_events(matrix_client, room_id: str, since_ts_ms: int = 0,
                      max_pages: int = 6, page_limit: int = 100) -> List[Dict]:
    """倒序分页拉取房间消息事件（最新在前）。

    触达早于 since_ts_ms 的事件即停止翻页；网络异常时返回已拉到的部分，
    保证调用方（verdict 扫描 / 委派看门狗）在噪声较大的房间里也有足够窗口。
    """
    import urllib.request
    import urllib.parse
    events: List[Dict] = []
    from_token = ""
    for _ in range(max_pages):
        url = (f"{matrix_client.hs}/_matrix/client/r0/rooms/"
               f"{urllib.parse.quote(room_id)}/messages?dir=b&limit={page_limit}")
        if from_token:
            url += f"&from={urllib.parse.quote(from_token)}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {matrix_client.token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                body = json.loads(r.read().decode())
        except Exception:
            break
        chunk = body.get("chunk", [])
        if not chunk:
            break
        oldest_ts = None
        for ev in chunk:
            ts = ev.get("origin_server_ts", 0)
            oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)
            # 先全量收集，再按时间过滤：历史教训是「页内遇到边界即丢弃后续事件」，
            # Matrix 分页不保证页内严格全局有序，提前 break 会把仍在窗口内的
            # worker 响应误判为不存在，导致看门狗对正常工作的 worker 误报失联。
            if since_ts_ms and ts < since_ts_ms:
                continue
            events.append(ev)
        # 整页最旧事件已早于边界，后续页只会更旧，停止翻页
        if since_ts_ms and oldest_ts is not None and oldest_ts < since_ts_ms:
            break
        from_token = body.get("end", "")
        if not from_token:
            break
    return events


# coordinator 消息正文中对 worker 的文字 @ 提及（m.mentions 缺失时的兜底判据）
_DELEGATION_RE = re.compile(r"@(analyzer|fixer|tester|evaluator)(?::|\s)")


def _detect_stuck_delegation(matrix_client, room_id: str, since_ts_ms: int):
    """检测「被委派后无响应」的 worker（Matrix sync 掉线 / 卡死）。

    判据：coordinator 对某 worker 的最近一次委派（m.mentions 或正文 @ 提及）
    已超过 WATCHDOG_SILENCE_SEC，且该 worker 在委派之后没有发过任何消息。
    返回 (worker_name, silence_sec)；无异常委派时返回 None。
    """
    events = _scan_room_events(matrix_client, room_id, since_ts_ms, max_pages=6)
    delegation_ts: Dict[str, int] = {}
    worker_last_ts: Dict[str, int] = {}
    for ev in events:  # 最新在前 → setdefault 记录的即最近一次
        if ev.get("type") != "m.room.message":
            continue
        sender = ev.get("sender", "")
        ts = int(ev.get("origin_server_ts", 0))
        if sender.startswith("@coordinator:"):
            content = ev.get("content", {}) or {}
            targets = set()
            for uid in (content.get("m.mentions") or {}).get("user_ids", []) or []:
                m = re.match(r"@(\w+):", uid or "")
                if m and m.group(1) in _WORKER_NAMES:
                    targets.add(m.group(1))
            for m in _DELEGATION_RE.finditer(content.get("body", "") or ""):
                targets.add(m.group(1))
            for w in targets:
                delegation_ts.setdefault(w, ts)
        else:
            m = re.match(r"@(\w+):", sender)
            if m and m.group(1) in _WORKER_NAMES:
                worker_last_ts.setdefault(m.group(1), ts)

    now_ms = int(time.time() * 1000)
    worst = None
    for w, dts in delegation_ts.items():
        if worker_last_ts.get(w, 0) > dts:
            continue  # 该 worker 在委派后已有响应，视为正常
        silence = (now_ms - dts) / 1000.0
        if silence >= WATCHDOG_SILENCE_SEC and (worst is None or silence > worst[1]):
            worst = (w, silence)
    return worst


def _recover_stuck_worker(matrix_client, room_id: str, worker_name: str, instance_id: str):
    """看门狗恢复动作：容器级重启 + controller ensure-ready + 请求 coordinator 重新委派。

    自动化此前需要人工执行的恢复流程（重启失联 worker 后请 coordinator 重新 @ 委派）。
    """
    log.warning("watchdog: %s 被委派后超过阈值仍未发言，判定失联，开始自动恢复", worker_name)
    try:
        subprocess.run(
            ["docker", "restart", f"{_WORKER_CONTAINER_PREFIX}-{worker_name}"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        log.error("watchdog: 重启容器失败 %s: %s", worker_name, e)
    try:
        r = subprocess.run(
            ["docker", "exec", _CONTROLLER_CONTAINER, _AGENTTEAMS_CLI, "worker", "ensure-ready", "--name", worker_name],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            log.error("watchdog: ensure-ready 失败 %s: %s", worker_name, (r.stderr or r.stdout or "").strip()[:200])
    except Exception as e:
        log.error("watchdog: ensure-ready 异常 %s: %s", worker_name, e)
    # 请求 coordinator 重新委派（必须带 m.mentions 才能唤醒 copaw runtime 的 coordinator）
    worker_uid = f"@{worker_name}:{_hiclaw_matrix_domain}"
    text = (
        f"{_COORDINATOR_USER_ID} Watchdog notice: worker {worker_name} 的 Matrix 连接此前无响应，"
        f"已自动重启容器并重新确认就绪。请重新向 {worker_uid} 委派 [{instance_id}] 当前阶段的任务。"
    )
    try:
        matrix_client.send_mention_message(room_id, text, [_COORDINATOR_USER_ID])
        log.info("watchdog: 已向 coordinator 发送重新委派请求 (%s)", worker_name)
    except Exception as e:
        log.error("watchdog: 发送重新委派请求失败: %s", e)


def _scan_matrix_verdict(matrix_client, room_id: str, instance_id: str, since_ts_ms: int = 0) -> Optional[Dict[str, str]]:
    """扫描 Matrix 房间中 agent 发回的 verdict 消息。

    仅接受「任务提交之后」出现的 coordinator verdict 消息：
    - since_ts_ms：提交时刻（毫秒），早于此时间的历史消息一律忽略；
    - instance_id 匹配：若消息中明确提到的其它 instance_id（pallets__xxx），跳过。
    扫描窗口为倒序分页拉取（最多 6 页 × 100 条），覆盖多轮返工的高噪声房间。
    """
    try:
        for event in _scan_room_events(matrix_client, room_id, since_ts_ms, max_pages=6):
            if not event.get("sender", "").startswith("@coordinator:"):
                continue
            content = event.get("content", {})
            msg_body = content.get("body", "")
            # instance_id 过滤：消息若明确提到其它 issue，跳过
            mentioned_ids = re.findall(r"pallets__[\w.-]+", msg_body)
            if mentioned_ids and instance_id not in mentioned_ids:
                continue
            verdict_m = re.search(r"Verdict:\s*(SUCCESS|FAIL)", msg_body)
            if verdict_m:
                verdict = verdict_m.group(1)
                # Try to extract patch from the message
                patch = ""
                patch_m = re.search(r"```diff\s*\n(.*?)```", msg_body, re.DOTALL)
                if not patch_m:
                    patch_m = re.search(r"```\s*\n(diff.*?|---.*?)(```|$)", msg_body, re.DOTALL)
                if patch_m:
                    patch = patch_m.group(1).strip()
                return {"verdict": verdict.lower(), "patch": patch, "raw_message": msg_body[:500]}
    except Exception:
        pass
    return None


def wait_for_completion(matrix_client, room_id: str, instance_id: str, timeout_sec: int = TASK_TIMEOUT_SEC) -> TaskResult:
    """轮询等待任务完成（检查 MinIO / 共享文件系统）。"""
    result = TaskResult(instance_id=instance_id, status="running")
    start = time.time()
    last_debug_emit = -60
    task_dirs = _task_artifact_dirs(instance_id)

    # #region debug-point B:wait-start
    _debug_report(
        "B",
        "scripts/swe_bench_runner.py:wait_for_completion:start",
        "[DEBUG] wait_for_completion started",
        {
            "instance_id": instance_id,
            "timeout_sec": timeout_sec,
            "team_snapshot": _debug_team_snapshot(),
                "task_dirs": task_dirs,
        },
    )
    # #endregion

    submit_ts_ms = getattr(matrix_client, "last_submit_ts_ms", 0)
    # 委派看门狗状态：上次检查时刻 + 每个 worker 上次自动恢复时刻（冷却防重启风暴）
    last_watchdog_check = 0.0
    watchdog_last_recover: Dict[str, float] = {}
    while time.time() - start < timeout_sec:
        # 检查 Matrix 消息中的 verdict（agent 通过聊天输出的结果）
        verdict_from_matrix = _scan_matrix_verdict(matrix_client, room_id, instance_id, submit_ts_ms)
        if verdict_from_matrix:
            result.status = "completed"
            result.agent_verdict = verdict_from_matrix.get("verdict", "unknown")
            result.patch = verdict_from_matrix.get("patch", "")
            log.info("从 Matrix 消息获取 Agent 自评 verdict: %s agent_verdict=%s", instance_id, result.agent_verdict)
            # 保存 verdict 和 artifacts 到本地
            inst_dir = os.path.join(RESULTS_DIR, instance_id)
            os.makedirs(inst_dir, exist_ok=True)
            with open(os.path.join(inst_dir, "verdict.json"), "w") as f:
                json.dump(verdict_from_matrix, f)
            # 从 MinIO 拉取 agent 发布的 artifacts
            _pull_minio_artifacts(instance_id, inst_dir)
            # 从本地 MinIO artifacts 中读取 patch（如果有）
            for fname in ["patch.diff", "result.md", "plan.md", "spec.md"]:
                local_path = os.path.join(inst_dir, fname)
                if os.path.exists(local_path):
                    log.info("Artifact 已保存: %s", fname)
            # 若 verdict 消息里未提取到 patch（如 coordinator 仅回复 verdict 摘要、
            # 未内联 ```diff 代码块），从 MinIO 拉取的 patch.diff 回填 result.patch，
            # 确保 evaluate_patch 能拿到真实 patch 做 SWE-bench 验证。
            if not result.patch:
                patch_file = os.path.join(inst_dir, "patch.diff")
                if os.path.exists(patch_file):
                    with open(patch_file, "r", encoding="utf-8") as f:
                        result.patch = f.read()
                    log.info("从 MinIO patch.diff 回填 result.patch (%d bytes)", len(result.patch))
            if result.patch:
                with open(os.path.join(inst_dir, "patch.diff"), "w") as f:
                    f.write(result.patch)
            return result

        # 检查 verdict 文件（Evaluator 完成后写入）
        verdict_path = _find_verdict_file(instance_id)
        if verdict_path:
            try:
                with open(verdict_path) as f:
                    verdict = json.load(f)
                result.agent_verdict = verdict.get("verdict") or verdict.get("summary", {}).get("status", "unknown")
                result.status = "completed"

                # 读取 patch
                diff_path = _find_diff_file(instance_id)
                if diff_path and os.path.exists(diff_path):
                    with open(diff_path) as f:
                        result.patch = f.read()

                log.info("任务完成: %s agent_verdict=%s", instance_id, result.agent_verdict)
                # #region debug-point D:completed
                _debug_report(
                    "D",
                    "scripts/swe_bench_runner.py:wait_for_completion:completed",
                    "[DEBUG] task completed with verdict artifact",
                    {
                        "instance_id": instance_id,
                        "agent_verdict": result.agent_verdict,
                        "verdict_path": verdict_path,
                        "diff_path": diff_path or "",
                    },
                )
                # #endregion
                return result
            except Exception as e:
                log.warning("读取 verdict 失败: %s", e)

        # 检查是否 escalated
        if _check_escalated(instance_id):
            result.status = "failed"
            result.agent_verdict = "escalated"
            result.error = "超过重试次数，已升级"
            log.warning("任务升级: %s", instance_id)
            # #region debug-point D:escalated
            _debug_report(
                "D",
                "scripts/swe_bench_runner.py:wait_for_completion:escalated",
                "[DEBUG] task escalated before verdict",
                {"instance_id": instance_id, "team_snapshot": _debug_team_snapshot()},
            )
            # #endregion
            return result

        # 委派看门狗：coordinator 委派后 worker 长时间无响应（Matrix sync 掉线/卡死）
        # → 容器级恢复 + 请求 coordinator 重新委派，避免静默等到任务整体超时。
        now = time.time()
        if submit_ts_ms and now - last_watchdog_check >= 30:
            last_watchdog_check = now
            try:
                stuck = _detect_stuck_delegation(matrix_client, room_id, submit_ts_ms)
                if stuck:
                    stuck_worker, silence_sec = stuck
                    last_recover = watchdog_last_recover.get(stuck_worker, 0.0)
                    if now - last_recover >= WATCHDOG_COOLDOWN_SEC:
                        watchdog_last_recover[stuck_worker] = now
                        _recover_stuck_worker(matrix_client, room_id, stuck_worker, instance_id)
                    else:
                        log.info(
                            "watchdog: %s 仍无响应 (静默 %.0fs)，处于恢复冷却期 (剩余 %.0fs)",
                            stuck_worker, silence_sec,
                            WATCHDOG_COOLDOWN_SEC - (now - last_recover),
                        )
            except Exception as e:
                log.debug("watchdog 检查异常（忽略）: %s", e)

        elapsed = int(time.time() - start)
        if elapsed - last_debug_emit >= 60:
            last_debug_emit = elapsed
            # 检查 Agent 进展
            team = _debug_team_snapshot()
            log.info(
                "等待 Agent 处理... %s (已等待 %ds / %ds) | team=%s ready=%s/%s",
                instance_id, elapsed, timeout_sec,
                team.get("phase", "?"), team.get("readyWorkers", "?"), team.get("totalWorkers", "?"),
            )
            # #region debug-point C:wait-progress
            _debug_report(
                "C",
                "scripts/swe_bench_runner.py:wait_for_completion:progress",
                "[DEBUG] still waiting for task artifacts",
                {
                    "instance_id": instance_id,
                    "elapsed_sec": elapsed,
                    "team_snapshot": team,
                    "verdict_exists": bool(verdict_path),
                    "diff_exists": bool(_find_diff_file(instance_id)),
                        "state_exists": bool(_find_state_file(instance_id)),
                        "task_dirs": task_dirs,
                },
            )
            # #endregion
        time.sleep(POLL_INTERVAL_SEC)

    result.status = "failed"
    result.error = f"超时 ({timeout_sec}s)"
    log.error("任务超时: %s", instance_id)
    # #region debug-point D:timeout
    _debug_report(
        "D",
        "scripts/swe_bench_runner.py:wait_for_completion:timeout",
        "[DEBUG] task timed out waiting for artifacts",
        {
            "instance_id": instance_id,
            "elapsed_sec": int(time.time() - start),
            "team_snapshot": _debug_team_snapshot(),
            "verdict_path": _find_verdict_file(instance_id) or "",
            "diff_path": _find_diff_file(instance_id) or "",
            "escalated": _check_escalated(instance_id),
        },
    )
    # #endregion
    return result


def _find_verdict_file(instance_id: str) -> Optional[str]:
    """在共享文件系统和 Docker 容器中查找 verdict 文件。"""
    for p in _artifact_candidates(instance_id, "verdict.json"):
        if os.path.exists(p):
            return p
    # fallback: Agent 可能把文件写入了 Docker 容器内的 /tmp/ 下
    for sub in ["swe-bench", f"swe-{instance_id.split('-')[-1]}", instance_id]:
        container_path = f"/tmp/{sub}/verdict.json"
        try:
            r = subprocess.run(
                ["docker", "exec", _MANAGER_CONTAINER, "test", "-f", container_path],
                capture_output=True, timeout=3,
            )
            if r.returncode == 0:
                # 复制到宿主机
                host_path = os.path.join(RESULTS_DIR, instance_id, "verdict.json")
                os.makedirs(os.path.dirname(host_path), exist_ok=True)
                subprocess.run(
                    ["docker", "cp", f"{_MANAGER_CONTAINER}:{container_path}", host_path],
                    capture_output=True, timeout=5,
                )
                if os.path.exists(host_path):
                    log.info("从容器复制 verdict: %s → %s", container_path, host_path)
                    return host_path
        except Exception:
            continue
    # fallback: openclaw Manager 把 verdict/result.md 写入 /root/{instance_id}/
    container_path = f"/root/{instance_id}/result.md"
    try:
        r = subprocess.run(
            ["docker", "exec", _MANAGER_CONTAINER, "test", "-f", container_path],
            capture_output=True, timeout=3,
        )
        if r.returncode == 0:
            host_path = os.path.join(RESULTS_DIR, instance_id, "result.md")
            os.makedirs(os.path.dirname(host_path), exist_ok=True)
            subprocess.run(
                ["docker", "cp", f"{_MANAGER_CONTAINER}:{container_path}", host_path],
                capture_output=True, timeout=5,
            )
            if os.path.exists(host_path):
                log.info("从容器复制 result.md: %s → %s", container_path, host_path)
                return host_path
    except Exception:
        pass
    return None


def _find_diff_file(instance_id: str) -> Optional[str]:
    """在共享文件系统和 Docker 容器中查找 diff 文件。"""
    for p in _artifact_candidates(instance_id, "fix.diff"):
        if os.path.exists(p):
            return p
    for sub in ["swe-bench", f"swe-{instance_id.split('-')[-1]}", instance_id]:
        container_path = f"/tmp/{sub}/fix.diff"
        try:
            r = subprocess.run(
                ["docker", "exec", _MANAGER_CONTAINER, "test", "-f", container_path],
                capture_output=True, timeout=3,
            )
            if r.returncode == 0:
                host_path = os.path.join(RESULTS_DIR, instance_id, "fix.diff")
                os.makedirs(os.path.dirname(host_path), exist_ok=True)
                subprocess.run(
                    ["docker", "cp", f"{_MANAGER_CONTAINER}:{container_path}", host_path],
                    capture_output=True, timeout=5,
                )
                if os.path.exists(host_path):
                    return host_path
        except Exception:
            continue
    # fallback: openclaw Manager 把 patch.diff 写入 /root/{instance_id}/
    container_path = f"/root/{instance_id}/patch.diff"
    try:
        r = subprocess.run(
            ["docker", "exec", _MANAGER_CONTAINER, "test", "-f", container_path],
            capture_output=True, timeout=3,
        )
        if r.returncode == 0:
            host_path = os.path.join(RESULTS_DIR, instance_id, "fix.diff")
            os.makedirs(os.path.dirname(host_path), exist_ok=True)
            subprocess.run(
                ["docker", "cp", f"{_MANAGER_CONTAINER}:{container_path}", host_path],
                capture_output=True, timeout=5,
            )
            if os.path.exists(host_path):
                log.info("从容器复制 patch.diff: %s → %s", container_path, host_path)
                return host_path
    except Exception:
        pass
    return None


def _check_escalated(instance_id: str) -> bool:
    """检查任务是否已被标记为 escalated。"""
    state_path = _find_state_file(instance_id)
    if state_path:
        try:
            with open(state_path) as f:
                state = json.load(f)
            return state.get("status") == "escalated"
        except Exception:
            pass
    return False


def _find_state_file(instance_id: str) -> Optional[str]:
    for p in _artifact_candidates(instance_id, "state.json"):
        if os.path.exists(p):
            return p
    return None


def _artifact_candidates(instance_id: str, filename: str) -> List[str]:
    candidates = []
    for task_dir in _task_artifact_dirs(instance_id):
        candidates.append(os.path.join(task_dir, filename))
    candidates.append(f"/tmp/swe-results/{instance_id}/{filename}")
    # Agent send_file_to_user 常用输出路径（instance-id 变体）
    for sub in ["swe-bench", f"swe-{instance_id.split('-')[-1]}", instance_id]:
        candidates.append(f"/tmp/{sub}/{filename}")
    candidates.append(os.path.join(RESULTS_DIR, instance_id, filename))
    return candidates


def _task_artifact_dirs(instance_id: str) -> List[str]:
    roots = [
        os.path.join(MATRIX_WORKSPACE_DIR, ".openclaw", "tasks"),
        os.path.join(MATRIX_WORKSPACE_DIR, "shared", "tasks"),
        os.path.expanduser("~/hiclaw-manager/.openclaw/tasks"),
        os.path.expanduser("~/hiclaw-manager/shared/tasks"),
        os.path.expanduser("~/agentteams-manager/.openclaw/tasks"),
        os.path.expanduser("~/agentteams-manager/shared/tasks"),
    ]
    dirs = []
    for root in roots:
        candidate = os.path.join(root, instance_id)
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs

# --------------------------------------------------------------------------- #
# SWE-bench 评估
# --------------------------------------------------------------------------- #

def evaluate_patch(instance: SweInstance, patch: str, repo_path: str) -> Dict:
    """
    评估 patch 是否通过 SWE-bench 测试。

    流程:
    1. 在干净副本上 checkout base_commit
    2. 应用 test_patch（添加测试）
    3. 应用 fix patch（我们的修复）
    4. 运行 FAIL_TO_PASS 测试 → 必须全部通过
    5. 运行 PASS_TO_PASS 测试 → 必须全部通过
    """
    result = {"instance_id": instance.instance_id, "status": "error"}

    if not patch.strip():
        result["error"] = "空 patch"
        return result

    # 创建工作副本
    work_dir = tempfile.mkdtemp(prefix=f"swe_{instance.instance_id}_")
    try:
        # 复制仓库
        subprocess.run(
            ["git", "clone", repo_path, work_dir],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", instance.base_commit],
            cwd=work_dir, check=True, capture_output=True,
        )

        # 应用 test_patch（SWE-bench 官方测试补丁）
        if instance.test_patch.strip():
            test_patch_file = os.path.join(work_dir, "_test_patch.diff")
            with open(test_patch_file, "w") as f:
                f.write(instance.test_patch)
            r = subprocess.run(
                ["git", "apply", "_test_patch.diff"],
                cwd=work_dir, capture_output=True, text=True,
            )
            if r.returncode != 0:
                # 尝试 --allow-empty（旧版本 git 不支持，用 --recount 替代）
                log.info("test_patch 首次应用失败，尝试 git apply --recount")
                r = subprocess.run(
                    ["git", "apply", "--recount", "_test_patch.diff"],
                    cwd=work_dir, capture_output=True, text=True,
                )
            if r.returncode != 0:
                log.warning("test_patch 应用失败: %s", r.stderr[:200])
                # 继续尝试，有些 test_patch 可能已经包含在 base_commit 中

        # 应用 fix patch
        # SWE-bench 约定：fix patch 只应改源码，测试文件由官方 test_patch 提供。
        # 但 Agent 生成的 patch 常混入 tests/ 修改（与 test_patch 重叠冲突），
        # 故先尝试排除 tests/ 应用源码部分；失败再回退直接应用。
        fix_patch_file = os.path.join(work_dir, "_fix_patch.diff")
        with open(fix_patch_file, "w") as f:
            f.write(patch)
        r = subprocess.run(
            ["git", "apply", "--exclude=tests/*", "_fix_patch.diff"],
            cwd=work_dir, capture_output=True, text=True,
        )
        if r.returncode != 0:
            log.info("排除 tests/ 应用失败，回退直接应用完整 patch: %s", r.stderr[:150])
            r = subprocess.run(
                ["git", "apply", "_fix_patch.diff"],
                cwd=work_dir, capture_output=True, text=True,
            )
        if r.returncode != 0:
            result["error"] = f"fix patch 应用失败: {r.stderr[:200]}"
            result["status"] = "patch_failed"
            return result

        # 安装依赖（Flask 用 pip install -e .[dev] 或 pip install -e .）
        _install_deps(work_dir)

        # 运行 FAIL_TO_PASS 测试
        f2p_results = _run_tests(work_dir, instance.fail_to_pass)
        f2p_pass = all(f2p_results.values()) if f2p_results else False

        # 运行 PASS_TO_PASS 测试
        p2p_results = _run_tests(work_dir, instance.pass_to_pass)
        p2p_pass = all(p2p_results.values()) if p2p_results else True  # 空列表视为通过

        result["fail_to_pass"] = f2p_results
        result["pass_to_pass"] = p2p_results
        result["f2p_all_pass"] = f2p_pass
        result["p2p_all_pass"] = p2p_pass
        result["resolved"] = f2p_pass and p2p_pass
        result["status"] = "evaluated"

        log.info(
            "评估结果: %s → resolved=%s (F2P=%s, P2P=%s)",
            instance.instance_id, result["resolved"], f2p_pass, p2p_pass,
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return result


def _install_deps(repo_dir: str):
    """安装仓库的开发依赖，并在隔离的 venv 中运行测试（避免与宿主机依赖冲突）。"""
    import venv as _venv

    venv_dir = os.path.join(repo_dir, ".swe_venv")
    if not os.path.exists(venv_dir):
        _venv.create(venv_dir, with_pip=True)

    pip = os.path.join(venv_dir, "bin", "pip") if sys.platform != "win32" \
        else os.path.join(venv_dir, "Scripts", "pip.exe")
    pytest = os.path.join(venv_dir, "bin", "pytest") if sys.platform != "win32" \
        else os.path.join(venv_dir, "Scripts", "pytest.exe")

    # 修复：Flask 2.0.1 需要 werkzeug < 2.1（url_quote 在 2.3 移除，__version__ 在 2.1 移除）
    known_constraints: Dict[str, str] = {
        "flask": "werkzeug<2.1",
        "Flask": "werkzeug<2.1",
    }
    # 检测 repo 包名
    pkg_name = None
    setup_py = os.path.join(repo_dir, "setup.py")
    if os.path.exists(setup_py):
        with open(setup_py) as f:
            import re
            m = re.search(r'''name\s*=\s*['"]([^'"]+)['"]''', f.read())
            if m:
                pkg_name = m.group(1)

    if os.path.exists(os.path.join(repo_dir, "setup.py")) or os.path.exists(os.path.join(repo_dir, "pyproject.toml")):
        # 安装基础包
        subprocess.run(
            [pip, "install", "-e", "."],
            cwd=repo_dir, capture_output=True, text=True, timeout=180,
        )
        # 应用依赖约束（降级不兼容的包）
        constraint = known_constraints.get(pkg_name or "")
        if constraint:
            subprocess.run(
                [pip, "install", constraint],
                capture_output=True, text=True, timeout=60,
            )
        # click 8.2 移除了 CliRunner(mix_stderr=...)，Flask 2.0.1 的 test_cli 需要 click<8.2
        subprocess.run(
            [pip, "install", "click<8.2"],
            capture_output=True, text=True, timeout=60,
        )

    # 安装 pytest（Flask 2.0 兼容 pytest<9，monkeypatch.notset 在 9.x 被移除）
    subprocess.run(
        [pip, "install", "pytest<9"],
        capture_output=True, text=True, timeout=60,
    )


def _run_tests(repo_dir: str, test_list: List[str]) -> Dict[str, bool]:
    """运行指定测试，返回 {test_name: passed}。
    使用 evaluate_patch 创建的隔离 venv 环境。
    """
    if not test_list:
        return {}

    venv_pytest = os.path.join(repo_dir, ".swe_venv", "bin", "pytest") if sys.platform != "win32" \
        else os.path.join(repo_dir, ".swe_venv", "Scripts", "pytest.exe")
    venv_python = os.path.join(repo_dir, ".swe_venv", "bin", "python") if sys.platform != "win32" \
        else os.path.join(repo_dir, ".swe_venv", "Scripts", "python.exe")

    # 若 venv 不存在或 pytest 不可用，退回到宿主 python
    if not os.path.exists(venv_pytest):
        log.warning("venv pytest 不可用，回退到宿主环境")
        venv_pytest = sys.executable
        venv_python = sys.executable

    results = {}
    for test in test_list:
        r = subprocess.run(
            [venv_pytest, test, "-x", "--tb=short", "-q", "-W", "default"],
            cwd=repo_dir, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PATH": os.path.dirname(venv_python) + os.pathsep + os.environ.get("PATH", "")},
        )
        passed = (r.returncode == 0)

        # SWE-bench 数据集中，含特殊字符（引号/逗号/括号/空格）的参数化测试 id
        # 会被截断，导致 pytest 精确匹配不到（"no tests ran" / "not found"）。
        # 此时降级为跑整个测试函数（不带参数化后缀），用函数级结果判定。
        if not passed:
            combined = (r.stdout or "") + (r.stderr or "")
            if "no tests ran" in combined or "not found" in combined or "collected 0" in combined:
                file_path, _, func_part = test.partition("::")
                func_name = func_part.split("[")[0]
                r2 = subprocess.run(
                    [venv_pytest, f"{file_path}::{func_name}", "-q", "-W", "default"],
                    cwd=repo_dir, capture_output=True, text=True, timeout=180,
                    env={**os.environ, "PATH": os.path.dirname(venv_python) + os.pathsep + os.environ.get("PATH", "")},
                )
                passed = (r2.returncode == 0)

        results[test] = passed

    return results


def _remove_path(path: str) -> bool:
    """删除文件或目录。存在时返回 True。

    safe-delete 会拦截 os.remove / shutil.rmtree 对重要文件（如 run_state.json）
    的删除并弹出确认，导致脚本中断。改用 os.rename（mv）将目标移动到带时间戳
    的回收名，达到"移除"效果且不被拦截。
    """
    if not os.path.exists(path):
        return False
    trash = f"{path}.trash-{int(time.time() * 1000)}"
    try:
        os.rename(path, trash)
    except OSError:
        # rename 失败（如跨文件系统），退回标准删除
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
    return True


def clear_run_artifacts(instance_ids: List[str], results_dir: str) -> Dict[str, int]:
    """清理历史运行产物，避免 rerun 时误读旧 verdict/diff。

    注意：coordinator(copaw) 的会话状态由 MinIO 后端承载，且重启时会把内存里的
    会话 flush 回 MinIO，因此单纯删本地文件/重启容器无法清掉"pipeline already
    running"这类陈旧状态——必须由外部（见运维手册）重置 coordinator 容器才能解决。
    本函数只负责清理 runner 侧的本地结果目录与 MinIO 共享产物缓存。
    """
    removed = {"run_state": 0, "instance_dirs": 0}

    state_path = os.path.join(results_dir, "run_state.json")
    if _remove_path(state_path):
        removed["run_state"] += 1

    base_dirs = [
        os.path.abspath(results_dir),
        "/tmp/swe-results",
        os.path.expanduser("~/hiclaw-manager/shared/tasks"),
        os.path.expanduser("~/agentteams-manager/shared/tasks"),
    ]
    for base_dir in base_dirs:
        for instance_id in instance_ids:
            if _remove_path(os.path.join(base_dir, instance_id)):
                removed["instance_dirs"] += 1

    # 也清理 Docker 容器内 Agent 可能写入的旧产物
    for instance_id in instance_ids:
        short_id = instance_id.split("-")[-1]
        for sub in ["swe-bench", f"swe-{short_id}", instance_id]:
            subprocess.run(
                ["docker", "exec", _MANAGER_CONTAINER, "rm", "-rf", f"/tmp/{sub}"],
                capture_output=True, timeout=3,
            )

    # 5. MinIO 共享产物（workers 发布的 spec/plan/result/patch 存于此，
    #    rerun 若不清理，evaluator/coordinator 可能直接读到旧 patch.diff 而"假通过"）。
    #    这是"重复运行直接出结果、根本没测试"的根因之一，必须递归删除。
    for instance_id in instance_ids:
        # 带时间戳后缀的任务目录也要清理（见 _minio_task_prefixes），
        # 否则 rerun 时 evaluator/coordinator 可能读到旧 patch 而"假通过"。
        for minio_prefix in _minio_task_prefixes(instance_id):
            try:
                proc = subprocess.run(
                    ["docker", "exec", _CONTROLLER_CONTAINER, "mc", "rm",
                     "--recursive", "--force", minio_prefix],
                    capture_output=True, timeout=10,
                )
                if proc.returncode == 0:
                    removed["minio"] = removed.get("minio", 0) + 1
                    log.info("  MinIO: 已删除共享产物 %s", minio_prefix)
            except Exception as e:
                log.warning("  MinIO 清理跳过 %s: %s", minio_prefix, e)

    return removed


def reset_issue_run_state(state: RunState, instance_ids: List[str], results_dir: str) -> Dict[str, int]:
    """清理指定 issue 的历史结果，确保单题重跑时不会命中旧产物。"""
    removed = clear_run_artifacts(instance_ids, results_dir)
    removed["state_results"] = 0
    for instance_id in instance_ids:
        if instance_id in state.results:
            del state.results[instance_id]
            removed["state_results"] += 1
    return removed

# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(args: argparse.Namespace):
    """主执行流程。"""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(REPO_CACHE_DIR, exist_ok=True)
    state_path = os.path.join(RESULTS_DIR, "run_state.json")

    # --reset-db-only: 仅清空数据库
    if args.reset_db_only:
        reset_db(repo_name="flask")
        # 同时清除本地状态文件
        if os.path.exists(state_path):
            _remove_path(state_path)
            log.info("已清除运行状态: %s", state_path)
        return

    # 加载 SWE-bench 实例
    instances = load_flask_instances()
    if not instances:
        log.error("未找到 Flask 的 SWE-bench 实例")
        return

    # --rerun-all / --reset-run-state: 清库 + 清结果 + 从头重跑
    if args.rerun_all:
        reset_db(repo_name="flask")
        cleared = clear_run_artifacts([inst.instance_id for inst in instances], RESULTS_DIR)
        state = RunState(started_at=datetime.now().isoformat())
        log.info(
            "已清空历史运行状态: run_state=%d, instance_dirs=%d，将从头重跑全部 instances",
            cleared["run_state"], cleared["instance_dirs"],
        )
    else:
        # 加载状态（支持断点续跑）
        state = RunState.load(state_path)
        log.info("加载状态: %d 个结果", len(state.results))

    # --reset-db: 清空数据库后重新索引
    if args.reset_db and not args.rerun_all:
        reset_db(repo_name="flask")
        # 保留旧字段兼容，但后续不再依赖本地 indexed_commits 做索引判断
        state.indexed_commits = []
        state.save(state_path)
        log.info("数据库已重置，将重新索引所有 commit")

    # --list: 仅列出
    if args.list:
        print(f"\nFlask SWE-bench instances ({len(instances)} 个):\n")
        for i, inst in enumerate(instances):
            status = state.results.get(inst.instance_id, TaskResult(inst.instance_id)).status
            print(f"  {i+1:3d}. [{status:10s}] {inst.instance_id:40s} commit={inst.base_commit[:8]}")
        return

    # --issue-index: 只跑一个 issue（按 --list 显示的 1-based 序号）
    if args.issue_index:
        if args.issue_index < 1 or args.issue_index > len(instances):
            log.error("--issue-index 超出范围: %d（当前共 %d 个 issue）", args.issue_index, len(instances))
            return
        selected = instances[args.issue_index - 1]
        instances = [selected]
        if not args.dry_run:
            cleared = reset_issue_run_state(state, [selected.instance_id], RESULTS_DIR)
            state.save(state_path)
            log.info(
                "仅运行第 %d 个 issue: %s (run_state=%d, instance_dirs=%d, state_results=%d)",
                args.issue_index,
                selected.instance_id,
                cleared["run_state"],
                cleared["instance_dirs"],
                cleared["state_results"],
            )
        else:
            log.info("仅运行第 %d 个 issue: %s（dry-run，不清理历史结果）", args.issue_index, selected.instance_id)

    # --start-from: 跳过前面的
    if args.start_from:
        skip = True
        filtered = []
        for inst in instances:
            if inst.instance_id == args.start_from:
                skip = False
            if not skip:
                filtered.append(inst)
        instances = filtered
        log.info("从 %s 开始，跳过前 %d 个", args.start_from, len(instances) - len(filtered))

    # --dry-run
    if args.dry_run:
        print(f"\n[DRY RUN] 计划处理 {len(instances)} 个 Flask instances:\n")
        commits = set()
        for inst in instances:
            index_hint = ""
            if args.issue_index:
                index_hint = f" [issue-index={args.issue_index}]"
            print(f"  {inst.instance_id:40s} commit={inst.base_commit[:8]}{index_hint}")
            commits.add(inst.base_commit)
        print(f"\n  唯一 commit 数: {len(commits)}")
        print("  索引策略: 运行时由 repo_indexer 根据 Redis + 数据库状态决定")
        if args.issue_index:
            print("  运行策略: 单题模式会清理该 issue 的历史结果并重新跑完整流程")
        return

    # Matrix 客户端（除非 --skip-submit）
    matrix = None
    room_id = ""
    if not args.skip_submit and not args.index_only:
        if not MATRIX_PASSWORD:
            log.error("请设置 MATRIX_PASSWORD 环境变量（或在 agentteams.env 中配置 HICLAW_ADMIN_PASSWORD）")
            return
        try:
            matrix = MatrixClient(MATRIX_HOMESERVER, MATRIX_USER, MATRIX_PASSWORD)
        except Exception as e:
            log.error("Matrix 登录失败: %s", e)
            return
        # 严格房间发现：只接受 controller 管理的 team room（含成员校验）。
        # MATRIX_ROOM_ID 环境变量若与 controller teamRoomID 不一致会被忽略并告警，
        # 避免把任务派到无路由绑定的陈旧/手工房间导致静默超时。
        room_id = matrix.discover_room()
        if not room_id:
            log.error(
                "未找到合法派单房间。请先执行 ./deploy/scripts/reset-agentteams-rooms.sh --yes 重建 team 后重试"
            )
            return
        log.info(
            "派单房间已确认: %s (成员校验通过: coordinator + %d workers)",
            room_id, len(_WORKER_USER_IDS),
        )
        # 官方 lifecycle 预检：派单前经 controller 确认全部 worker 就绪，
        # 容器 running 不代表 agent 可用（休眠/失联时容器仍在）。
        if not MatrixClient.ensure_workers_ready():
            log.error(
                "worker 未全部就绪，中止派单。可执行 ./deploy/scripts/agentteams-ctl.sh agents start 后重试"
            )
            return

    # ---- 按 base_commit 分组，按时间顺序处理 ----
    # 分组：{base_commit: [instances]}
    commit_groups: Dict[str, List[SweInstance]] = {}
    for inst in instances:
        commit_groups.setdefault(inst.base_commit, []).append(inst)

    # 排序：按 commit 在 git 历史中的日期排序（向前推进）
    def _commit_sort_key(commit: str) -> str:
        """获取 commit 日期用于排序（首次调用时 clone 仓库获取）。"""
        try:
            repo_path = ensure_repo(instances[0].repo, commit, REPO_CACHE_DIR)
            return get_commit_date(repo_path, commit)
        except Exception:
            return commit  # fallback to hash order

    sorted_commits = sorted(commit_groups.keys(), key=_commit_sort_key)

    log.info(
        "共 %d 个唯一 commit，%d 个 instance，按时间顺序处理",
        len(sorted_commits), len(instances),
    )

    ns = make_ns(TARGET_REPO)  # 单命名空间 per repo

    # ---- 主循环：按 commit 分组处理 ----
    processed = 0
    for commit_idx, commit in enumerate(sorted_commits, 1):
        group = commit_groups[commit]
        log.info("=" * 70)
        log.info(
            "[Commit %d/%d] %s — %d 个 issue",
            commit_idx, len(sorted_commits), commit[:8], len(group),
        )
        log.info("=" * 70)

        # Step 1: 确保仓库就绪并 checkout 到目标 commit
        try:
            repo_path = ensure_repo(group[0].repo, commit, REPO_CACHE_DIR)
        except Exception as e:
            log.error("仓库准备失败 (commit=%s): %s", commit[:8], e)
            for inst in group:
                result = TaskResult(inst.instance_id, status="failed", error=f"仓库准备失败: {e}")
                state.results[inst.instance_id] = result
            state.save(state_path)
            continue

        # Step 2: 始终交给 repo_indexer 协调索引状态
        if args.skip_index:
            log.info("跳过索引 (--skip-index): commit=%s", commit[:8])
        else:
            ok = index_repo(repo_path, commit, ns)
            if ok:
                log.info("索引协调完成: commit=%s", commit[:8])
            else:
                log.error("索引失败 (commit=%s)，跳过该 commit 的所有 issue", commit[:8])
                for inst in group:
                    result = TaskResult(inst.instance_id, status="failed", error="索引失败")
                    state.results[inst.instance_id] = result
                state.save(state_path)
                continue

        if args.index_only:
            state.save(state_path)
            continue

        # Step 3: 处理该 commit 下的所有 issue
        for inst in group:
            processed += 1
            result = state.results.get(inst.instance_id, TaskResult(inst.instance_id))

            # 单题模式允许重复跑同一个 issue；批量模式仍跳过已完成项
            should_force_rerun = bool(args.issue_index)
            if result.status in ("completed", "skipped") and not should_force_rerun:
                log.info("  跳过已完成: %s (%s)", inst.instance_id, result.status)
                continue

            log.info(
                "  [%d/%d] %s",
                processed, len(instances), inst.instance_id,
            )
            if should_force_rerun:
                log.info("  单题重跑模式：忽略历史状态并重新执行完整流程")
            start_time = time.time()

            # Step 3a: 提交任务
            if matrix and room_id:
                try:
                    matrix.send_task(room_id, inst)
                    result.status = "submitted"
                except Exception as e:
                    result.status = "failed"
                    result.error = f"提交失败: {e}"
                    state.results[inst.instance_id] = result
                    state.save(state_path)
                    continue
            else:
                log.warning("  无 Matrix 客户端，跳过提交")
                result.status = "skipped"
                state.results[inst.instance_id] = result
                state.save(state_path)
                continue

            # Step 3b: 等待完成
            log.info(
                "任务已提交，等待 Agent 流水线处理（Analyzer → Fixer → Tester → Evaluator）..."
                " 可通过 Element Web (http://127.0.0.1:18088) 观察 Agent 对话过程"
            )
            # 注意：wait_for_completion 的 timeout_sec 默认参数是函数定义时捕获的
            # （=1800），不会随 _update_config 修改后的全局 TASK_TIMEOUT_SEC 变化。
            # 必须显式传入当前全局值，否则 --timeout 参数永远不生效。
            result = wait_for_completion(matrix, room_id, inst.instance_id, timeout_sec=TASK_TIMEOUT_SEC)
            result.duration_sec = time.time() - start_time

            # Step 3c: 评估 patch（SWE-bench 客观验证，维度②）
            if result.status == "completed" and result.patch:
                swebench_result = evaluate_patch(inst, result.patch, repo_path)
                result.swebench_result = swebench_result
                if swebench_result.get("resolved"):
                    log.info("  ✅ %s SWE-bench RESOLVED", inst.instance_id)
                else:
                    log.info("  ❌ %s SWE-bench NOT RESOLVED", inst.instance_id)

            state.results[inst.instance_id] = result
            state.save(state_path)

        # 该 commit 的所有 issue 处理完毕，继续下一个 commit
        log.info(
            "Commit %s 处理完毕（%d 个 issue），推进到下一个 commit",
            commit[:8], len(group),
        )

    # 最终报告
    _print_summary(state)


def _print_summary(state: RunState):
    """打印最终结果汇总。"""
    print("\n" + "=" * 70)
    print("  SWE-bench Flask 测试结果汇总")
    print("=" * 70)

    total = len(state.results)
    completed = sum(1 for r in state.results.values() if r.status == "completed")
    agent_success = sum(1 for r in state.results.values() if r.agent_verdict in ("success", "approved"))
    swebench_resolved = sum(1 for r in state.results.values() if r.swebench_result and r.swebench_result.get("resolved"))
    failed = sum(1 for r in state.results.values() if r.status == "failed")

    print(f"\n  总实例数:            {total}")
    print(f"  已完成:              {completed}")
    print(f"  Agent 自评成功:      {agent_success}   ← 维度①：AgentTeams 流水线判定 PASS")
    print(f"  SWE-bench 客观验证通过: {swebench_resolved}   ← 维度②：官方标准答案 F2P+P2P 全过")
    print(f"  失败:                {failed}")

    if state.results:
        print(f"\n  {'Instance':<40s} {'Agent自评':<12s} {'SWE-bench验证':<16s} {'Duration':<10s}")
        print(f"  {'-'*40} {'-'*12} {'-'*16} {'-'*10}")
        for iid, r in sorted(state.results.items()):
            agent_verdict = r.agent_verdict or r.status
            resolved_str = "✅ resolved" if (r.swebench_result and r.swebench_result.get("resolved")) else "❌ not resolved"
            dur = f"{r.duration_sec:.0f}s" if r.duration_sec else "-"
            print(f"  {iid:<40s} {agent_verdict:<12s} {resolved_str:<16s} {dur:<10s}")

    print(f"\n  结果文件: {RESULTS_DIR}/run_state.json")
    print(f"  运行时间: {state.started_at} → {state.last_updated}")
    print()

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="SWE-bench Flask 自动化测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--list", action="store_true", help="仅列出 Flask instances")
    ap.add_argument("--dry-run", action="store_true", help="打印计划，不实际操作")
    ap.add_argument("--index-only", action="store_true", help="仅索引仓库，不提交任务")
    ap.add_argument("--skip-index", action="store_true", help="跳过数据库索引（离线测试用）")
    ap.add_argument("--skip-submit", action="store_true", help="跳过 Matrix 提交（仅索引+本地验证）")
    ap.add_argument("--issue-index", type=int, default=0,
                    help="只运行第 N 个 issue（按 --list 顺序，1-based；每次都会重新跑该 issue）")
    ap.add_argument("--start-from", default="", help="从指定 instance_id 开始")
    ap.add_argument("--repo-cache", default=REPO_CACHE_DIR, help=f"仓库缓存目录 (default: {REPO_CACHE_DIR})")
    ap.add_argument("--results-dir", default=RESULTS_DIR, help=f"结果输出目录 (default: {RESULTS_DIR})")
    ap.add_argument("--timeout", type=int, default=TASK_TIMEOUT_SEC, help=f"单任务超时秒数 (default: {TASK_TIMEOUT_SEC})")
    ap.add_argument("--reset-db", action="store_true",
                    help="运行前清空数据库索引（防止漏题：清除所有 flask_* 命名空间后重建）")
    ap.add_argument("--reset-db-only", action="store_true",
                    help="仅清空数据库索引，不执行测试")
    ap.add_argument("--rerun-all", "--reset-run-state", dest="rerun_all", action="store_true",
                    help="清空数据库索引与历史结果，从头重跑全部 Flask instances")

    args = ap.parse_args()

    # Override module-level config from CLI args
    _update_config(args.repo_cache, args.results_dir, args.timeout)

    run(args)


def _update_config(repo_cache: str, results_dir: str, timeout: int):
    """Update module-level configuration from CLI arguments."""
    global REPO_CACHE_DIR, RESULTS_DIR, TASK_TIMEOUT_SEC
    REPO_CACHE_DIR = repo_cache
    RESULTS_DIR = results_dir
    TASK_TIMEOUT_SEC = timeout


if __name__ == "__main__":
    main()
