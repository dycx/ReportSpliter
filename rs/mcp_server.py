"""最小 MCP stdio 服务（JSON-RPC 2.0，newline-delimited）。

供 opencode / Claude Code 通过 mcpServers 配置接入：
  {
    "mcpServers": {
      "report-spliter": { "command": "rs", "args": ["mcp"] }
    }
  }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from rs import __version__
from rs.algorithm import reconstruct_algorithm
from rs.align import align_algorithms, load_design_spec
from rs.config import load_config, write_template
from rs.design_import import import_design, write_spec
from rs.lineage import build_registry, discover_reports
from rs.models import ModuleSpec
from rs.package import export_module
from rs.pipeline import build_index, find_module, get_entries, run_module
from rs.resolve import resolve_module
from rs.store import load_graph, out_dir, save_graph
from rs.validate import audit_module


PROTOCOL_VERSION = "2025-06-18"


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "rs_init",
        "description": "在项目目录生成/更新 project.yml（模块与入口）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "module": {"type": "string", "description": "模块名"},
                "entry": {"type": "array", "items": {"type": "string"},
                          "description": "入口，如 http:/api/reports 或 symbol:com.acme.X"},
                "desc": {"type": "string", "default": ""},
            },
        },
    },
    {
        "name": "rs_index",
        "description": "构建/更新项目代码图",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "refresh": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "rs_discover",
        "description": "发现 Spring 入口（HTTP 端点/定时任务/监听器/main/Feign）",
        "inputSchema": {
            "type": "object",
            "properties": {"project_dir": {"type": "string", "default": "."}},
        },
    },
    {
        "name": "rs_modules",
        "description": "列出 project.yml 中的模块及导出状态",
        "inputSchema": {
            "type": "object",
            "properties": {"project_dir": {"type": "string", "default": "."}},
        },
    },
    {
        "name": "rs_resolve",
        "description": "计算指定模块的依赖闭包",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "module": {"type": "string"},
            },
            "required": ["module"],
        },
    },
    {
        "name": "rs_export",
        "description": "物化模块包（code/ + manifest.json + llm-context.md）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "module": {"type": "string"},
            },
            "required": ["module"],
        },
    },
    {
        "name": "rs_validate",
        "description": "模块结构校验与审计（缺口/过度提取/未解析调用）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "module": {"type": "string"},
            },
            "required": ["module"],
        },
    },
    {
        "name": "rs_run",
        "description": "端到端：index→discover→resolve→export→validate",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "module": {"type": "string"},
                "refresh": {"type": "boolean", "default": False},
            },
            "required": ["module"],
        },
    },
    {
        "name": "rs_algorithm",
        "description": "代码→算法重建：还原模块每个入口的算法步骤（带代码锚点）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "module": {"type": "string"},
            },
            "required": ["module"],
        },
    },
    {
        "name": "rs_align",
        "description": "代码重建算法 vs 设计算法（algorithms/<module>.yaml）对齐，输出差异与人工审核队列",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "module": {"type": "string"},
                "design": {"type": "string", "default": ""},
            },
            "required": ["module"],
        },
    },
    {
        "name": "rs_reports",
        "description": "发现报表（Hive 表/CSV）并构建血缘注册表",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "with_history": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "rs_report",
        "description": "查看单个报表的血缘明细",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "rs_design_import",
        "description": "导入 Word/Excel/Markdown 设计算法 → algorithms/<module>.yaml",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "default": "."},
                "module": {"type": "string"},
                "from": {"type": "string", "description": "设计文档路径"},
            },
            "required": ["module", "from"],
        },
    },
]


def _tool_result(text: str, is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        project_dir = str(Path(args.get("project_dir", ".")).resolve())
        module = args.get("module", "")

        if name == "rs_init":
            specs = []
            if args.get("module"):
                specs.append(ModuleSpec(
                    name=args["module"],
                    description=args.get("desc", ""),
                    entries=[_parse_entry(e) for e in args.get("entry", []) or []],
                ))
            path = write_template(project_dir, specs)
            return _tool_result(json.dumps({"config": str(path), "modules": [s.to_dict() for s in specs]},
                                           ensure_ascii=False, indent=2))

        cfg = load_config(project_dir)

        if name == "rs_index":
            idx = build_index(cfg, refresh=bool(args.get("refresh")), quiet=True)
            return _tool_result(json.dumps({"files": len(idx.files), "symbols": len(idx.symbols),
                                            "edges": len(idx.edges)}, ensure_ascii=False))

        if name == "rs_discover":
            idx = build_index(cfg, quiet=True)
            entries = get_entries(cfg, idx, quiet=True)
            return _tool_result(json.dumps(
                {"total": len(entries), "entry_points": [e.to_dict() for e in entries]},
                ensure_ascii=False, indent=2))

        if name == "rs_modules":
            rows = [{
                "module": m.name,
                "description": m.description,
                "entries": len(m.entries),
                "exported": (out_dir(cfg.root, m.name) / "manifest.json").exists(),
            } for m in cfg.modules]
            return _tool_result(json.dumps({"modules": rows}, ensure_ascii=False, indent=2))

        MODULE_TOOLS = {
            "rs_resolve", "rs_export", "rs_validate", "rs_run",
            "rs_algorithm", "rs_align", "rs_design_import",
        }
        if name in MODULE_TOOLS and not module:
            return _tool_result("缺少参数 module", is_error=True)
        if name == "rs_design_import" and not args.get("from"):
            return _tool_result("缺少参数 from（设计文档路径）", is_error=True)

        if name == "rs_resolve":
            spec = find_module(cfg, module)
            idx = build_index(cfg, quiet=True)
            entries = get_entries(cfg, idx, quiet=True)
            graph = resolve_module(idx, spec, entries)
            save_graph(graph, cfg.root)
            return _tool_result(json.dumps({
                "module": module, "symbols": len(graph.symbols),
                "files": len(graph.files), "entries_matched": len(graph.entries),
                "unresolved": len(graph.unresolved_calls),
            }, ensure_ascii=False, indent=2))

        if name == "rs_export":
            graph = load_graph(cfg.root, module)
            if graph is None:
                return _tool_result(f"缺少模块图，先调用 rs_resolve {module}", is_error=True)
            out = out_dir(cfg.root, module)
            manifest = export_module(cfg, graph, out)
            return _tool_result(json.dumps({
                "module": module, "out": str(out),
                "stats": manifest["stats"],
                "llm_context": str(out / "llm-context.md"),
            }, ensure_ascii=False, indent=2))

        if name == "rs_validate":
            graph = load_graph(cfg.root, module)
            if graph is None:
                return _tool_result(f"缺少模块图，先调用 rs_resolve {module}", is_error=True)
            idx = build_index(cfg, quiet=True)
            audit = audit_module(idx, graph, out_dir(cfg.root, module))
            return _tool_result(json.dumps(audit, ensure_ascii=False, indent=2))

        if name == "rs_run":
            summary = run_module(cfg, module, refresh=bool(args.get("refresh")), quiet=True)
            return _tool_result(json.dumps(summary, ensure_ascii=False, indent=2))

        if name == "rs_algorithm":
            from rs.pipeline import build_index, find_module, get_entries
            from rs.resolve import resolve_module
            from rs.store import load_graph, save_graph
            idx = build_index(cfg, quiet=True)
            graph = load_graph(cfg.root, module)
            if graph is None:
                spec = find_module(cfg, module)
                entries = get_entries(cfg, idx, quiet=True)
                graph = resolve_module(idx, spec, entries)
                save_graph(graph, cfg.root)
            alg = reconstruct_algorithm(idx, graph, cfg.root)
            return _tool_result(json.dumps(alg, ensure_ascii=False, indent=2))

        if name == "rs_align":
            from rs.algorithm import reconstruct_algorithm
            from rs.pipeline import build_index, find_module, get_entries
            from rs.resolve import resolve_module
            from rs.store import load_graph, save_graph
            idx = build_index(cfg, quiet=True)
            graph = load_graph(cfg.root, module)
            if graph is None:
                spec = find_module(cfg, module)
                entries = get_entries(cfg, idx, quiet=True)
                graph = resolve_module(idx, spec, entries)
                save_graph(graph, cfg.root)
            alg = reconstruct_algorithm(idx, graph, cfg.root)
            design_file = args.get("design") or f"{cfg.root}/algorithms/{module}.yaml"
            design = load_design_spec(design_file)
            report = align_algorithms(alg, design)
            return _tool_result(json.dumps(report, ensure_ascii=False, indent=2))

        if name == "rs_reports":
            registry = build_registry(cfg.root, with_history=bool(args.get("with_history")))
            return _tool_result(json.dumps(registry, ensure_ascii=False, indent=2))

        if name == "rs_report":
            reports = discover_reports(cfg.root)
            name = args.get("name", "")
            hit = [r for r in reports
                   if r["name"] == name or r["output"] == name
                   or Path(r["output"]).name == name
                   or r["output"].endswith("/" + name)]
            if not hit:
                return _tool_result(f"未找到报表 '{name}'", is_error=True)
            return _tool_result(json.dumps(hit[0], ensure_ascii=False, indent=2))

        if name == "rs_design_import":
            steps = import_design(args.get("from", ""))
            out = f"{cfg.root}/algorithms/{module}.yaml"
            write_spec(module, steps, out)
            return _tool_result(json.dumps(
                {"module": module, "steps": len(steps), "spec": out,
                 "preview": steps[:10]}, ensure_ascii=False, indent=2))

        return _tool_result(f"未知工具: {name}", is_error=True)
    except Exception as e:
        return _tool_result(f"错误: {type(e).__name__}: {e}", is_error=True)


def _parse_entry(raw: str) -> dict:
    from rs.cli import _parse_entry_spec
    return _parse_entry_spec(raw)


def _send(msg: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve_stdio() -> None:
    """读取 stdin 上的 JSON-RPC 消息并响应。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        # 通知类消息不响应
        if msg_id is None:
            continue

        if method == "initialize":
            _send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "report-spliter", "version": __version__},
                },
            })
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            result = _call_tool(name, args)
            _send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method in ("shutdown", "exit"):
            _send({"jsonrpc": "2.0", "id": msg_id, "result": None})
            break
        else:
            _send({"jsonrpc": "2.0", "id": msg_id,
                   "error": {"code": -32601, "message": f"Method not found: {method}"}})
