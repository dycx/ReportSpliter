"""project.yml 的加载与默认值。"""

from __future__ import annotations

from pathlib import Path
from typing import List

import yaml

from rs.models import ModuleSpec, ProjectConfig


DEFAULT_EXCLUDE_DIRS = [
    ".git", ".svn", ".hg", "node_modules", ".venv", "venv",
    "target", "build", "dist", "out", ".report-spliter", ".idea", ".vscode",
    "__pycache__", "generated",
]

DEFAULT_EXCLUDE_GLOBS = [
    "**/generated/**",
    "**/target/**",
    "**/build/**",
]


def default_config(root: str) -> ProjectConfig:
    return ProjectConfig(
        root=root,
        language="java",
        exclude_dirs=list(DEFAULT_EXCLUDE_DIRS),
        exclude_globs=list(DEFAULT_EXCLUDE_GLOBS),
        modules=[],
    )


def load_config(project_dir: str | Path) -> ProjectConfig:
    """加载 project.yml；不存在时返回默认配置（并提示）。"""
    cfg_path = Path(project_dir) / "project.yml"
    if not cfg_path.exists():
        return default_config(str(Path(project_dir).resolve()))

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not raw:
        return default_config(str(Path(project_dir).resolve()))

    root = str(Path(project_dir).resolve())
    project = raw.get("project", {})
    exclude_dirs = project.get("exclude_dirs") or list(DEFAULT_EXCLUDE_DIRS)
    exclude_globs = project.get("exclude_globs") or list(DEFAULT_EXCLUDE_GLOBS)

    modules = []
    for m in raw.get("modules", []) or []:
        modules.append(ModuleSpec(
            name=m.get("name", ""),
            description=m.get("description", ""),
            entries=m.get("entries", []) or [],
            resources=m.get("resources", []) or [],
            exclude_symbols=m.get("exclude_symbols", []) or [],
        ))

    return ProjectConfig(
        root=root,
        language=project.get("language", "java"),
        exclude_dirs=exclude_dirs,
        exclude_globs=exclude_globs,
        modules=modules,
    )


def save_config(cfg: ProjectConfig, project_dir: str | Path) -> Path:
    path = Path(project_dir) / "project.yml"
    path.write_text(yaml.safe_dump(cfg.to_dict(), allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


def write_template(project_dir: str | Path, modules: List[ModuleSpec] | None = None) -> Path:
    """生成 project.yml 模板（含注释示例）。"""
    cfg = default_config(str(Path(project_dir).resolve()))
    header = f"""# ReportSpliter 项目配置
# 字段说明见 docs/architecture-redesign.md §1.3
project:
  root: {Path(project_dir).resolve()}
  language: java
  exclude_dirs: {DEFAULT_EXCLUDE_DIRS}
  exclude_globs: {DEFAULT_EXCLUDE_GLOBS}

"""
    if modules:
        cfg.modules = modules
        body = yaml.safe_dump({"modules": [m.to_dict() for m in modules]},
                              allow_unicode=True, sort_keys=False)
        template = header + body
    else:
        template = header + """# 模块示例（取消注释并按需修改）：
# modules:
#   - name: report-export
#     description: 报表导出功能
#     entries:
#       - type: http_endpoint
#         path: /api/reports
#     resources:
#       - src/main/resources/**
"""

    path = Path(project_dir) / "project.yml"
    path.write_text(template, encoding="utf-8")
    return path
