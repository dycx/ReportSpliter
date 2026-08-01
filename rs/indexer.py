"""项目代码图构建：收集 .java 文件 → 解析 → 符号表 → 边 → 全局解析。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Set

from rs.java_analyzer import analyze_java_file
from rs.models import (
    SCHEMA_INDEX,
    EDGE_ANNOTATION,
    EDGE_CALL,
    EDGE_EXTENDS,
    EDGE_FIELD_TYPE,
    EDGE_IMPLEMENTS,
    EDGE_IMPORT,
    EDGE_TYPE_REF,
    Edge,
    ImportRef,
    SourceFile,
    Symbol,
)


def _is_excluded(rel_path: str, cfg_exclude_dirs: List[str], cfg_exclude_globs: List[str],
                 root: Path) -> bool:
    for d in cfg_exclude_dirs:
        if d and f"/{d}/" in f"/{rel_path}/":
            return True
    if cfg_exclude_globs:
        import fnmatch
        for g in cfg_exclude_globs:
            if fnmatch.fnmatch(rel_path, g) or fnmatch.fnmatch(Path(rel_path).name, g):
                return True
    return False


class ProjectIndex:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.files: Dict[str, SourceFile] = {}
        self.symbols: Dict[str, Symbol] = {}
        self.qname_index: Dict[str, List[str]] = {}
        self.edges: List[Edge] = []
        self.built_at: str = ""

    # ------------------------------------------------------------- 构建

    def build(self, exclude_dirs: List[str], exclude_globs: List[str],
              refresh: bool = False, quiet: bool = False) -> int:
        from rs import __version__
        import datetime

        files = []
        for p in self.root.rglob("*.java"):
            rel = p.relative_to(self.root).as_posix()
            if _is_excluded(rel, exclude_dirs, exclude_globs, self.root):
                continue
            files.append(p)

        # 第一遍：解析所有文件
        parsed = []
        for p in sorted(files):
            rel = p.relative_to(self.root).as_posix()
            try:
                result = analyze_java_file(rel, p.read_bytes())
                parsed.append(result)
                self.files[rel] = result.file
                for s in result.symbols:
                    self.symbols[s.id] = s
                    self.qname_index.setdefault(s.qname, []).append(s.id)
                self.edges.extend(result.edges)
            except Exception as e:  # 单个文件解析失败不阻断整体
                if not quiet:
                    print(f"[index] WARN parse failed {rel}: {e}")

        # 第二遍：标记外部边
        self._mark_external_edges()
        self.built_at = datetime.datetime.now().isoformat(timespec="seconds")
        return len(files)

    def _mark_external_edges(self) -> None:
        project_packages = {f.package for f in self.files.values() if f.package}
        prefix_set: Set[str] = set()
        for pkg in project_packages:
            segs = pkg.split(".")
            prefix_set.add(".".join(segs[:2]) if len(segs) >= 2 else pkg)

        def is_project_dst(dst: str) -> bool:
            if dst in self.symbols:
                return True
            return any(dst == p or dst.startswith(p + ".") for p in prefix_set)

        for e in self.edges:
            if e.kind == EDGE_IMPORT:
                # 通配导入/静态导入按包前缀判断
                name = e.dst
                e.external = not any(name == p or name.startswith(p + ".") for p in prefix_set)
                continue
            if e.dst in self.symbols:
                e.external = False
            elif is_project_dst(e.dst):
                # 项目包前缀下的目标（可能是继承方法等），不算外部
                e.external = False
            else:
                e.external = True

    # ------------------------------------------------------------- 序列化

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_INDEX,
            "project_root": str(self.root),
            "language": "java",
            "built_at": self.built_at,
            "files": [f.to_dict() for f in sorted(self.files.values(), key=lambda x: x.path)],
            "symbols": {k: v.to_dict() for k, v in sorted(self.symbols.items())},
            "qname_index": self.qname_index,
            "edges": [e.to_dict() for e in self.edges],
            "stats": {
                "files": len(self.files),
                "symbols": len(self.symbols),
                "edges": len(self.edges),
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectIndex":
        idx = cls(data.get("project_root", "."))
        idx.built_at = data.get("built_at", "")
        for fd in data.get("files", []):
            imports = [
                ImportRef(name=i.get("name", ""), line=i.get("line", 0),
                          wildcard=i.get("wildcard", False), static=i.get("static", False))
                for i in fd.get("imports", [])
            ]
            f = SourceFile(path=fd.get("path", ""), package=fd.get("package", ""),
                           lines=fd.get("lines", 0), imports=imports,
                           symbols=fd.get("symbols", []))
            idx.files[f.path] = f
        for sid, sd in data.get("symbols", {}).items():
            idx.symbols[sid] = Symbol(**sd)
        idx.qname_index = data.get("qname_index", {})
        if not idx.qname_index:
            for sid, s in idx.symbols.items():
                idx.qname_index.setdefault(s.qname, []).append(sid)
        for ed in data.get("edges", []):
            idx.edges.append(Edge(**ed))
        return idx

    def resolve_qname(self, qname: str) -> List[str]:
        """qname（无签名）→ 符号 id 列表。"""
        ids = self.qname_index.get(qname, [])
        if ids:
            return sorted(ids)
        if qname in self.symbols:
            return [qname]
        return []

    def edges_from(self, symbol_id: str) -> List[Edge]:
        return [e for e in self.edges if e.src == symbol_id]

    def edges_to(self, symbol_id: str) -> List[Edge]:
        return [e for e in self.edges if e.dst == symbol_id]
