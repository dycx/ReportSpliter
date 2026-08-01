"""代码 → 算法重建：从模块闭包中还原每个入口的算法步骤（带代码锚点）。"""

from __future__ import annotations

import datetime
import json
import re
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from rs.indexer import ProjectIndex
from rs.models import EDGE_CALL, SCHEMA_ALGORITHM, ModuleGraph


READ_HINTS = ("get", "find", "load", "query", "read", "select", "fetch", "search",
              "count", "list", "parse", "lookup", "retrieve")
WRITE_HINTS = ("save", "write", "export", "render", "create", "insert", "update",
               "delete", "send", "publish", "record", "persist", "commit", "put",
               "post", "store", "append")
_ACCESSOR_RE = re.compile(r"^(get|set|is|has)[A-Z]")


def classify_step(name: str) -> str:
    if _ACCESSOR_RE.match(name):
        return "detail"
    low = name.lower()

    def hit(hints) -> bool:
        for h in hints:
            if len(h) <= 3:
                if re.search(rf"(?<![a-z0-9]){re.escape(h)}(?![a-z0-9])", low):
                    return True
            elif h in low:
                return True
        return False

    if hit(READ_HINTS):
        return "read"
    if hit(WRITE_HINTS):
        return "output"
    return "transform"


def _snippet(project_root: Path, file: str, line: int) -> str:
    try:
        lines = (project_root / file).read_text(encoding="utf-8", errors="replace").splitlines()
        if 1 <= line <= len(lines):
            return lines[line - 1].strip()
    except Exception:
        pass
    return ""


def _call_args_from_snippet(snippet: str, callee_simple: str) -> List[str]:
    """从源码行粗略提取调用实参（归一为简单标识符）。"""
    if not snippet:
        return []
    pattern = rf"{re.escape(callee_simple)}\s*\((.*?)\)"
    matches = re.findall(pattern, snippet)
    if not matches:
        return []
    args = []
    for a in matches[-1].split(","):
        a = a.strip()
        if not a:
            continue
        if a.startswith('"') or a.startswith("'"):
            continue
        # 表达式取首个标识符（接收者/变量名），如 user.getId() → user
        a = re.split(r"[.\s(]", a)[0]
        if a and a not in ("this", "new", "null", "true", "false"):
            args.append(a)
    return args


def _call_adjacency(index: ProjectIndex, graph: ModuleGraph) -> Dict[str, List[Tuple[str, int]]]:
    """闭包内的调用邻接表：src_symbol -> [(dst_symbol_id, line)]（去重）。"""
    adj: Dict[str, List[Tuple[str, int]]] = {}
    seen: Set[Tuple[str, str, int]] = set()
    for e in index.edges:
        if e.kind != EDGE_CALL or not e.resolved or e.external or e.src not in graph.symbols:
            continue
        ids = index.resolve_qname(e.dst) if e.dst not in index.symbols else [e.dst]
        for i in ids:
            if i in graph.symbols and (e.src, i, e.line) not in seen:
                seen.add((e.src, i, e.line))
                adj.setdefault(e.src, []).append((i, e.line))
    return adj


def _entry_steps(index: ProjectIndex, graph: ModuleGraph, project_root: Path,
                 entry_symbol_id: str, adj: Dict[str, List[Tuple[str, int]]]) -> List[dict]:
    """按入口 BFS 收集调用步骤。"""
    steps: List[dict] = []
    visited: Set[str] = {entry_symbol_id}
    q: deque = deque([entry_symbol_id])
    step_no = 0
    while q:
        cur = q.popleft()
        for dst_id, line in sorted(adj.get(cur, []), key=lambda x: x[1]):
            if dst_id in visited:
                continue
            visited.add(dst_id)
            dst = index.symbols[dst_id]
            cur_sym = index.symbols[cur]
            step_no += 1
            snippet = _snippet(project_root, cur_sym.file, line)
            inputs = _call_args_from_snippet(snippet, dst.simple)
            if not inputs:
                inputs = [p.get("name", "") for p in dst.params if p.get("name")]
            outputs = [dst.return_type] if dst.return_type and dst.return_type not in ("void", "") else []
            steps.append({
                "id": f"step-{step_no}",
                "kind": classify_step(dst.simple),
                "label": dst.simple,
                "callee": dst.qname,
                "caller": cur_sym.qname,
                "file": cur_sym.file,
                "line": line,
                "snippet": snippet,
                "inputs": inputs,
                "outputs": outputs,
                "confidence": 0.9,
            })
            q.append(dst_id)
    return steps


