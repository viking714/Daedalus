"""Neo4j 仓储（代码依赖关系图）。

支持单命名空间 per repo（对应详细设计 v2.1 §3.1）：
- 节点 `ns` 属性值为 repo 名，所有 commit 共享同一份图数据；
- Redis 追踪 file hash，repo_indexer 做增量更新（删除+新增变更文件）；
- 未指定 ns 时为空串（生产场景跟随 HEAD）。

节点：File / Function / Class
  Function/Class 除 fqn/name 外，冗余存 path/repo/ns，便于按路径过滤与按 (repo, path, name) 精确关联。
边：  File -[:DEFINES]-> Function/Class
      Function -[:CALLS]-> Function/Class（按符号名 + 同 repo 约束启发式关联）
      File -[:IMPORTS]-> File（按 import 模块名匹配路径片段）
      Class -[:HAS_METHOD]-> Function（按方法块的 parent_class 关联同类方法）
"""

from .base import DbUnavailable
from .config import get_config


class Neo4jStore:
    def __init__(self) -> None:
        self._driver = None

    def _connect(self):
        try:
            from neo4j import GraphDatabase
        except ImportError as e:
            raise DbUnavailable("neo4j 驱动未安装：pip install neo4j-driver") from e
        cfg = get_config()
        try:
            self._driver = GraphDatabase.driver(
                cfg["neo4j_uri"],
                auth=(cfg["neo4j_user"], cfg["neo4j_password"]),
                connection_timeout=5,
            )
            self._driver.verify_connectivity()
        except Exception as e:
            raise DbUnavailable(f"无法连接 Neo4j：{e}") from e

    def ping(self) -> bool:
        self._connect()
        self._driver.verify_connectivity()
        return True

    def ensure_schema(self) -> None:
        if self._driver is None:
            self._connect()
        with self._driver.session() as s:
            # 复合唯一约束：(path, ns) / (fqn, ns)——同一 commit 内路径/符号唯一，
            # 不同 commit（不同 ns）允许同名节点共存（对应设计文档 §7.6 commit-aware 隔离）。
            # 先删旧版单字段约束（若存在），再建复合约束。
            for old in ("file_path", "func_fqn", "class_fqn"):
                try:
                    s.run(f"DROP CONSTRAINT {old} IF EXISTS;")
                except Exception:  # noqa: BLE001
                    pass
            s.run("CREATE CONSTRAINT file_path_ns IF NOT EXISTS FOR (f:File) REQUIRE (f.path, f.ns) IS UNIQUE;")
            s.run("CREATE CONSTRAINT func_fqn_ns IF NOT EXISTS FOR (f:Function) REQUIRE (f.fqn, f.ns) IS UNIQUE;")
            s.run("CREATE CONSTRAINT class_fqn_ns IF NOT EXISTS FOR (c:Class) REQUIRE (c.fqn, c.ns) IS UNIQUE;")

    def upsert_symbol(self, chunk: dict, ns: str = "") -> None:
        if self._driver is None:
            self._connect()
        repo = chunk.get("repo")
        path = chunk["path"]
        symbol = chunk.get("symbol") or "<module>"
        kind = chunk.get("kind") or "module"
        fqn = chunk["chunk_id"]
        with self._driver.session() as s:
            if kind in ("function", "method"):
                s.run(
                    "MERGE (f:File {path:$path, ns:$ns}) SET f.repo=$repo "
                    "MERGE (fn:Function {fqn:$fqn, ns:$ns}) "
                    "SET fn.name=$symbol, fn.kind=$kind, fn.path=$path, fn.repo=$repo "
                    "MERGE (f)-[:DEFINES]->(fn)",
                    path=path, repo=repo, fqn=fqn, symbol=symbol, kind=kind, ns=ns,
                )
            elif kind == "class":
                s.run(
                    "MERGE (f:File {path:$path, ns:$ns}) SET f.repo=$repo "
                    "MERGE (c:Class {fqn:$fqn, ns:$ns}) "
                    "SET c.name=$symbol, c.path=$path, c.repo=$repo "
                    "MERGE (f)-[:DEFINES]->(c)",
                    path=path, repo=repo, fqn=fqn, symbol=symbol, ns=ns,
                )
            else:
                s.run("MERGE (f:File {path:$path, ns:$ns}) SET f.repo=$repo", path=path, repo=repo, ns=ns)

    def batch_upsert_symbols(self, chunks: list, ns: str = "") -> None:
        """批量写入符号节点（一次 UNWIND 往返，避免逐条 N+1 开销）。"""
        if not chunks:
            return
        if self._driver is None:
            self._connect()
        rows = []
        for c in chunks:
            rows.append({
                "path": c["path"],
                "repo": c.get("repo"),
                "fqn": c["chunk_id"],
                "symbol": c.get("symbol") or "<module>",
                "kind": c.get("kind") or "module",
                "ns": ns,
            })
        with self._driver.session() as s:
            # File 节点（去重）
            s.run(
                """
                UNWIND $rows AS row
                MERGE (f:File {path:row.path, ns:row.ns})
                SET f.repo = coalesce(row.repo, f.repo)
                """,
                rows=rows,
            )
            # Function / Method 节点
            func_rows = [r for r in rows if r["kind"] in ("function", "method")]
            if func_rows:
                s.run(
                    """
                    UNWIND $rows AS row
                    MERGE (fn:Function {fqn:row.fqn, ns:row.ns})
                    SET fn.name=row.symbol, fn.kind=row.kind, fn.path=row.path, fn.repo=row.repo
                    WITH fn, row
                    MATCH (f:File {path:row.path, ns:row.ns})
                    MERGE (f)-[:DEFINES]->(fn)
                    """,
                    rows=func_rows,
                )
            # Class 节点
            cls_rows = [r for r in rows if r["kind"] == "class"]
            if cls_rows:
                s.run(
                    """
                    UNWIND $rows AS row
                    MERGE (c:Class {fqn:row.fqn, ns:row.ns})
                    SET c.name=row.symbol, c.path=row.path, c.repo=row.repo
                    WITH c, row
                    MATCH (f:File {path:row.path, ns:row.ns})
                    MERGE (f)-[:DEFINES]->(c)
                    """,
                    rows=cls_rows,
                )

    def link_calls(self, chunk: dict) -> None:
        """为一个函数/方法块建 CALLS 边（按被调符号名匹配 + 同 repo + 同 ns 约束，同名目标最多连 3 个防误连爆炸）。"""
        calls = chunk.get("calls") or []
        if not calls or chunk.get("kind") not in ("function", "method"):
            return
        if self._driver is None:
            self._connect()
        ns = chunk.get("ns", "")
        with self._driver.session() as s:
            s.run(
                """
                MATCH (caller:Function {fqn:$fqn, ns:$ns})
                UNWIND $names AS name
                CALL {
                    WITH name, $repo, $ns
                    MATCH (t) WHERE (t:Function OR t:Class) AND t.name = name AND t.repo = $repo AND t.ns = $ns
                    RETURN t LIMIT 3
                }
                MERGE (caller)-[:CALLS]->(t)
                """,
                fqn=chunk["chunk_id"], names=calls, repo=chunk.get("repo"), ns=ns,
            )

    def link_methods(self, chunk: dict) -> None:
        """为方法块建 HAS_METHOD 边：按 (path, repo, ns, name) 定位所属类，连 Class-[:HAS_METHOD]->Function。"""
        parent = chunk.get("parent_class")
        if not parent or chunk.get("kind") != "method":
            return
        if self._driver is None:
            self._connect()
        ns = chunk.get("ns", "")
        with self._driver.session() as s:
            s.run(
                """
                MATCH (c:Class) WHERE c.path=$path AND c.repo=$repo AND c.name=$parent AND c.ns=$ns
                MATCH (m:Function {fqn:$fqn, ns:$ns})
                MERGE (c)-[:HAS_METHOD]->(m)
                """,
                path=chunk["path"], repo=chunk.get("repo"),
                parent=parent, fqn=chunk["chunk_id"], ns=ns,
            )

    def link_imports(self, path: str, modules: list, ns: str = "") -> None:
        """文件级 IMPORTS 边：模块名转路径片段（a.b.c -> /a/b/c.），按片段匹配已索引文件（同 ns）。"""
        if not modules:
            return
        if self._driver is None:
            self._connect()
        frags = ["/" + m + "." for m in modules] + ["/" + m.split("/")[-1] + "." for m in modules]
        with self._driver.session() as s:
            s.run(
                """
                MATCH (src:File {path:$path, ns:$ns})
                UNWIND $frags AS frag
                CALL {
                    WITH frag, $ns
                    MATCH (t:File) WHERE t.path CONTAINS frag AND t.ns = $ns
                    RETURN t LIMIT 2
                }
                WITH src, t WHERE t.path <> $path
                MERGE (src)-[:IMPORTS]->(t)
                """,
                path=path, frags=list(dict.fromkeys(frags)), ns=ns,
            )

    # ---------- 批量建边（性能优化：用单次 UNWIND 替代逐块 N+1 往返）----------

    def batch_link_calls(self, rows: list) -> None:
        """rows: [(fqn, names, repo, ns), ...] 批量建 CALLS 边（同 repo + 同 ns 约束 + 同名最多连 3）。"""
        rows = [{"fqn": f, "names": n, "repo": r, "ns": ns or ""}
                for f, n, r, *rest in rows if n for ns in [rest[0] if rest else ""]]
        if not rows:
            return
        if self._driver is None:
            self._connect()
        with self._driver.session() as s:
            s.run(
                """
                UNWIND $rows AS row
                MATCH (caller:Function {fqn:row.fqn, ns:row.ns})
                UNWIND row.names AS name
                CALL {
                    WITH name, row
                    MATCH (t) WHERE (t:Function OR t:Class) AND t.name = name AND t.repo = row.repo AND t.ns = row.ns
                    RETURN t LIMIT 3
                }
                MERGE (caller)-[:CALLS]->(t)
                """,
                rows=rows,
            )

    def batch_link_methods(self, rows: list) -> None:
        """rows: [(fqn, parent, path, repo, ns), ...] 批量建 HAS_METHOD 边。"""
        rows = [{"fqn": f, "parent": p, "path": pa, "repo": r, "ns": ns or ""}
                for f, p, pa, r, *rest in rows if p for ns in [rest[0] if rest else ""]]
        if not rows:
            return
        if self._driver is None:
            self._connect()
        with self._driver.session() as s:
            s.run(
                """
                UNWIND $rows AS row
                MATCH (c:Class) WHERE c.path=row.path AND c.repo=row.repo AND c.name=row.parent AND c.ns=row.ns
                MATCH (m:Function {fqn:row.fqn, ns:row.ns})
                MERGE (c)-[:HAS_METHOD]->(m)
                """,
                rows=rows,
            )

    def batch_link_imports(self, rows: list) -> None:
        """rows: [(path, modules, ns), ...] 批量建 IMPORTS 边（每个文件只处理一次，同 ns 过滤）。"""
        data = []
        for row in rows:
            path, mods = row[0], row[1]
            ns = row[2] if len(row) > 2 else ""
            if not mods:
                continue
            frags = ["/" + m + "." for m in mods] + ["/" + m.split("/")[-1] + "." for m in mods]
            frags = list(dict.fromkeys(frags))
            data.append({"path": path, "frags": frags, "ns": ns})
        if not data:
            return
        if self._driver is None:
            self._connect()
        with self._driver.session() as s:
            s.run(
                """
                UNWIND $rows AS row
                MATCH (src:File {path:row.path, ns:row.ns})
                UNWIND row.frags AS frag
                CALL {
                    WITH frag, row
                    MATCH (t:File) WHERE t.path CONTAINS frag AND t.ns = row.ns
                    RETURN t LIMIT 2
                }
                WITH src, t WHERE t.path <> row.path
                MERGE (src)-[:IMPORTS]->(t)
                """,
                rows=data,
            )

    def impact_stats(self, paths: list, ns: str = "") -> dict:
        """影响面统计：被改文件所定义符号的真实调用方数，以及跨文件 import 数（同 ns 过滤）。
        用 ENDS WITH 宽松匹配路径，容忍 diff 路径与索引路径的小差异。"""
        if not paths:
            return {"direct_callers": 0, "imported_files": 0}
        if self._driver is None:
            self._connect()
        ns_filter = "AND f.ns = $ns" if ns else ""
        with self._driver.session() as s:
            rec = s.run(
                f"""
                MATCH (f:File) WHERE ANY(p IN $paths WHERE f.path ENDS WITH p) {ns_filter}
                MATCH (f)-[:DEFINES]->(fn:Function)
                OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
                RETURN count(DISTINCT caller) AS direct_callers
                """,
                paths=list(paths), ns=ns,
            ).single()
            direct = rec["direct_callers"] if rec else 0
            rec2 = s.run(
                f"""
                MATCH (f:File) WHERE ANY(p IN $paths WHERE f.path ENDS WITH p) {ns_filter}
                MATCH (f)-[:IMPORTS]->(g:File)
                WHERE g.path <> f.path
                RETURN count(DISTINCT g) AS imported_files
                """,
                paths=list(paths), ns=ns,
            ).single()
            imported = rec2["imported_files"] if rec2 else 0
        return {"direct_callers": direct, "imported_files": imported}

    def has_namespace_data(self, ns: str = "") -> bool:
        """检查命名空间下是否已有节点。"""
        if self._driver is None:
            self._connect()
        ns_filter = "WHERE n.ns = $ns" if ns else ""
        with self._driver.session() as s:
            rec = s.run(
                f"""
                MATCH (n) {ns_filter}
                RETURN count(n) > 0 AS exists
                """,
                ns=ns,
            ).single()
        return bool(rec["exists"]) if rec else False

    def expand_chunks(self, seed_fqns: list, limit: int = 10, exclude=None, ns: str = "") -> list:
        """检索后图谱扩充：优先沿 CALLS（被调方/调用方），不足再补 IMPORTS 文件里的符号。

        返回 [{"chunk_id", "relation", "via"}]，relation ∈ callee/caller/imported_symbol。
        同 ns 过滤防止跨 commit 数据混入。
        """
        if self._driver is None:
            self._connect()
        exclude = set(exclude or ()) | set(seed_fqns)
        out, seen = [], set()
        ns_filter = "AND n.ns = $ns" if ns else ""
        ns_filter_f = "AND f.ns = $ns" if ns else ""

        def _collect(records):
            for r in records:
                fqn = r["fqn"]
                if fqn and fqn not in exclude and fqn not in seen:
                    seen.add(fqn)
                    out.append({"chunk_id": fqn, "relation": r["rel"], "via": r["via"]})

        # 测试/示例/规格路径过滤：扩充结果不应混入测试代码
        _not_test = (
            "NOT (n.path CONTAINS '/tests/' OR n.path CONTAINS '/test/' "
            "OR n.path CONTAINS '/spec/' OR n.path CONTAINS '/examples/' "
            "OR (n.path CONTAINS '/test_' AND n.path ENDS WITH '.py') "
            "OR (n.path CONTAINS '/spec_' AND n.path ENDS WITH '.py'))"
        )
        with self._driver.session() as s:
            # 第一优先级：调用关系（语义最强）
            _collect(s.run(
                f"""
                MATCH (seed) WHERE seed.fqn IN $seeds {ns_filter.replace('n.', 'seed.')}
                CALL {{
                    WITH seed, $ns
                    MATCH (seed)-[:CALLS]->(n) WHERE n.fqn IS NOT NULL AND {_not_test} {ns_filter}
                    RETURN n.fqn AS fqn, 'callee' AS rel, seed.name AS via
                    UNION
                    WITH seed, $ns
                    MATCH (n:Function)-[:CALLS]->(seed) WHERE {_not_test} {ns_filter}
                    RETURN n.fqn AS fqn, 'caller' AS rel, seed.name AS via
                }}
                RETURN DISTINCT fqn, rel, via LIMIT $k
                """,
                seeds=seed_fqns, k=limit * 2, ns=ns,
            ))
            # 第二优先级：同类兄弟方法（类内方法独立成块后，命中其一应拉入同类的其他强相关方法）
            remain = limit - len(out)
            if remain > 0:
                _collect(s.run(
                    f"""
                    MATCH (seed) WHERE seed.fqn IN $seeds {ns_filter.replace('n.', 'seed.')}
                    MATCH (c:Class)-[:HAS_METHOD]->(seed)
                    MATCH (c)-[:HAS_METHOD]->(sib:Function)
                    WHERE sib <> seed AND {_not_test} {ns_filter}
                    RETURN DISTINCT sib.fqn AS fqn, 'sibling_method' AS rel, c.name AS via
                    LIMIT $k
                    """,
                    seeds=seed_fqns, k=remain, ns=ns,
                ))
            # 第三优先级：种子所在文件 import 的文件中定义的符号（补足名额）
            remain = limit - len(out)
            if remain > 0:
                _collect(s.run(
                    f"""
                    MATCH (f:File)-[:DEFINES]->(seed) WHERE seed.fqn IN $seeds {ns_filter_f.replace('f.', 'seed.')}
                    MATCH (f)-[:IMPORTS]->(g:File)-[:DEFINES]->(n)
                    WHERE {_not_test} {ns_filter}
                    RETURN DISTINCT n.fqn AS fqn, 'imported_symbol' AS rel, g.path AS via
                    LIMIT $k
                    """,
                    seeds=seed_fqns, k=remain, ns=ns,
                ))
        return out[:limit]

    def symbol_lookup(self, query: str, top_k: int = 10, ns: str = "") -> list:
        if self._driver is None:
            self._connect()
        ns_filter = "AND fn.ns = $ns" if ns else ""
        with self._driver.session() as s:
            recs = s.run(
                f"MATCH (fn:Function) WHERE fn.name CONTAINS $q {ns_filter} RETURN fn.fqn AS fqn LIMIT $k",
                q=query, k=top_k, ns=ns,
            )
            return [{"chunk_id": r["fqn"]} for r in recs]

    def dependency_subgraph(self, seed_fqns: list, depth: int = 2, ns: str = "") -> dict:
        if self._driver is None:
            self._connect()
        ns_filter = "AND seed.ns = $ns" if ns else ""
        with self._driver.session() as s:
            rec = s.run(
                f"""
                MATCH (seed:Function)
                WHERE seed.fqn IN $seeds {ns_filter}
                CALL {{
                    WITH seed
                    MATCH path = (seed)-[:CALLS*1..{depth}]->(callee:Function)
                    RETURN callee
                }}
                RETURN collect(DISTINCT callee.fqn) AS callers
                """,
                seeds=seed_fqns, ns=ns,
            ).single()
            callers = rec["callers"] if rec else []
        return {"seed_fqns": seed_fqns, "direct_callers": callers, "depth": depth}

    # ------------------------------------------------------------------ #
    # 增量索引：按文件删除
    # ------------------------------------------------------------------ #

    def delete_by_paths(self, paths: list, ns: str = "") -> int:
        """删除指定文件路径的节点及其关系（增量更新时清理旧数据）。返回删除节点数。"""
        if not paths:
            return 0
        if self._driver is None:
            self._connect()
        ns_filter = "AND f.ns = $ns" if ns else ""
        with self._driver.session() as s:
            # 删除文件节点及其关联的 Function/Class 节点
            result = s.run(
                f"""
                MATCH (f:File) WHERE f.path IN $paths {ns_filter}
                OPTIONAL MATCH (f)-[:DEFINES]->(sym)
                WITH f, sym
                DETACH DELETE f, sym
                RETURN count(DISTINCT f) AS deleted
                """,
                paths=list(paths), ns=ns,
            )
            rec = result.single()
            return rec["deleted"] if rec else 0

    def delete_all(self, ns: str = "") -> int:
        """删除整个命名空间的所有节点和关系（reset 时使用）。返回删除节点数。"""
        if self._driver is None:
            self._connect()
        ns_filter = "WHERE n.ns = $ns" if ns else ""
        with self._driver.session() as s:
            result = s.run(
                f"""
                MATCH (n) {ns_filter}
                WITH count(n) AS total
                CALL {{
                    MATCH (n) {ns_filter}
                    DETACH DELETE n
                }}
                RETURN total
                """,
                ns=ns,
            )
            rec = result.single()
            return rec["total"] if rec else 0
