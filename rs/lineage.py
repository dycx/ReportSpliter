"""报表血缘：发现 Hive 表/CSV 输出，构建列级血缘与报告注册表。

输入：项目的 .sql 文件 + 宿主代码（Scala/Java/Python）中嵌入的
spark.sql / saveAsTable / write.csv 等输出点。
输出：报告注册表（每个报告的上游表、列级血缘、定义锚点、git 变更历史）。
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import sqlglot
from sqlglot import exp


HOST_SUFFIXES = {".scala", ".java", ".py", ".sql"}

SQL_EMBED_RE = re.compile(
    r"(?:spark\.|sqlContext\.)?sql\s*\(\s*(?:\"\"\"|''')(.*?)(?:\"\"\"|''')",
    re.DOTALL,
)
SQL_EMBED_ONE_RE = re.compile(
    r"(?:spark\.|sqlContext\.)?sql\s*\(\s*\"([^\"]{10,})\"",
    re.DOTALL,
)
SAVE_AS_TABLE_RE = re.compile(r"\.saveAsTable\s*\(\s*['\"]([^'\"]+)")
INSERT_INTO_RE = re.compile(r"\.insertInto\s*\(\s*['\"]([^'\"]+)")
CSV_WRITE_RE = re.compile(r"\.write\b.{0,200}?\.csv\s*\(\s*['\"]([^'\"]+)")
TO_CSV_RE = re.compile(r"to_csv\s*\(\s*['\"]([^'\"]+)")
HOST_READ_RE = re.compile(
    r"(?:spark\.|sqlContext\.)?sql\s*\(\s*(?:\"\"\"|'''|)(.*?)(?:\"\"\"|'''|)\s*\)"
    r"|\.table\s*\(\s*['\"]([^'\"]+)",
    re.DOTALL,
)


def _line_of(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _extract_sql_assets(root: Path) -> List[dict]:
    """收集所有 SQL 片段：独立 .sql 文件 + 宿主代码嵌入字符串。"""
    assets = []
    for p in sorted(root.rglob("*")):
        if p.is_dir() or p.suffix.lower() not in HOST_SUFFIXES:
            continue
        rel = p.relative_to(root).as_posix()
        if any(seg in rel.split("/") for seg in
               (".git", "target", "build", "node_modules", "out", ".report-spliter", "generated")):
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if p.suffix.lower() == ".sql":
            assets.append({"file": rel, "line": 1, "sql": content, "snippet": content[:200]})
            continue
        # 嵌入字符串
        for m in SQL_EMBED_RE.finditer(content):
            assets.append({"file": rel, "line": _line_of(content, m.start()),
                           "sql": m.group(1).strip(), "snippet": content[m.start():m.start()+180]})
        for m in SQL_EMBED_ONE_RE.finditer(content):
            assets.append({"file": rel, "line": _line_of(content, m.start()),
                           "sql": m.group(1).strip(), "snippet": content[m.start():m.start()+180]})
    return assets


def _alias_map(select: exp.Select) -> Dict[str, str]:
    """FROM/JOIN 别名 → 真实表名。"""
    aliases: Dict[str, str] = {}
    for t in select.find_all(exp.Table):
        name = t.name
        full = f"{t.db}.{name}" if t.db else name
        aliases[t.alias or name] = full
    return aliases


def _cte_names(select: exp.Select) -> Set[str]:
    names = set()
    for cte in select.args.get("with", {}).get("expressions", []):
        if cte.alias:
            names.add(cte.alias)
    return names


def _output_columns(select: exp.Select) -> List[str]:
    if select is None:
        return []
    named = [a for a in select.named_selects if a]
    if named:
        return named
    return [e.sql()[:40] for e in select.expressions]


def _collect_column_sources(select: exp.Select, col: str) -> List[str]:
    """手动收集某输出列的上游列引用（含别名解析）。"""
    sources: List[str] = []
    aliases = _alias_map(select)
    cte = _cte_names(select)
    target_expr = None
    for e in select.selects:
        if getattr(e, "alias", None) == col:
            target_expr = e
            break
    if target_expr is None and len(select.expressions) == 1:
        target_expr = select.expressions[0]
    if target_expr is None:
        return sources
    for c in target_expr.find_all(exp.Column):
        t = c.table
        if t and t not in cte:
            t = aliases.get(t, t)
            sources.append(f"{t}.{c.name}")
        else:
            sources.append(c.name)
    return sorted(set(sources))


def _sqlglot_lineage_sources(sql: str, col: str, aliases: Optional[Dict[str, str]] = None) -> List[str]:
    """优先用 sqlglot.lineage；失败回退手动。"""
    aliases = aliases or {}
    try:
        from sqlglot.lineage import lineage
        node = lineage(column=col, sql=sql, dialect="spark")
        sources: Set[str] = set()
        stack = [node]
        while stack:
            n = stack.pop()
            if not n.downstream:
                src = getattr(n.source, "name", None)
                nm = n.name or ""
                if "." in nm:
                    a, rest = nm.split(".", 1)
                    sources.add(f"{aliases.get(a, a)}.{rest}")
                elif src:
                    sources.add(f"{aliases.get(src, src)}.{nm}")
                elif nm:
                    sources.add(nm)
            stack.extend(n.downstream)
        return sorted(s for s in sources if s)
    except Exception:
        return []


def _parse_sql_outputs(asset: dict) -> List[dict]:
    """解析一条 SQL，提取输出（Hive 表/视图）+ 列级血缘。"""
    outputs = []
    try:
        stmts = sqlglot.parse(asset["sql"], dialect="spark", read=None)
    except Exception:
        return outputs
    for stmt in stmts:
        if stmt is None:
            continue
        select = None
        table = None
        kind = "hive_table"
        if isinstance(stmt, exp.Insert):
            table = stmt.this
            select = stmt.expression
        elif isinstance(stmt, exp.Create) and getattr(stmt, "kind", "") in ("TABLE", "VIEW"):
            table = stmt.this
            select = stmt.expression
            kind = "view" if getattr(stmt, "kind", "") == "VIEW" else "hive_table"
        if table is None or select is None or not isinstance(select, exp.Select):
            continue
        tname = f"{table.db}.{table.name}" if table.db else table.name
        cols = _output_columns(select)
        cte = _cte_names(select)
        aliases = _alias_map(select)
        lineage = {}
        for col in cols:
            sources = _sqlglot_lineage_sources(asset["sql"], col, aliases) or \
                _collect_column_sources(select, col)
            lineage[col] = sources
        # 上游表：整条 select 里引用到的表（排除目标表与 CTE）
        upstream = set()
        for t in select.find_all(exp.Table):
            name = t.name
            if name in cte or name == tname.rsplit(".", 1)[-1]:
                continue
            upstream.add(aliases.get(name, name))
        upstream.discard(tname)
        outputs.append({
            "kind": kind,
            "output": tname,
            "select_sql": select.sql(pretty=True)[:3000],
            "columns": cols,
            "lineage": lineage,
            "upstream_tables": sorted(upstream),
        })
    return outputs


def _host_csv_outputs(root: Path) -> List[dict]:
    """宿主代码里的 CSV 输出点。"""
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_dir() or p.suffix.lower() not in (".scala", ".java", ".py"):
            continue
        rel = p.relative_to(root).as_posix()
        if any(seg in rel.split("/") for seg in
               (".git", "target", "build", "node_modules", "out", ".report-spliter")):
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for pat, kind in ((CSV_WRITE_RE, "csv"), (TO_CSV_RE, "csv")):
            for m in pat.finditer(content):
                path = m.group(1)
                line = _line_of(content, m.start())
                snippet = content[m.start():m.start() + 180].replace("\n", " ")
                out.append({
                    "kind": "csv",
                    "output": path,
                    "file": rel,
                    "line": line,
                    "snippet": snippet,
                    "host_upstream": _host_file_upstream(content, line),
                })
        for pat, kind in ((SAVE_AS_TABLE_RE, "hive_table"), (INSERT_INTO_RE, "hive_table")):
            for m in pat.finditer(content):
                table = m.group(1)
                line = _line_of(content, m.start())
                snippet = content[m.start():m.start() + 180].replace("\n", " ")
                out.append({
                    "kind": kind,
                    "output": table,
                    "file": rel,
                    "line": line,
                    "snippet": snippet,
                    "host_upstream": _host_file_upstream(content, line),
                })
    return out


def _host_file_upstream(content: str, line: int) -> List[str]:
    """近似上游：该文件此行之前的 spark.sql / table 读取。"""
    reads = []
    head = content.splitlines()[:line]
    head_text = "\n".join(head)
    for pat in (SQL_EMBED_RE, SQL_EMBED_ONE_RE):
        for m in pat.finditer(head_text):
            sql = m.group(1)
            try:
                for stmt in sqlglot.parse(sql, dialect="spark"):
                    for t in stmt.find_all(exp.Table):
                        reads.append(f"{t.db}.{t.name}" if t.db else t.name)
            except Exception:
                continue
    for m in re.finditer(r"\.table\s*\(\s*['\"]([^'\"]+)", head_text):
        reads.append(m.group(1))
    return sorted(set(reads))


def discover_reports(root: str | Path) -> List[dict]:
    """发现全部报告（Hive 表/视图 + CSV 文件）并合并定义点。"""
    root = Path(root)
    reports: Dict[Tuple[str, str], dict] = {}
    assets = _extract_sql_assets(root)
    for asset in assets:
        for o in _parse_sql_outputs(asset):
            key = (o["kind"], o["output"])
            r = reports.setdefault(key, {
                "name": o["output"],
                "kind": o["kind"],
                "output": o["output"],
                "definitions": [],
                "upstream_tables": set(),
                "lineage": {},
                "columns": [],
            })
            r["definitions"].append({
                "file": asset["file"], "line": asset["line"],
                "snippet": asset["snippet"][:200],
            })
            r["upstream_tables"] |= set(o["upstream_tables"])
            for col, srcs in o["lineage"].items():
                r["lineage"].setdefault(col, [])
                for s in srcs:
                    if s not in r["lineage"][col]:
                        r["lineage"][col].append(s)
            r["columns"] = sorted(set(r["columns"]) | set(o["columns"]))

    for o in _host_csv_outputs(root):
        name = Path(o["output"]).name.split(".")[0] or "csv-export"
        key = (o["kind"], f"{o['output']}@{o['file']}:{o['line']}")
        r = reports.setdefault(key, {
            "name": name,
            "kind": o["kind"],
            "output": o["output"],
            "definitions": [],
            "upstream_tables": set(o.get("host_upstream", [])),
            "lineage": {},
            "columns": [],
        })
        r["definitions"].append({
            "file": o["file"], "line": o["line"], "snippet": o["snippet"],
        })

    result = []
    for r in reports.values():
        r["upstream_tables"] = sorted(r["upstream_tables"])
        r["definitions"].sort(key=lambda d: (d["file"], d["line"]))
        r["producer_files"] = sorted({d["file"] for d in r["definitions"]})
        r["history"] = []
        result.append(r)
    result.sort(key=lambda r: (r["kind"], r["name"]))
    return result


def git_history(root: str | Path, files: List[str], limit: int = 10) -> List[dict]:
    """报告生产文件最近的 git 提交（文件级）。"""
    if not files:
        return []
    try:
        cmd = ["git", "-C", str(root), "log", "--date=short",
               f"--pretty=format:%h|%ad|%s", f"-n{limit}", "--"]
        cmd += list(files)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    if res.returncode != 0:
        return []
    commits = []
    for line in res.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return commits


def build_registry(root: str | Path, with_history: bool = False) -> dict:
    """构建报告注册表 + 报告依赖图 + 概览。"""
    reports = discover_reports(root)
    by_name = {r["name"]: r for r in reports}
    if with_history:
        for r in reports:
            r["history"] = git_history(root, r["producer_files"])

    # 报告依赖图：本报告的上游表里，哪些是其他报告/中间视图（按输出名匹配）
    outputs = {r["output"]: r["name"] for r in reports}
    deps = {}
    for r in reports:
        up = [outputs[t] for t in r["upstream_tables"] if t in outputs]
        deps[r["name"]] = up

    return {
        "schema": "report-registry/v1",
        "project_root": str(Path(root).resolve()),
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "reports": reports,
        "dependencies": deps,
        "stats": {
            "reports": len(reports),
            "hive_tables": sum(1 for r in reports if r["kind"] == "hive_table"),
            "views": sum(1 for r in reports if r["kind"] == "view"),
            "csv": sum(1 for r in reports if r["kind"] == "csv"),
        },
    }


def write_registry_md(path: Path, registry: dict) -> None:
    lines = ["# 报表血缘概览", "", f"项目：{registry['project_root']}", ""]
    st = registry["stats"]
    lines.append(f"Hive 表 {st['hive_tables']} / 视图 {st['views']} / CSV {st['csv']}，"
                 f"共 {st['reports']} 个报告", )
    lines.append("")

    if registry["dependencies"]:
        lines.append("## 报告依赖图")
        lines.append("")
        lines.append("```mermaid")
        lines.append("flowchart LR")
        for r, ups in registry["dependencies"].items():
            for u in ups:
                lines.append(f'  {r.replace(".", "_")}[{r}] --> {u.replace(".", "_")}[{u}]')
        lines.append("```")
        lines.append("")

    lines.append("## 报告明细")
    for r in registry["reports"]:
        lines.append(f"\n### {r['name']}（{r['kind']}）")
        lines.append(f"\n输出：`{r['output']}`")
        lines.append("\n定义位置：")
        for d in r["definitions"]:
            lines.append(f"- `{d['file']}:{d['line']}` — `{d['snippet'][:100]}`")
        lines.append(f"\n上游表：{', '.join(r['upstream_tables']) or '（无）'}")
        if r["lineage"]:
            lines.append("\n列级血缘：")
            for col, srcs in r["lineage"].items():
                lines.append(f"- `{col}` ← {', '.join(srcs) or '（未解析）'}")
        if r["history"]:
            lines.append("\n最近变更：")
            for h in r["history"][:5]:
                lines.append(f"- `{h['hash']}` {h['date']} {h['subject']}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
