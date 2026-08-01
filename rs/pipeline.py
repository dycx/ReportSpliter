"""端到端流程编排：index → discover → resolve → export → validate。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from rs.config import ProjectConfig, load_config
from rs.discover import discover_entries
from rs.indexer import ProjectIndex
from rs.models import ModuleSpec
from rs.package import export_module
from rs.resolve import resolve_module
from rs.store import (
    entry_path,
    graph_path,
    index_path,
    load_entries,
    load_graph,
    load_index,
    out_dir,
    save_entries,
    save_graph,
    save_index,
)
from rs.validate import audit_module


def build_index(cfg: ProjectConfig, refresh: bool = False, quiet: bool = False) -> ProjectIndex:
    index = None if refresh else load_index(cfg.root)
    if index is not None:
        if not quiet:
            print(f"[index] 使用缓存 {index_path(cfg.root)}（{len(index.symbols)} 符号，"
                  f"{len(index.files)} 文件）")
        return index
    index = ProjectIndex(cfg.root)
    n = index.build(cfg.exclude_dirs, cfg.exclude_globs, refresh=refresh, quiet=quiet)
    save_index(index, cfg.root)
    if not quiet:
        print(f"[index] 已索引 {n} 个文件，{len(index.symbols)} 符号，"
              f"{len(index.edges)} 边 → {index_path(cfg.root)}")
    return index


def get_entries(cfg: ProjectConfig, index: ProjectIndex, quiet: bool = False):
    entries = load_entries(cfg.root)
    if entries is None:
        entries = discover_entries(index)
        save_entries(entries, cfg.root)
    if not quiet:
        print(f"[discover] {len(entries)} 个入口（{entry_path(cfg.root)}）")
    return entries


def find_module(cfg: ProjectConfig, name: str) -> ModuleSpec:
    for m in cfg.modules:
        if m.name == name:
            return m
    names = ", ".join(m.name for m in cfg.modules) or "（无）"
    raise ValueError(f"project.yml 中不存在模块 '{name}'。已有模块: {names}")


def run_module(cfg: ProjectConfig, module_name: str, refresh: bool = False,
               quiet: bool = False) -> dict:
    module = find_module(cfg, module_name)
    index = build_index(cfg, refresh=refresh, quiet=quiet)
    entries = get_entries(cfg, index, quiet=quiet)

    graph = load_graph(cfg.root, module_name) if not refresh else None
    if graph is None:
        graph = resolve_module(index, module, entries)
        save_graph(graph, cfg.root)
    if not quiet:
        print(f"[resolve] 模块 '{module_name}'：{len(graph.symbols)} 符号，"
              f"{len(graph.files)} 文件")

    out = out_dir(cfg.root, module_name)
    manifest = export_module(cfg, graph, out)
    audit = audit_module(index, graph, out)
    if not quiet:
        print(f"[export] 模块包 → {out}")

    return {
        "module": module_name,
        "status": "ok",
        "artifacts": {
            "index": str(index_path(cfg.root)),
            "entries": str(entry_path(cfg.root)),
            "graph": str(graph_path(cfg.root, module_name)),
            "out": str(out),
            "manifest": str(out / "manifest.json"),
            "llm_context": str(out / "llm-context.md"),
            "audit": str(out / "audit-report.json"),
        },
        "entries_matched": len(manifest["entries"]),
        "stats": manifest["stats"],
        "audit_stats": audit["stats"],
        "over_inclusion_ratio": audit["over_inclusion"]["ratio"],
    }


def default_project_cfg(project_dir: str) -> ProjectConfig:
    return load_config(project_dir)
