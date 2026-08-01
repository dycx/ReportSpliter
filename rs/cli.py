"""rs 命令行接口。所有命令机器可读（--json），供 agent 调用。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

import click

from rs import __version__
from rs.algorithm import reconstruct_algorithm, write_algorithm_md
from rs.align import align_algorithms, load_design_spec, write_alignment_md
from rs.config import default_config, load_config, write_template
from rs.design_import import import_design, write_spec
from rs.discover import discover_entries
from rs.lineage import build_registry, discover_reports, write_registry_md
from rs.models import ModuleSpec
from rs.pipeline import build_index, find_module, get_entries, run_module
from rs.package import export_module
from rs.resolve import resolve_module
from rs.store import (
    entry_path,
    graph_path,
    index_path,
    load_graph,
    load_index,
    out_dir,
    save_entries,
    save_graph,
)
from rs.validate import audit_module


def _json_out(data) -> None:
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _project_dir(path: str) -> str:
    return str(Path(path).resolve())


def _load_or_resolve(cfg, module: str):
    """返回 (index, graph)，必要时先构建索引并解析模块。"""
    idx = build_index(cfg, quiet=True)
    graph = load_graph(cfg.root, module)
    if graph is None:
        spec = find_module(cfg, module)
        entries = get_entries(cfg, idx, quiet=True)
        graph = resolve_module(idx, spec, entries)
        save_graph(graph, cfg.root)
    return idx, graph


_ENTRY_TYPE_ALIAS = {
    "http": "http_endpoint",
    "symbol": "symbol",
    "scheduled": "scheduled",
    "listener": "listener",
    "feign": "feign_client",
    "main": "application_main",
}


def _parse_entry_spec(raw: str) -> dict:
    """解析 --entry 参数：http:/api/x / symbol:com.acme.X / scheduled: / main:"""
    if ":" in raw:
        t, v = raw.split(":", 1)
        t = _ENTRY_TYPE_ALIAS.get(t.strip(), t.strip())
        v = v.strip()
    else:
        t, v = "symbol", raw.strip()
    if t == "http_endpoint":
        return {"type": "http_endpoint", "path": v or "/"}
    if t == "symbol":
        return {"type": "symbol", "name": v}
    if t in ("scheduled", "listener", "feign_client", "application_main"):
        return {"type": t}
    return {"type": t, "path": v}


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="rs")
def cli() -> None:
    """ReportSpliter: Java/Spring 仓库模块级划分工具。"""


@cli.command("init")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--module", "modules", multiple=True, help="模块名，可重复")
@click.option("--entry", "entries", multiple=True, help="入口，如 http:/api/reports 或 symbol:com.acme.X")
@click.option("--desc", "desc", default="", help="模块描述")
def init(project_dir: str, modules: List[str], entries: List[str], desc: str) -> None:
    """生成 project.yml（含模块与入口）。"""
    dir_ = _project_dir(project_dir)
    Path(dir_).mkdir(parents=True, exist_ok=True)
    specs: List[ModuleSpec] = []
    if modules:
        for name in modules:
            specs.append(ModuleSpec(
                name=name,
                description=desc or f"{name} 模块",
                entries=[_parse_entry_spec(e) for e in entries],
            ))
    path = write_template(dir_, specs)
    click.echo(f"已生成 {path}")


@cli.command("index")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--refresh", is_flag=True, help="强制重建代码图")
@click.option("--json", "as_json", is_flag=True)
def index_cmd(project_dir: str, refresh: bool, as_json: bool) -> None:
    """构建/更新代码图。"""
    cfg = load_config(project_dir)
    idx = build_index(cfg, refresh=refresh)
    if as_json:
        _json_out({"files": len(idx.files), "symbols": len(idx.symbols),
                   "edges": len(idx.edges), "path": str(index_path(cfg.root))})


@cli.command("discover")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--json", "as_json", is_flag=True)
def discover_cmd(project_dir: str, as_json: bool) -> None:
    """Spring 入口发现。"""
    cfg = load_config(project_dir)
    idx = build_index(cfg, quiet=True)
    entries = get_entries(cfg, idx)
    if as_json:
        _json_out({"total": len(entries), "entry_points": [e.to_dict() for e in entries]})
    else:
        for e in sorted(entries, key=lambda x: x.type):
            click.echo(f"  [{e.type}] {e.label or e.symbol}  {e.file}:{e.line}")
        click.echo(f"共 {len(entries)} 个入口 → {entry_path(cfg.root)}")


@cli.command("resolve")
@click.argument("module")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--json", "as_json", is_flag=True)
def resolve_cmd(module: str, project_dir: str, as_json: bool) -> None:
    """计算模块依赖闭包。"""
    cfg = load_config(project_dir)
    spec = find_module(cfg, module)
    idx = build_index(cfg, quiet=True)
    entries = get_entries(cfg, idx, quiet=True)
    graph = resolve_module(idx, spec, entries)
    save_graph(graph, cfg.root)
    if as_json:
        _json_out({
            "module": module,
            "symbols": len(graph.symbols),
            "files": len(graph.files),
            "entries_matched": len(graph.entries),
            "unresolved": len(graph.unresolved_calls),
            "graph": str(graph_path(cfg.root, module)),
        })
    else:
        click.echo(f"模块 '{module}': {len(graph.symbols)} 符号 / {len(graph.files)} 文件 / "
                   f"{len(graph.entries)} 入口匹配 / {len(graph.unresolved_calls)} 未解析调用")


@cli.command("export")
@click.argument("module")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--json", "as_json", is_flag=True)
def export_cmd(module: str, project_dir: str, as_json: bool) -> None:
    """物化模块包（code/ + manifest.json + llm-context.md）。"""
    cfg = load_config(project_dir)
    graph = load_graph(cfg.root, module)
    if graph is None:
        raise click.ClickException(f"缺少模块图，先运行 rs resolve {module}")
    out = out_dir(cfg.root, module)
    manifest = export_module(cfg, graph, out)
    if as_json:
        _json_out({"module": module, "out": str(out),
                   "files": manifest["stats"]["files"],
                   "manifest": str(out / "manifest.json"),
                   "llm_context": str(out / "llm-context.md")})
    else:
        click.echo(f"模块包已生成 → {out}")


@cli.command("validate")
@click.argument("module")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--json", "as_json", is_flag=True)
def validate_cmd(module: str, project_dir: str, as_json: bool) -> None:
    """结构校验与审计。"""
    cfg = load_config(project_dir)
    idx = load_index(cfg.root)
    if idx is None:
        raise click.ClickException("缺少代码图，先运行 rs index")
    graph = load_graph(cfg.root, module)
    if graph is None:
        raise click.ClickException(f"缺少模块图，先运行 rs resolve {module}")
    audit = audit_module(idx, graph, out_dir(cfg.root, module))
    if as_json:
        _json_out(audit)
    else:
        st = audit["stats"]
        click.echo(f"审计 '{module}': 文件 {st['files']} / 符号 {st['symbols']} / "
                   f"引用缺口 {st['gaps']} / 未解析调用 {st['unresolved']} / "
                   f"内部调用缺失 {st['internal_missing']} / 低命中文件 {st['low_hit_files']}")
        click.echo(f"过度提取率: {audit['over_inclusion']['ratio']} "
                   f"（{audit['over_inclusion']['file_lines']} 文件行 / "
                   f"{audit['over_inclusion']['unique_symbol_lines']} 唯一符号行）")


@cli.command("run")
@click.argument("module")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--refresh", is_flag=True, help="强制重建索引")
@click.option("--json", "as_json", is_flag=True)
def run_cmd(module: str, project_dir: str, refresh: bool, as_json: bool) -> None:
    """全流程：index → discover → resolve → export → validate。"""
    cfg = load_config(project_dir)
    summary = run_module(cfg, module, refresh=refresh, quiet=as_json)
    if as_json:
        _json_out(summary)
    else:
        click.echo(f"模块 '{module}' 完成")
        click.echo(f"  入口匹配: {summary['entries_matched']}")
        click.echo(f"  符号/文件: {summary['stats']['symbols']} / {summary['stats']['files']}")
        click.echo(f"  未解析调用: {summary['stats']['unresolved_calls']}")
        click.echo(f"  过度提取率: {summary['over_inclusion_ratio']}")
        click.echo(f"  模块包: {summary['artifacts']['out']}")
        click.echo(f"  LLM 上下文: {summary['artifacts']['llm_context']}")


@cli.command("algorithm")
@click.argument("module")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--json", "as_json", is_flag=True)
def algorithm_cmd(module: str, project_dir: str, as_json: bool) -> None:
    """代码 → 算法重建：还原每个入口的算法步骤（带代码锚点）。"""
    cfg = load_config(project_dir)
    idx, graph = _load_or_resolve(cfg, module)
    alg = reconstruct_algorithm(idx, graph, cfg.root)
    out = out_dir(cfg.root, module)
    out.mkdir(parents=True, exist_ok=True)
    import json as _json
    (out / "algorithm.json").write_text(
        _json.dumps(alg, ensure_ascii=False, indent=2), encoding="utf-8")
    write_algorithm_md(out / "algorithm.md", alg)
    if as_json:
        _json_out({"module": module, "stats": alg["stats"],
                   "algorithm": str(out / "algorithm.json"),
                   "algorithm_md": str(out / "algorithm.md")})
    else:
        st = alg["stats"]
        click.echo(f"算法重建 '{module}': {st['entries']} 入口 / {st['steps']} 步骤 → "
                   f"{out / 'algorithm.md'}")


@cli.command("align")
@click.argument("module")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--design", "design_path", default=None,
              help="设计算法 YAML（默认 <project>/algorithms/<module>.yaml）")
@click.option("--json", "as_json", is_flag=True)
def align_cmd(module: str, project_dir: str, design_path: Optional[str], as_json: bool) -> None:
    """代码重建算法 vs 设计算法对齐，输出差异与人工审核队列。"""
    cfg = load_config(project_dir)
    idx, graph = _load_or_resolve(cfg, module)
    alg = reconstruct_algorithm(idx, graph, cfg.root)
    design_file = design_path or str(Path(cfg.root) / "algorithms" / f"{module}.yaml")
    try:
        design = load_design_spec(design_file)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))
    report = align_algorithms(alg, design)
    out = out_dir(cfg.root, module)
    out.mkdir(parents=True, exist_ok=True)
    import json as _json
    (out / "alignment-report.json").write_text(
        _json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_alignment_md(out / "alignment-report.md", report)
    (out / "review-queue.json").write_text(
        _json.dumps(report["review_queue"], ensure_ascii=False, indent=2), encoding="utf-8")
    if as_json:
        _json_out(report)
    else:
        st = report["stats"]
        click.echo(f"对齐 '{module}': 匹配 {st['matched']} / 代码新增 {st['added_in_code']} / "
                   f"设计缺失 {st['missing_in_code']} / 需人工审核 {st['needs_review']}")
        click.echo(f"  报告: {out / 'alignment-report.md'}")
        click.echo(f"  审核队列: {out / 'review-queue.json'}")


@cli.command("design-import")
@click.argument("module")
@click.option("--from", "from_path", required=True, help="设计文档路径（.md/.docx/.xlsx）")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--out", "out_path", default=None, help="输出 spec 路径（默认 algorithms/<module>.yaml）")
@click.option("--json", "as_json", is_flag=True)
def design_import_cmd(module: str, from_path: str, project_dir: str,
                      out_path: Optional[str], as_json: bool) -> None:
    """导入 Word/Excel/Markdown 设计算法 → algorithms/<module>.yaml。"""
    cfg = load_config(project_dir)
    try:
        steps = import_design(from_path)
    except (ValueError, Exception) as e:
        raise click.ClickException(str(e))
    if not steps:
        raise click.ClickException("文档中未解析到任何步骤，请检查格式")
    out = out_path or str(Path(cfg.root) / "algorithms" / f"{module}.yaml")
    write_spec(module, steps, out)
    if as_json:
        _json_out({"module": module, "steps": len(steps), "spec": out,
                   "preview": steps[:10]})
    else:
        click.echo(f"已导入 {len(steps)} 个步骤 → {out}")
        for s in steps[:8]:
            click.echo(f"  [{s['kind']}] {s['label']}")
        if len(steps) > 8:
            click.echo(f"  ... 共 {len(steps)} 步")


@cli.command("reports")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--with-history", is_flag=True, help="附加 git 变更历史")
@click.option("--json", "as_json", is_flag=True)
def reports_cmd(project_dir: str, with_history: bool, as_json: bool) -> None:
    """发现报表（Hive 表/CSV）并构建血缘注册表。"""
    cfg = load_config(project_dir)
    registry = build_registry(cfg.root, with_history=with_history)
    from rs.store import rs_dir
    rs_dir_path = rs_dir(cfg.root)
    rs_dir_path.mkdir(parents=True, exist_ok=True)
    (rs_dir_path / "reports.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    overview = Path(cfg.root) / "out" / "reports-overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    write_registry_md(overview, registry)
    if as_json:
        _json_out(registry)
    else:
        st = registry["stats"]
        click.echo(f"报表血缘: Hive 表 {st['hive_tables']} / 视图 {st['views']} / "
                   f"CSV {st['csv']}，共 {st['reports']} 个报告")
        for r in registry["reports"]:
            click.echo(f"  [{r['kind']}] {r['name']}  ← {', '.join(r['upstream_tables']) or '（无）'}")
        click.echo(f"  概览: {overview}")


@cli.command("report")
@click.argument("name")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--json", "as_json", is_flag=True)
def report_cmd(name: str, project_dir: str, as_json: bool) -> None:
    """查看单个报表的血缘明细。"""
    cfg = load_config(project_dir)
    reports = discover_reports(cfg.root)
    hit = [r for r in reports
           if r["name"] == name or r["output"] == name
           or Path(r["output"]).name == name
           or r["output"].endswith("/" + name)]
    if not hit:
        names = ", ".join(r["name"] for r in reports[:20]) or "（未发现）"
        raise click.ClickException(f"未找到报表 '{name}'。已发现: {names}")
    r = hit[0]
    if as_json:
        _json_out(r)
    else:
        click.echo(f"报表 {r['name']}（{r['kind']}）→ {r['output']}")
        for d in r["definitions"]:
            click.echo(f"  定义: {d['file']}:{d['line']} — {d['snippet'][:100]}")
        click.echo(f"  上游: {', '.join(r['upstream_tables']) or '（无）'}")
        for col, srcs in r["lineage"].items():
            click.echo(f"  列血缘: {col} ← {', '.join(srcs) or '（未解析）'}")


@cli.command("ls")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--json", "as_json", is_flag=True)
def ls_cmd(project_dir: str, as_json: bool) -> None:
    """列出模块与产物状态。"""
    cfg = load_config(project_dir)
    rows = []
    for m in cfg.modules:
        out = out_dir(cfg.root, m.name)
        rows.append({
            "module": m.name,
            "description": m.description,
            "entries": len(m.entries),
            "exported": (out / "manifest.json").exists(),
            "out": str(out),
        })
    if as_json:
        _json_out({"modules": rows, "index": (index_path(cfg.root)).exists()})
    else:
        if not rows:
            click.echo("project.yml 中没有模块。用 rs init --module ... 添加。")
        for r in rows:
            flag = "已导出" if r["exported"] else "未导出"
            click.echo(f"  {r['module']}  {r['description']}  "
                       f"({r['entries']} 入口, {flag})")


@cli.command("show")
@click.argument("module")
@click.argument("project_dir", default=".", type=click.Path(file_okay=False))
@click.option("--json", "as_json", is_flag=True)
def show_cmd(module: str, project_dir: str, as_json: bool) -> None:
    """查看模块包摘要。"""
    cfg = load_config(project_dir)
    out = out_dir(cfg.root, module)
    manifest_path = out / "manifest.json"
    if not manifest_path.exists():
        raise click.ClickException(f"模块 '{module}' 未导出，先运行 rs run {module}")
    import json as _json
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    if as_json:
        _json_out(manifest)
    else:
        click.echo(f"模块 '{module}': {manifest.get('description', '')}")
        click.echo(f"  入口 {manifest['stats']['entries']} / 文件 {manifest['stats']['files']} / "
                   f"符号 {manifest['stats']['symbols']}")
        for f in manifest["files"]:
            hit = len(f.get("symbols", []))
            click.echo(f"    {f['path']}  ({hit} 符号)")


@cli.command("mcp")
def mcp_cmd() -> None:
    """启动 MCP stdio 服务（供 opencode / Claude Code 接入）。"""
    from rs.mcp_server import serve_stdio
    serve_stdio()


def main() -> None:
    cli()
