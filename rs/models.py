"""核心 IR 数据模型。

所有中间产物都是版本化 JSON，阶段间只通过本模块定义的 schema 通信。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


SCHEMA_INDEX = "project-index/v1"
SCHEMA_ENTRY_POINTS = "entry-points/v1"
SCHEMA_MODULE_GRAPH = "module-graph/v1"
SCHEMA_MANIFEST = "module-manifest/v1"
SCHEMA_AUDIT = "audit-report/v1"
SCHEMA_ALGORITHM = "algorithm/v1"
SCHEMA_ALIGNMENT = "alignment-report/v1"


# ---------------------------------------------------------------- symbols

@dataclass
class Symbol:
    id: str                     # 唯一标识（qname + kind 消歧）
    kind: str                   # class|interface|enum|record|annotation|method|constructor|field
    qname: str                  # 全限定名，如 com.acme.report.ReportExportController
    simple: str
    file: str                   # 相对项目根的文件路径
    start_line: int
    end_line: int
    annotations: List[str] = field(default_factory=list)
    annotation_values: Dict[str, str] = field(default_factory=dict)   # 注解名 -> 首个字符串参数
    modifiers: List[str] = field(default_factory=list)
    owner: str = ""             # 所属类型 qname
    signature: str = ""         # 方法：返回类型 + 参数摘要
    return_type: str = ""
    field_type: str = ""
    params: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- edges

EDGE_CONTAINS = "contains"
EDGE_CALL = "call"
EDGE_TYPE_REF = "type_ref"
EDGE_FIELD_TYPE = "field_type"
EDGE_EXTENDS = "extends"
EDGE_IMPLEMENTS = "implements"
EDGE_ANNOTATION = "annotation"
EDGE_IMPORT = "import"


@dataclass
class Edge:
    src: str
    dst: str
    kind: str
    line: int = 0
    resolved: bool = True
    external: bool = False      # dst 是否为项目外类型（JDK/框架/三方库）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- files

@dataclass
class ImportRef:
    name: str
    line: int
    wildcard: bool = False
    static: bool = False


@dataclass
class SourceFile:
    path: str
    package: str = ""
    lines: int = 0
    imports: List[ImportRef] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)   # 该文件内的符号 id

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


# ---------------------------------------------------------------- entries

@dataclass
class EntryPoint:
    id: str
    type: str                   # http_endpoint|scheduled|listener|application_main|feign_client
    symbol: str                 # 入口方法/类 qname
    file: str
    line: int
    http_method: str = ""
    http_path: str = ""
    confidence: float = 0.9
    source: str = "spring"
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- modules

@dataclass
class ModuleSpec:
    name: str
    description: str = ""
    entries: List[Dict[str, Any]] = field(default_factory=list)
    resources: List[str] = field(default_factory=list)
    exclude_symbols: List[str] = field(default_factory=list)   # qname 前缀，排除不进闭包

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProjectConfig:
    root: str = "."
    language: str = "java"
    exclude_dirs: List[str] = field(default_factory=list)
    exclude_globs: List[str] = field(default_factory=list)
    modules: List[ModuleSpec] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- outputs

@dataclass
class ModuleGraph:
    name: str
    description: str = ""
    entries: List[Dict[str, Any]] = field(default_factory=list)
    seed_symbols: List[str] = field(default_factory=list)
    symbols: Dict[str, Symbol] = field(default_factory=dict)       # 闭包内符号
    files: List[str] = field(default_factory=list)                 # 命中文件
    edges: List[Dict[str, Any]] = field(default_factory=list)      # 闭包内边
    reachable_from: Dict[str, List[str]] = field(default_factory=dict)  # symbol -> entry ids
    external_refs: Dict[str, List[str]] = field(default_factory=dict)   # kind -> qname 列表
    unresolved_calls: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "entries": self.entries,
            "seed_symbols": self.seed_symbols,
            "symbols": {k: v.to_dict() for k, v in self.symbols.items()},
            "files": self.files,
            "edges": self.edges,
            "reachable_from": self.reachable_from,
            "external_refs": self.external_refs,
            "unresolved_calls": self.unresolved_calls,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModuleGraph":
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            entries=data.get("entries", []),
            seed_symbols=data.get("seed_symbols", []),
            symbols={k: Symbol(**v) for k, v in data.get("symbols", {}).items()},
            files=data.get("files", []),
            edges=data.get("edges", []),
            reachable_from=data.get("reachable_from", {}),
            external_refs=data.get("external_refs", {}),
            unresolved_calls=data.get("unresolved_calls", []),
        )
