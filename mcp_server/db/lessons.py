"""经验库（lessons）仓储 —— 对齐方案设计 §5「经验沉淀闭环」。

经验沉淀闭环的数据层：管理 `lessons` 表（PostgreSQL + pgvector），
提供三个核心能力：
- ensure_schema()：幂等建表 + HNSW 向量索引
- search_similar()：按根因向量语义检索相似经验（按 repo 隔离）
- upsert_with_dedup()：写入前去重 + 合并（MERGE / SIMILAR / NEW 三级决策）

去重与合并规则（对齐方案设计 §5.3）：
- 相似度阈值：score >= 0.95 → MERGE；[0.85, 0.95) → SIMILAR；< 0.85 → NEW
- 合并规则：
    * affected_modules / edge_cases / tags：取并集（历史踩坑不丢失）
    * success：旧 OR 新（至少一次成功即标记成功）
    * diff_summary：保留更详细的（长度更长者）
    * merge_count：+1（被独立验证次数）
    * retry_count：取 min（保留最优轮次）
- 其余文本字段（fix_pattern / error_signature / fix_strategy / test_changes /
  resolution_summary）：新值非空则覆盖旧值（新信息优先）。

本模块与 PgVectorStore 一样懒加载 psycopg2，不可用时抛 DbUnavailable，
由上层（Skill 脚本 / 组合工具）捕获并返回结构化降级结果。
"""

import logging
import uuid

from .base import DbUnavailable
from .config import get_config

logger = logging.getLogger("lessons")

# 去重相似度阈值（设计 §5.3）
MERGE_THRESHOLD = 0.95
SIMILAR_THRESHOLD = 0.85

# lessons 表列（与 SELECT 顺序一致，供行转换复用）
_LESSON_COLS = (
    "id, repo, task_id, root_cause, fix_pattern, error_signature, fix_strategy, "
    "affected_modules, tags, diff_summary, test_changes, edge_cases, success, "
    "resolution_summary, retry_count, merge_count, related_to"
)


