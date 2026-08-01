"""模块依赖闭包：从入口种子出发，沿调用/类型/继承/注解边反向可达。"""

from __future__ import annotations

from collections import deque
from typing import Dict, List, Set

from rs.discover import discover_entries
from rs.indexer import ProjectIndex
from rs.models import (
    EDGE_ANNOTATION,
    EDGE_CALL,
    EDGE_EXTENDS,
    EDGE_FIELD_TYPE,
    EDGE_IMPLEMENTS,
    EDGE_TYPE_REF,
    Edge,
    EntryPoint,
    ModuleGraph,
    ModuleSpec,
)


# 闭包沿这些反向边展开（src 依赖 dst，反向即从 src 找其依赖）
FOLLOW_KINDS = {
    EDGE_CALL,
    EDGE_TYPE_REF,
    EDGE_FIELD_TYPE,
    EDGE_EXTENDS,
    EDGE_IMPLEMENTS,
    EDGE_ANNOTATION,
}


def match_entry_point(ep: EntryPoint, spec_entry: dict) -> bool:
    """判断一个发现的入口是否匹配模块规格中的条目。"""
    etype = spec_entry.get("type")
    if etype and etype != ep.type:
        # http_endpoint 规格按路径匹配即可
        if not (etype == "http_endpoint" and ep.type == "http_endpoint"):
            return False

    path = spec_entry.get("path")
    method = spec_entry.get("method")
    name = spec_entry.get("name")

    if ep.type == "http_endpoint":
        if path:
            p = str(path).rstrip("/")
            if ep.http_path != p and not ep.http_path.startswith(p + "/"):
                return False
        if method and method.upper() != (ep.http_method or "ANY"):
            return False
        return True

    if name:
        return ep.symbol == name or ep.symbol.startswith(name)
    return True


def match_entries(entries: List[EntryPoint], spec_entries: List[dict]) -> List[EntryPoint]:
    matched = []
    for ep in entries:
        if any(match_entry_point(ep, se) for se in spec_entries):
            matched.append(ep)
    return matched


def _project_package_prefixes(index: ProjectIndex) -> Set[str]:
    prefixes: Set[str] = set()
    for f in index.files.values():
        if not f.package:
            continue
        segs = f.package.split(".")
        prefixes.add(".".join(segs[:2]) if len(segs) >= 2 else f.package)
    return prefixes


def resolve_module(index: ProjectIndex, module: ModuleSpec,
                   entry_points: List[EntryPoint] | None = None) -> ModuleGraph:
    """计算模块闭包。"""
    if entry_points is None:
        entry_points = discover_entries(index)

    matched = match_entries(entry_points, module.entries)
    if not matched:
        raise ValueError(
            f"模块 '{module.name}' 未匹配到任何入口。"
            f"当前项目发现 {len(entry_points)} 个入口，请检查 project.yml 的 entries。"
        )

    # 种子：入口符号 + 其所属类型
    seed_symbols: List[str] = []
    for ep in matched:
        if ep.symbol not in index.symbols:
            continue
        seed_symbols.append(ep.symbol)
        owner = index.symbols[ep.symbol].owner
        if owner and owner in index.symbols:
            seed_symbols.append(owner)

    excluded_prefixes = [s for s in module.exclude_symbols if s]

    def is_excluded(sid: str) -> bool:
        return any(sid == p or sid.startswith(p) for p in excluded_prefixes)

    # BFS 反向闭包
    symbols: Dict[str, object] = {}
    reachable_from: Dict[str, Set[str]] = {}
    edge_index: Dict[str, List[Edge]] = {}
    for e in index.edges:
        edge_index.setdefault(e.src, []).append(e)

    worklist: deque = deque()

    def add(sym_id: str, src_entries: Set[str]) -> None:
        if sym_id in symbols or sym_id not in index.symbols or is_excluded(sym_id):
            return
        sym = index.symbols[sym_id]
        symbols[sym_id] = sym
        reachable_from[sym_id] = set(src_entries)
        worklist.append(sym_id)
        # 类被纳入时，字段一并纳入（字段的 field_type 边会继续拉依赖）
        if sym.kind in ("class", "interface", "enum", "record", "annotation"):
            for e in edge_index.get(sym_id, []):
                if e.kind == "contains":
                    child = index.symbols.get(e.dst)
                    if child is not None and child.kind == "field":
                        add(e.dst, src_entries)

    for ep in matched:
        add(ep.symbol, {ep.id})
        owner = index.symbols.get(ep.symbol).owner if ep.symbol in index.symbols else None
        if owner:
            add(owner, {ep.id})

    while worklist:
        cur = worklist.popleft()
        cur_entries = reachable_from.get(cur, set())
        for e in edge_index.get(cur, []):
            if e.kind not in FOLLOW_KINDS or not e.resolved or e.external:
                continue
            dst = e.dst
            targets = index.resolve_qname(dst) if dst not in index.symbols else [dst]
            if not targets:
                continue
            for dst_id in targets:
                # 方法/字段被纳入时，其所属类型一并纳入
                dst_sym = index.symbols[dst_id]
                if dst_sym.owner and dst_sym.owner in index.symbols:
                    add(dst_sym.owner, cur_entries)
                add(dst_id, cur_entries)

    # 闭包内边
    closure_edges = []
    for e in index.edges:
        if e.src in symbols and e.kind in FOLLOW_KINDS:
            dst_ids = index.resolve_qname(e.dst) if e.dst not in symbols else [e.dst]
            if any(d in symbols for d in dst_ids):
                closure_edges.append(e.to_dict())

    # 外部引用（闭包内符号指向项目外）
    external_refs: Dict[str, List[str]] = {}
    for e in index.edges:
        if e.src in symbols and e.external and e.dst not in symbols:
            lst = external_refs.setdefault(e.kind, [])
            if e.dst not in lst:
                lst.append(e.dst)

    # 未解析调用（闭包内）
    unresolved = []
    for e in index.edges:
        if e.src in symbols and e.kind == EDGE_CALL and not e.resolved:
            unresolved.append({
                "caller": e.src,
                "callee": e.dst,
                "file": index.symbols[e.src].file,
                "line": e.line,
            })

    graph = ModuleGraph(
        name=module.name,
        description=module.description,
        entries=[ep.to_dict() for ep in matched],
        seed_symbols=seed_symbols,
        symbols={k: v for k, v in symbols.items()},
        files=sorted({s.file for s in symbols.values()}),
        edges=closure_edges,
        reachable_from={k: sorted(v) for k, v in reachable_from.items()},
        external_refs={k: sorted(v)[:500] for k, v in external_refs.items()},
        unresolved_calls=unresolved,
    )
    return graph
