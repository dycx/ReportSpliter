"""模块物化：把闭包写入 out/<module>/，产出 manifest.json 与 llm-context.md。"""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from typing import Dict, List

from rs.models import EDGE_CALL, SCHEMA_MANIFEST, ModuleGraph, ProjectConfig


def _code_dir(out_root: Path) -> Path:
    return out_root / "code"


def export_module(cfg: ProjectConfig, graph: ModuleGraph, out_root: Path) -> dict:
    """物化模块包，返回 manifest 数据。"""
    out_root.mkdir(parents=True, exist_ok=True)
    code = _code_dir(out_root)
    code.mkdir(parents=True, exist_ok=True)
    project_root = Path(cfg.root)

    # 拷贝闭包命中的源文件
    files_meta = []
    for rel in graph.files:
        src = project_root / rel
        if not src.exists():
            continue
        dst = code / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        syms = []
        for sid in graph.symbols:
            s = graph.symbols[sid]
            if s.file == rel:
                syms.append({
                    "id": sid,
                    "kind": s.kind,
                    "simple": s.simple,
                    "start": s.start_line,
                    "end": s.end_line,
                    "reachable_from": graph.reachable_from.get(sid, []),
                })
        syms.sort(key=lambda x: (x["start"], x["end"]))
        file_lines = max((x["end"] for x in syms), default=0)
        files_meta.append({
            "path": rel,
            "kind": "source",
            "lines": file_lines,
            "symbols": syms,
        })

    # 资源闭包
    resources: List[str] = []
    for m in cfg.modules:
        if m.name == graph.name:
            resources = m.resources
            break
    for pattern in resources:
        for p in sorted(project_root.glob(pattern)):
            if not p.is_file():
                continue
            rel = p.relative_to(project_root).as_posix()
            dst = code / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            files_meta.append({"path": rel, "kind": "resource", "lines": 0, "symbols": []})

    call_edges = [e for e in graph.edges if e.get("kind") == EDGE_CALL]
    stats = {
        "files": len(files_meta),
        "symbols": len(graph.symbols),
        "entries": len(graph.entries),
        "call_edges": len(call_edges),
        "external_refs": sum(len(v) for v in graph.external_refs.values()),
        "unresolved_calls": len(graph.unresolved_calls),
    }

    manifest = {
        "schema": SCHEMA_MANIFEST,
        "module": graph.name,
        "description": graph.description,
        "project_root": cfg.root,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "entries": graph.entries,
        "seed_symbols": graph.seed_symbols,
        "files": files_meta,
        "reachable_from": graph.reachable_from,
        "call_edges": [[e["src"], e["dst"], e.get("line", 0)] for e in call_edges],
        "external_refs": graph.external_refs,
        "unresolved_calls": graph.unresolved_calls,
        "stats": stats,
    }
    (out_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    write_llm_context(out_root / "llm-context.md", manifest)
    return manifest


def write_llm_context(path: Path, manifest: dict) -> None:
    """生成面向大模型的模块分析上下文。"""
    lines = []
    lines.append(f"# 模块：{manifest['module']}")
    if manifest.get("description"):
        lines.append(f"\n{manifest['description']}")

    lines.append("\n## 入口点")
    lines.append("\n| 类型 | 路径/方法 | 符号 | 位置 |")
    lines.append("|---|---|---|---|")
    for ep in manifest["entries"]:
        loc = f"{ep['file']}:{ep['line']}"
        if ep["type"] == "http_endpoint":
            route = f"{ep.get('http_method') or 'ANY'} {ep.get('http_path') or '/'}"
        else:
            route = ep.get("label", "")
        lines.append(f"| {ep['type']} | {route} | `{ep['symbol']}` | {loc} |")

    lines.append("\n## 文件清单")
    lines.append("\n```text")
    for f in manifest["files"]:
        hit = len(f.get("symbols", []))
        lines.append(f"{f['path']}  ({hit} 个命中符号)")
    lines.append("```")

    lines.append("\n## 每文件关键符号")
    for f in manifest["files"]:
        syms = f.get("symbols", [])
        if not syms:
            continue
        lines.append(f"\n### {f['path']}")
        for s in syms[:40]:
            lines.append(f"- `{s['simple']}` ({s['kind']}, L{s['start']}-L{s['end']})")
        if len(syms) > 40:
            lines.append(f"- ... 共 {len(syms)} 个符号")

    lines.append("\n## 模块内调用链")
    lines.append("\n```text")
    for src, dst, ln in manifest["call_edges"][:300]:
        lines.append(f"{src} -> {dst}  (L{ln})")
    if len(manifest["call_edges"]) > 300:
        lines.append(f"... 共 {len(manifest['call_edges'])} 条调用边")
    lines.append("```")

    lines.append("\n## 外部依赖（项目外引用）")
    lines.append("\n| 类型 | 引用 |")
    lines.append("|---|---|")
    for kind, names in manifest["external_refs"].items():
        for n in names[:100]:
            lines.append(f"| {kind} | `{n}` |")

    lines.append("\n## 未解析调用（潜在切片缺口）")
    if manifest["unresolved_calls"]:
        lines.append("\n| 调用方 | 被调 | 位置 |")
        lines.append("|---|---|---|")
        for u in manifest["unresolved_calls"][:100]:
            lines.append(f"| `{u['caller']}` | `{u['callee']}` | {u['file']}:{u['line']} |")
    else:
        lines.append("\n无。")

    lines.append("\n## 统计")
    st = manifest["stats"]
    lines.append(f"\n- 文件 {st['files']}，符号 {st['symbols']}，入口 {st['entries']}，"
                 f"调用边 {st['call_edges']}，外部引用 {st['external_refs']}，"
                 f"未解析调用 {st['unresolved_calls']}")
    lines.append("\n> 代码本体在 code/ 目录，按原项目相对路径组织。")

    path.write_text("\n".join(lines), encoding="utf-8")