def _rand_id(prefix: str = "KM") -> str:
    """生成经验记录 ID（如 KM-xxxxxxxx）。"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _row_to_lesson(row, with_score: bool = False) -> dict:
    """将数据库行转换为 lesson dict（字段顺序与 _LESSON_COLS 一致）。"""
    lesson = {
        "id": row[0],
        "repo": row[1],
        "task_id": row[2],
        "root_cause": row[3],
        "fix_pattern": row[4],
        "error_signature": row[5],
        "fix_strategy": row[6],
        "affected_modules": row[7] or [],
        "tags": row[8] or [],
        "diff_summary": row[9],
        "test_changes": row[10],
        "edge_cases": row[11] or [],
        "success": bool(row[12]),
        "resolution_summary": row[13],
        "retry_count": row[14] or 0,
        "merge_count": row[15] or 1,
        "related_to": row[16],
    }
    if with_score and len(row) > 17:
        lesson["score"] = round(row[17], 4)
    return lesson


class LessonsStore:
    """经验库数据访问（Repository）。"""

    def __init__(self) -> None:
        self._conn = None

    def _connect(self):
        try:
            import psycopg2
        except ImportError as e:  # 未安装客户端
            raise DbUnavailable("psycopg2 未安装：pip install psycopg2-binary") from e
        cfg = get_config()
        try:
            self._conn = psycopg2.connect(
                host=cfg["pghost"],
                port=cfg["pgport"],
                dbname=cfg["pgdb"],
                user=cfg["pguser"],
                password=cfg["pgpassword"],
                connect_timeout=5,
            )
        except Exception as e:  # 连接失败（隧道未开 / 库未起）
            raise DbUnavailable(f"无法连接 PostgreSQL：{e}") from e

    def _cur(self):
        if self._conn is None or self._conn.closed:
            self._connect()
        return self._conn.cursor()

    def ensure_schema(self) -> None:
        """幂等建表 + 可选 HNSW 向量索引。"""
        dim = get_config()["embed_dim"]
        cur = self._cur()
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS lessons (
                    id                TEXT PRIMARY KEY,
                    repo              TEXT NOT NULL,
                    task_id           TEXT,
                    root_cause        TEXT NOT NULL,
                    root_cause_vec    vector({dim}),
                    fix_pattern       TEXT,
                    error_signature   TEXT,
                    fix_strategy      TEXT,
                    affected_modules  TEXT[],
                    tags              TEXT[],
                    diff_summary      TEXT,
                    test_changes      TEXT,
                    edge_cases        TEXT[],
                    success           BOOLEAN NOT NULL,
                    resolution_summary TEXT,
                    retry_count       INTEGER DEFAULT 0,
                    merge_count       INTEGER DEFAULT 1,
                    related_to        TEXT,
                    created_at        TIMESTAMP DEFAULT now()
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS lessons_repo_idx ON lessons (repo);"
            )
            self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            raise DbUnavailable(f"初始化 lessons schema 失败：{e}") from e
        # HNSW 索引为可选加速：旧版 pgvector 不支持时降级跳过。
        try:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS lessons_root_cause_vec_idx "
                "ON lessons USING hnsw (root_cause_vec vector_cosine_ops);"
            )
            self._conn.commit()
        except Exception as e:  # noqa: BLE001
            self._conn.rollback()
            logger.warning("lessons HNSW 索引不可用（%s），降级为无索引顺序扫描。", e)

    def search_similar(
        self,
        query_vec,
        repo: str = "",
        top_k: int = 5,
        success_only: bool = False,
    ) -> list:
        """按根因向量语义检索相似经验，返回含 score 的记录列表。

        Args:
            query_vec: 查询向量（root_cause 或 fix_pattern 的 embedding）
            repo: 仓库过滤（空串则不限仓库）
            top_k: 返回条数
            success_only: 仅返回成功经验（Fixer 查询模式为 True）
        """
        vec_str = str(list(query_vec))
        where = []
        params: list = []
        if repo:
            where.append("repo = %s")
            params.append(repo)
        if success_only:
            where.append("success = true")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params += [vec_str, vec_str, top_k]
        cur = self._cur()
        cur.execute(
            f"""
            SELECT {_LESSON_COLS},
                   1 - (root_cause_vec <=> %s::vector) AS score
            FROM lessons
            {where_sql}
            ORDER BY root_cause_vec <=> %s::vector
            LIMIT %s;
            """,
            params,
        )
        return [_row_to_lesson(r, with_score=True) for r in cur.fetchall()]

    def upsert_with_dedup(self, lesson: dict, root_cause_vec) -> dict:
        """写入前去重 + 合并，返回决策结果。

        Args:
            lesson: 完整 lesson 字段 dict（不含 id / root_cause_vec）
            root_cause_vec: 根因 embedding

        Returns:
            {
                decision: "MERGE" | "SIMILAR" | "NEW",
                lesson_id: str,
                score: float | None,       # MERGE/SIMILAR 时命中相似记录的分数
                related_to: str | None,    # SIMILAR 时关联的相似 lesson ID
            }
        """
        repo = lesson.get("repo") or ""
        top = self.search_similar(root_cause_vec, repo=repo, top_k=1)

        if top and top[0]["score"] >= MERGE_THRESHOLD:
            self._merge(top[0], lesson, root_cause_vec)
            return {
                "decision": "MERGE",
                "lesson_id": top[0]["id"],
                "score": top[0]["score"],
                "related_to": None,
            }
        if top and top[0]["score"] >= SIMILAR_THRESHOLD:
            lesson["related_to"] = top[0]["id"]
            new_id = self._insert(lesson, root_cause_vec)
            return {
                "decision": "SIMILAR",
                "lesson_id": new_id,
                "score": top[0]["score"],
                "related_to": top[0]["id"],
            }
        new_id = self._insert(lesson, root_cause_vec)
        return {
            "decision": "NEW",
            "lesson_id": new_id,
            "score": None,
            "related_to": None,
        }

    # ------------------------------------------------------------------ #
    # 内部：插入 / 合并
    # ------------------------------------------------------------------ #

    def _insert(self, lesson: dict, root_cause_vec) -> str:
        lesson_id = lesson.get("id") or _rand_id()
        cur = self._cur()
        cur.execute(
            """
            INSERT INTO lessons (
                id, repo, task_id, root_cause, root_cause_vec, fix_pattern,
                error_signature, fix_strategy, affected_modules, tags, diff_summary,
                test_changes, edge_cases, success, resolution_summary, retry_count,
                merge_count, related_to
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            );
            """,
            (
                lesson_id,
                lesson.get("repo") or "",
                lesson.get("task_id"),
                lesson.get("root_cause") or "",
                str(list(root_cause_vec)),
                lesson.get("fix_pattern"),
                lesson.get("error_signature"),
                lesson.get("fix_strategy"),
                list(lesson.get("affected_modules") or []),
                list(lesson.get("tags") or []),
                lesson.get("diff_summary"),
                lesson.get("test_changes"),
                list(lesson.get("edge_cases") or []),
                bool(lesson.get("success", False)),
                lesson.get("resolution_summary"),
                int(lesson.get("retry_count") or 0),
                int(lesson.get("merge_count") or 1),
                lesson.get("related_to"),
            ),
        )
        self._conn.commit()
        return lesson_id

    def _merge(self, existing: dict, incoming: dict, root_cause_vec) -> None:
        """将新经验合并进已有记录（设计 §5.3 合并规则）。"""
        def _union(old, new):
            return list(dict.fromkeys((old or []) + (new or [])))

        merged = {
            "id": existing["id"],
            "repo": existing["repo"] or incoming.get("repo") or "",
            "task_id": incoming.get("task_id") or existing.get("task_id"),
            "root_cause": existing["root_cause"] or incoming.get("root_cause") or "",
            "fix_pattern": incoming.get("fix_pattern") or existing.get("fix_pattern"),
            "error_signature": incoming.get("error_signature") or existing.get("error_signature"),
            "fix_strategy": incoming.get("fix_strategy") or existing.get("fix_strategy"),
            "affected_modules": _union(existing.get("affected_modules"), incoming.get("affected_modules")),
            "tags": _union(existing.get("tags"), incoming.get("tags")),
            "diff_summary": _longer(existing.get("diff_summary"), incoming.get("diff_summary")),
            "test_changes": incoming.get("test_changes") or existing.get("test_changes"),
            "edge_cases": _union(existing.get("edge_cases"), incoming.get("edge_cases")),
            "success": bool(existing.get("success")) or bool(incoming.get("success")),
            "resolution_summary": incoming.get("resolution_summary") or existing.get("resolution_summary"),
            "retry_count": _min_or_zero(existing.get("retry_count"), incoming.get("retry_count")),
            "merge_count": int(existing.get("merge_count") or 1) + 1,
            "related_to": existing.get("related_to"),
        }
        cur = self._cur()
        cur.execute(
            """
            UPDATE lessons SET
                repo=%s, task_id=%s, root_cause=%s, root_cause_vec=%s, fix_pattern=%s,
                error_signature=%s, fix_strategy=%s, affected_modules=%s, tags=%s,
                diff_summary=%s, test_changes=%s, edge_cases=%s, success=%s,
                resolution_summary=%s, retry_count=%s, merge_count=%s, related_to=%s
            WHERE id=%s;
            """,
            (
                merged["repo"],
                merged["task_id"],
                merged["root_cause"],
                str(list(root_cause_vec)),
                merged["fix_pattern"],
                merged["error_signature"],
                merged["fix_strategy"],
                list(merged["affected_modules"]),
                list(merged["tags"]),
                merged["diff_summary"],
                merged["test_changes"],
                list(merged["edge_cases"]),
                merged["success"],
                merged["resolution_summary"],
                merged["retry_count"],
                merged["merge_count"],
                merged["related_to"],
                merged["id"],
            ),
        )
        self._conn.commit()


def _longer(a, b):
    """保留更详细的文本（长度更长者）。"""
    a, b = a or "", b or ""
    return a if len(a) >= len(b) else b


def _min_or_zero(a, b):
    """取 min，但两者都为空时返回 0。"""
    vals = [v for v in (a, b) if v is not None]
    if not vals:
        return 0
    return min(vals)
