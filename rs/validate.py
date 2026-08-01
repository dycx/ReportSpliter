"""结构校验与审计：切片缺口 / 过度提取 / 引用一致性。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Set

from rs.indexer import ProjectIndex
from rs.models import EDGE_CALL, SCHEMA_AUDIT, ModuleGraph


def _project_prefixes(index: ProjectIndex) -> Set[str]:
    prefixes: Set[str] = set()
    for f in index.files.values():
        if not f.package:
            continue
        segs = f.package.split(".")
        prefixes.add(".".join(segs[:2]) if len(segs) >= 2 else f.package)
    return prefixes


def audit_module(index: ProjectIndex, graph: ModuleGraph, out_root: Path) -> dict:
    """生成审计报告。"""
    included_files = set(graph.files)
    prefixes = _project_prefixes(index)
    included_set = set(graph.symbols.keys())

    # 1) 项目内引用缺口：闭包文件里 import 了项目内类但该类文件不在模块中
    gaps = []
    for rel in graph.files:
        sf = index.files.get(rel)
        if not sf:
            continue
        for imp in sf.imports:
            if imp.wildcard or imp.static:
                continue
            qname = imp.name
            # 只关心项目内的类
            if not any(qname == p or qname.startswith(p + ".") for p in prefixes):
                continue
            target = index.symbols.get(qname)
            if target is None:
                continue
            if target.file not in included_files:
                gaps.append({
                    "file": rel,
                    "import": qname,
                    "target_file": target.file,
                    "reason": "引用了项目内类但该类未被纳入模块",
                })

    # 2) 未解析调用（闭包内）
    unresolved = graph.unresolved_calls

    # 3) 内部调用目标缺失：接收者是项目类型但方法未找到（可能继承或缺口）
    internal_missing = []
    for e in index.edges:
        if e.src in included_set and e.kind == EDGE_CALL and e.resolved and not e.external:
            if not any(e.dst.startswith(p + ".") for p in prefixes):
                continue
            ids = index.resolve_qname(e.dst) if e.dst not in index.symbols else [e.dst]
            if not ids:
                # 接收者是项目类型但方法未声明（继承自外部父类/接口等）
                internal_missing.append({
                    "caller": e.src,
                    "callee": e.dst,
                    "file": index.symbols[e.src].file,
                    "line": e.line,
                    "kind": "inherited_or_unresolved",
                })
            elif not any(i in included_set for i in ids):
                internal_missing.append({
                    "caller": e.src,
                    "callee": e.dst,
                    "file": index.symbols[e.src].file,
                    "line": e.line,
                    "kind": "gap",
                    "targets": ids,
                })

    # 4) 过度提取度量
    total_file_lines = 0
    total_unique_symbol_lines = 0
    low_hit_files = []
    for rel in graph.files:
        sf = index.files.get(rel)
        fl = sf.lines if sf else 0
        total_file_lines += fl
        syms = [s for s in graph.symbols.values() if s.file == rel]
        covered = set()
        for s in syms:
            covered.update(range(s.start_line, s.end_line + 1))
        total_unique_symbol_lines += len(covered)
        if len(syms) <= 1:
            low_hit_files.append({"path": rel, "symbols": len(syms)})

    over_inclusion_ratio = round(total_file_lines / max(1, total_unique_symbol_lines), 2)

    report = {
        "schema": SCHEMA_AUDIT,
        "module": graph.name,
        "included_files": len(graph.files),
        "included_symbols": len(graph.symbols),
        "project_import_gaps": gaps,
        "unresolved_calls": unresolved,
        "internal_call_missing": internal_missing,
        "over_inclusion": {
            "file_lines": total_file_lines,
            "unique_symbol_lines": total_unique_symbol_lines,
            "ratio": over_inclusion_ratio,
            "note": "ratio=文件总行数/闭包唯一符号行数，越大说明整文件包含的无关代码越多",
        },
        "low_hit_files": low_hit_files,
        "stats": {
            "files": len(graph.files),
            "symbols": len(graph.symbols),
            "gaps": len(gaps),
            "unresolved": len(unresolved),
            "internal_missing": len(internal_missing),
            "low_hit_files": len(low_hit_files),
        },
    }
    (out_root / "audit-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