def reconstruct_algorithm(index: ProjectIndex, graph: ModuleGraph,
                          project_root: str | Path) -> dict:
    """重建模块算法：每个入口一个步骤序列。"""
    project_root = Path(project_root)
    adj = _call_adjacency(index, graph)
    entries_out = []

    for ep in graph.entries:
        ep_sym = index.symbols.get(ep.get("symbol", ""))
        if ep_sym is None:
            continue
        entry_step = {
            "id": "entry",
            "kind": "entry",
            "label": ep.get("label") or ep.get("symbol", ""),
            "callee": ep_sym.qname,
            "caller": "",
            "file": ep_sym.file,
            "line": ep_sym.start_line,
            "snippet": ep_sym.signature,
            "inputs": [p.get("name", "") for p in ep_sym.params if p.get("name")],
            "outputs": [ep_sym.return_type] if ep_sym.return_type and ep_sym.return_type != "void" else [],
            "confidence": ep.get("confidence", 1.0),
        }
        steps = _entry_steps(index, graph, project_root, ep["symbol"], adj)

        # 未解析的外部调用（框架/继承方法）作为 external 步骤
        caller_ids = {s["caller"] for s in steps}
        ext_steps = []
        for u in graph.unresolved_calls:
            u_sym = index.symbols.get(u.get("caller", ""))
            if u_sym is None:
                continue
            ext_steps.append({
                "id": f"ext-{u.get('file')}:{u.get('line')}",
                "kind": "external",
                "label": f"{u.get('callee')}（未解析）",
                "callee": u.get("callee", ""),
                "caller": u.get("caller", ""),
                "file": u.get("file", ""),
                "line": u.get("line", 0),
                "snippet": _snippet(project_root, u.get("file", ""), u.get("line", 0)),
                "inputs": [],
                "outputs": [],
                "confidence": 0.4,
            })
        steps.extend(ext_steps)

        entries_out.append({
            "entry": ep,
            "steps": [entry_step] + steps,
        })

    return {
        "schema": SCHEMA_ALGORITHM,
        "module": graph.name,
        "description": graph.description,
        "project_root": str(project_root),
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "entries": entries_out,
        "stats": {
            "entries": len(entries_out),
            "steps": sum(len(e["steps"]) for e in entries_out),
        },
    }


def flatten_algorithm_steps(algorithm: dict) -> List[dict]:
    """把多入口的步骤按 (callee, file, line) 去重合并成模块级步骤列表。"""
    seen: Set[Tuple[str, str, int]] = set()
    out: List[dict] = []
    for e in algorithm["entries"]:
        for s in e["steps"]:
            key = (s.get("callee", ""), s.get("file", ""), s.get("line", 0))
            if key in seen:
                continue
            seen.add(key)
            step = dict(s)
            step["id"] = f"{s.get('kind')}:{s.get('callee', '')}:{s.get('file', '')}:{s.get('line', 0)}"
            out.append(step)
    return out


def write_algorithm_md(path: Path, algorithm: dict) -> None:
    lines = [f"# 算法重建：{algorithm['module']}",
             "", algorithm.get("description", ""), ""]
    for e in algorithm["entries"]:
        ep = e["entry"]
        lines.append(f"## 入口 {ep.get('label', ep.get('symbol'))}")
        lines.append("")
        lines.append("| # | 类型 | 步骤 | 代码锚点 |")
        lines.append("|---|---|---|---|")
        for i, s in enumerate(e["steps"], 1):
            anchor = f"{s['file']}:{s['line']}" if s["file"] else "-"
            lines.append(f"| {i} | {s['kind']} | {s['label']} | {anchor} |")
        lines.append("")
        for s in e["steps"]:
            if s["snippet"]:
                lines.append(f"- [{s['kind']}] {s['label']} — `{s['snippet'][:100]}`")
        lines.append("")
    lines.append("## 统计")
    lines.append("")
    st = algorithm["stats"]
    lines.append(f"- 入口 {st['entries']} 个，步骤 {st['steps']} 条")
    path.write_text("\n".join(lines), encoding="utf-8")
