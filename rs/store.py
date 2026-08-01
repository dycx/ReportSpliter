"""产物路径与读写。所有中间产物落在 <project>/.report-spliter/。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from rs.indexer import ProjectIndex
from rs.models import EntryPoint, ModuleGraph


def rs_dir(root: str | Path) -> Path:
    return Path(root) / ".report-spliter"


def index_path(root: str | Path) -> Path:
    return rs_dir(root) / "index.json"


def entry_path(root: str | Path) -> Path:
    return rs_dir(root) / "entry-points.json"


def graph_path(root: str | Path, module: str) -> Path:
    return rs_dir(root) / "graphs" / f"{module}.json"


def out_dir(root: str | Path, module: str) -> Path:
    return Path(root) / "out" / module


def load_index(root: str | Path) -> ProjectIndex | None:
    p = index_path(root)
    if not p.exists():
        return None
    try:
        return ProjectIndex.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def save_index(index: ProjectIndex, root: str | Path) -> Path:
    p = index_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(index.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_entries(root: str | Path) -> List[EntryPoint] | None:
    p = entry_path(root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [EntryPoint(**e) for e in data.get("entry_points", [])]
    except Exception:
        return None


def save_entries(entries: List[EntryPoint], root: str | Path) -> Path:
    p = entry_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": "entry-points/v1",
        "total": len(entries),
        "entry_points": [e.to_dict() for e in entries],
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_graph(root: str | Path, module: str) -> ModuleGraph | None:
    p = graph_path(root, module)
    if not p.exists():
        return None
    try:
        return ModuleGraph.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def save_graph(graph: ModuleGraph, root: str | Path) -> Path:
    p = graph_path(root, graph.name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(graph.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return p

