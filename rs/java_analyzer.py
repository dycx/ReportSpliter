"""基于 tree-sitter 的 Java 源码分析器。

把单个 .java 文件解析为：符号（类型/方法/构造器/字段）、边（调用/类型引用/
继承/注解/包含），以及方法体内的作用域信息，供调用解析使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from tree_sitter import Language, Parser, Node
import tree_sitter_java

from rs.models import Edge, ImportRef, SourceFile, Symbol


_LANG = Language(tree_sitter_java.language())
_PARSER = Parser(_LANG)


def _text(node: Optional[Node]) -> str:
    return node.text.decode() if node is not None else ""


def _field(node: Optional[Node], name: str) -> Optional[Node]:
    return node.child_by_field_name(name) if node is not None else None


def _child(node: Optional[Node], node_type: str) -> Optional[Node]:
    """按子节点类型查找（用于没有 field 名的节点，如 modifiers/package/import）。"""
    if node is None:
        return None
    for c in node.children:
        if c.type == node_type:
            return c
    return None


def _named_children(node: Node) -> List[Node]:
    return [c for c in node.children if c.is_named]


def _annotation_name(node: Node) -> str:
    name = _field(node, "name")
    if name is not None:
        return name.text.decode()
    # 兜底：取第一个 identifier
    for c in _named_children(node):
        if c.type == "identifier":
            return c.text.decode()
    return node.text.decode().lstrip("@")


def _annotation_value(node: Node) -> str:
    """提取注解第一个字符串字面量（用于路由 path）。"""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "string_literal":
            for c in n.children:
                if c.is_named:
                    return c.text.decode()
            return n.text.decode().strip("\"'")
        stack.extend(reversed(_named_children(n)))
    return ""


def _type_text(node: Optional[Node]) -> str:
    return _text(node).replace(" ", "")


def _param_key(params: List[Dict[str, str]]) -> str:
    return ",".join(p["type"] for p in params)


@dataclass
class ParsedFile:
    file: SourceFile
    symbols: List[Symbol]
    edges: List[Edge]


class JavaFileAnalyzer:
    """解析单个 Java 文件。"""

    TYPE_KINDS = {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "record",
        "annotation_type_declaration": "annotation",
    }

    # 常见框架/JDK 前缀（用于把明显的外部引用尽早标记，避免误当项目符号）
    EXTERNAL_PREFIXES = (
        "java.", "javax.", "jdk.", "sun.", "org.", "com.fasterxml.",
        "com.google.", "io.", "lombok.", "ch.qos.", "jakarta.",
        "org.apache.", "org.slf4j.", "org.springframework.",
    )

    def __init__(self, rel_path: str, source: bytes):
        self.rel_path = rel_path
        self.tree = _PARSER.parse(source)
        self.source_lines = source.decode("utf-8", errors="replace").splitlines()

        self.package = ""
        self.import_map: Dict[str, str] = {}       # 简单名 -> 全限定名
        self.wildcard_imports: List[str] = []
        self.static_imports: List[str] = []

        self.symbols: List[Symbol] = []
        self.edges: List[Edge] = []
        self.file = SourceFile(path=rel_path, lines=len(self.source_lines))

        # 用于方法内调用解析的作用域缓存
        self._type_scopes: Dict[str, Dict[str, str]] = {}   # method_id -> var -> type_simple
        self._method_return_types: Dict[str, str] = {}      # method_qname -> return type simple
        self._class_fields: Dict[str, Dict[str, str]] = {}  # type_qname -> field -> type simple

    # ------------------------------------------------------------- 入口

    def analyze(self) -> ParsedFile:
        root = self.tree.root_node
        for child in _named_children(root):
            if child.type == "package_declaration":
                self._parse_package(child)
            elif child.type == "import_declaration":
                self._parse_import(child)

        for child in _named_children(root):
            if child.type in self.TYPE_KINDS:
                self._parse_type(child, owner_qname="", owner_id="")

        self.file.symbols = [s.id for s in self.symbols]
        self.file.imports = self._build_import_refs()
        return ParsedFile(file=self.file, symbols=self.symbols, edges=self.edges)

    # ------------------------------------------------------------- 基础解析

    def _parse_package(self, node: Node) -> None:
        sid = _child(node, "scoped_identifier") or _child(node, "identifier")
        self.package = _text(sid)
        self.file.package = self.package

    def _parse_import(self, node: Node) -> None:
        sid = _child(node, "scoped_identifier") or _child(node, "identifier")
        name = _text(sid)
        wildcard = any(c.type == "asterisk" for c in node.children)
        static = any(c.type == "static" for c in node.children)
        line = node.start_point[0] + 1
        if wildcard:
            self.wildcard_imports.append(name)
            if static:
                self.static_imports.append(name)
            return
        if static:
            self.static_imports.append(name)
            return
        simple = name.rsplit(".", 1)[-1]
        self.import_map[simple] = name
        self.edges.append(Edge(src=self.rel_path, dst=name, kind="import", line=line))

    def _build_import_refs(self) -> List[ImportRef]:
        refs = []
        for n, line in self._import_lines():
            refs.append(ImportRef(name=n, line=line, wildcard=n.endswith(".*"), static=n in self.static_imports))
        return refs

    def _import_lines(self) -> List[Tuple[str, int]]:
        out = []
        for e in self.edges:
            if e.kind == "import":
                out.append((e.dst, e.line))
        return out

    # ------------------------------------------------------------- 类型

    def _parse_type(self, node: Node, owner_qname: str, owner_id: str) -> None:
        kind = self.TYPE_KINDS[node.type]
        name_node = _field(node, "name")
        simple = _text(name_node)
        qname = f"{owner_qname}.{simple}" if owner_qname else f"{self.package}.{simple}" if self.package else simple
        if not simple:
            return

        annotations, annotation_values, modifiers = self._parse_modifiers(_child(node, "modifiers"))
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        sym = Symbol(
            id=qname,
            kind=kind,
            qname=qname,
            simple=simple,
            file=self.rel_path,
            start_line=start,
            end_line=end,
            annotations=annotations,
            annotation_values=annotation_values,
            modifiers=modifiers,
            owner=owner_qname,
            signature=f"{kind} {simple}",
        )
        self.symbols.append(sym)
        if owner_id:
            self.edges.append(Edge(src=owner_id, dst=qname, kind="contains", line=start))

        # 继承/实现
        for clause in ("extends_clause", "extends_interfaces"):
            ext = _child(node, clause)
            if ext is not None:
                for t in self._collect_types(ext):
                    for tq in self._resolve_type(t):
                        self.edges.append(Edge(src=qname, dst=tq, kind="extends", line=start))
        impl = _child(node, "implements_clause")
        if impl is not None:
            for t in self._collect_types(impl):
                for tq in self._resolve_type(t):
                    self.edges.append(Edge(src=qname, dst=tq, kind="implements", line=start))

        # 注解边
        for a in annotations:
            for aq in self._resolve_type_by_name(a):
                self.edges.append(Edge(src=qname, dst=aq, kind="annotation", line=start))

        self._class_fields.setdefault(qname, {})

        body = _field(node, "body")
        if body is not None:
            self._parse_class_body(body, qname, qname)

    def _parse_class_body(self, body: Node, type_qname: str, type_id: str) -> None:
        for child in _named_children(body):
            if child.type in self.TYPE_KINDS:
                self._parse_type(child, owner_qname=type_qname, owner_id=type_id)
            elif child.type == "method_declaration":
                self._parse_method(child, type_qname, type_id)
            elif child.type == "constructor_declaration":
                self._parse_constructor(child, type_qname, type_id)
            elif child.type == "field_declaration":
                self._parse_field(child, type_qname, type_id)

    # ------------------------------------------------------------- 方法

    def _parse_method(self, node: Node, type_qname: str, type_id: str) -> None:
        annotations, annotation_values, modifiers = self._parse_modifiers(_child(node, "modifiers"))
        name = _text(_field(node, "name"))
        ret_type = _type_text(_field(node, "type"))
        params = self._parse_params(_field(node, "parameters"))
        sig = f"{name}({_param_key(params)})"
        qname = f"{type_qname}.{name}"
        mid = f"{qname}{{{sig}}}"

        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        sym = Symbol(
            id=mid,
            kind="method",
            qname=qname,
            simple=name,
            file=self.rel_path,
            start_line=start,
            end_line=end,
            annotations=annotations,
            annotation_values=annotation_values,
            modifiers=modifiers,
            owner=type_qname,
            signature=sig,
            return_type=ret_type,
            params=params,
        )
        self.symbols.append(sym)
        self.edges.append(Edge(src=type_id, dst=mid, kind="contains", line=start))
        self._method_return_types[mid] = ret_type.split("<")[0].split(".")[-1]

        # 返回类型/参数类型引用
        self._add_type_refs_from(mid, _field(node, "type"), start)
        for p in params:
            self._add_type_refs_from(mid, None, start, type_text=p["type_raw"])

        for a in annotations:
            for aq in self._resolve_type_by_name(a):
                self.edges.append(Edge(src=mid, dst=aq, kind="annotation", line=start))

        body = _field(node, "body")
        if body is not None:
            self._analyze_body(body, mid, type_qname, params)

    def _parse_constructor(self, node: Node, type_qname: str, type_id: str) -> None:
        annotations, annotation_values, modifiers = self._parse_modifiers(_child(node, "modifiers"))
        params = self._parse_params(_field(node, "parameters"))
        sig = f"<init>({_param_key(params)})"
        qname = f"{type_qname}.<init>"
        mid = f"{qname}{{{sig}}}"

        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        sym = Symbol(
            id=mid,
            kind="constructor",
            qname=qname,
            simple="<init>",
            file=self.rel_path,
            start_line=start,
            end_line=end,
            annotations=annotations,
            annotation_values=annotation_values,
            modifiers=modifiers,
            owner=type_qname,
            signature=sig,
            return_type="",
            params=params,
        )
        self.symbols.append(sym)
        self.edges.append(Edge(src=type_id, dst=mid, kind="contains", line=start))
        self._method_return_types[mid] = type_qname.rsplit(".", 1)[-1]

        for p in params:
            self._add_type_refs_from(mid, None, start, type_text=p["type_raw"])
        for a in annotations:
            for aq in self._resolve_type_by_name(a):
                self.edges.append(Edge(src=mid, dst=aq, kind="annotation", line=start))

        body = _field(node, "body")
        if body is not None:
            self._analyze_body(body, mid, type_qname, params)

    def _parse_params(self, params_node: Optional[Node]) -> List[Dict[str, str]]:
        params = []
        if params_node is None:
            return params
        for fp in _named_children(params_node):
            if fp.type != "formal_parameter":
                continue
            t = _field(fp, "type")
            n = _field(fp, "name")
            type_raw = _type_text(t)
            type_simple = self._simple_of_type(t)
            params.append({
                "name": _text(n),
                "type": type_simple,
                "type_raw": type_raw,
            })
        return params

    def _simple_of_type(self, type_node: Optional[Node]) -> str:
        if type_node is None:
            return ""
        txt = _type_text(type_node)
        if "<" in txt:
            return txt.split("<")[0].rsplit(".", 1)[-1]
        return txt.rsplit(".", 1)[-1]

    # ------------------------------------------------------------- 字段

    def _parse_field(self, node: Node, type_qname: str, type_id: str) -> None:
        annotations, annotation_values, modifiers = self._parse_modifiers(_child(node, "modifiers"))
        type_node = _field(node, "type")
        field_type_raw = _type_text(type_node)
        field_type_simple = self._simple_of_type(type_node)
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1

        declarators = []
        for c in _named_children(node):
            if c.type == "variable_declarator":
                declarators.append(_text(_field(c, "name")))

        for name in declarators:
            fid = f"{type_qname}.{name}"
            sym = Symbol(
                id=fid,
                kind="field",
                qname=fid,
                simple=name,
                file=self.rel_path,
                start_line=start,
                end_line=end,
                annotations=annotations,
                annotation_values=annotation_values,
                modifiers=modifiers,
                owner=type_qname,
                field_type=field_type_raw,
                signature=f"{field_type_raw} {name}",
            )
            self.symbols.append(sym)
            self.edges.append(Edge(src=type_id, dst=fid, kind="contains", line=start))
            self._class_fields.setdefault(type_qname, {})[name] = field_type_simple
            self._add_type_refs_from(fid, type_node, start)
            for a in annotations:
                for aq in self._resolve_type_by_name(a):
                    self.edges.append(Edge(src=fid, dst=aq, kind="annotation", line=start))

    # ------------------------------------------------------------- 方法体

    def _analyze_body(self, body: Node, method_id: str, type_qname: str,
                      params: List[Dict[str, str]]) -> None:
        scope: Dict[str, str] = {p["name"]: p["type"] for p in params}
        # 局部变量
        self._collect_local_vars(body, scope, method_id)
        # 字段名也可作为调用接收者
        for fname, ftype in self._class_fields.get(type_qname, {}).items():
            scope.setdefault(fname, ftype)

        self._scan_calls(body, method_id, type_qname, scope)

    def _collect_local_vars(self, node: Node, scope: Dict[str, str], method_id: str) -> None:
        for child in _named_children(node):
            if child.type == "local_variable_declaration":
                type_node = _field(child, "type")
                tsimple = self._simple_of_type(type_node)
                self._add_type_refs_from(method_id, type_node, child.start_point[0] + 1)
                for c in _named_children(child):
                    if c.type == "variable_declarator":
                        vname = _text(_field(c, "name"))
                        scope[vname] = tsimple
            elif child.type in ("for_statement", "enhanced_for_statement", "try_statement",
                                "catch_clause", "lambda_expression", "if_statement",
                                "switch_expression", "block"):
                self._collect_local_vars(child, scope, method_id)

    def _scan_calls(self, node: Node, method_id: str, type_qname: str,
                    scope: Dict[str, str]) -> None:
        for child in _named_children(node):
            if child.type == "method_invocation":
                self._handle_invocation(child, method_id, type_qname, scope)
                # 嵌套调用（参数中的调用）也要收集
                self._scan_calls(child, method_id, type_qname, scope)
            elif child.type == "object_creation_expression":
                self._handle_object_creation(child, method_id)
                self._scan_calls(child, method_id, type_qname, scope)
            else:
                self._scan_calls(child, method_id, type_qname, scope)

    def _handle_invocation(self, node: Node, method_id: str, type_qname: str,
                           scope: Dict[str, str]) -> None:
        name = _text(_field(node, "name"))
        line = node.start_point[0] + 1
        obj = _field(node, "object")
        receiver_qnames: List[str] = []
        if obj is None:
            receiver_qnames.append(type_qname)
        else:
            receiver_qnames = self._receiver_qnames(obj, scope, type_qname)

        if not receiver_qnames:
            # 无法解析接收者：记录未解析调用
            self.edges.append(Edge(src=method_id, dst=f"{name}", kind="call",
                                   line=line, resolved=False))
            return

        for rq in receiver_qnames:
            callee = f"{rq}.{name}"
            self.edges.append(Edge(src=method_id, dst=callee, kind="call", line=line))

    def _handle_object_creation(self, node: Node, method_id: str) -> None:
        type_node = _field(node, "type")
        line = node.start_point[0] + 1
        if type_node is None:
            return
        for tq in self._resolve_type(type_node):
            self.edges.append(Edge(src=method_id, dst=tq, kind="call", line=line))
            self.edges.append(Edge(src=method_id, dst=tq, kind="type_ref", line=line))
        # 泛型参数里的类型也做引用
        self._add_type_refs_from(method_id, type_node, line)

    def _receiver_qnames(self, obj: Node, scope: Dict[str, str], type_qname: str) -> List[str]:
        """尽力解析调用接收者类型，返回候选全限定名列表。"""
        if obj.type == "identifier":
            name = obj.text.decode()
            if name == "this":
                return [type_qname]
            tsimple = scope.get(name)
            if tsimple:
                return self._resolve_type_by_name(tsimple)
            # 类型静态调用，如 DateUtils.format(...)
            return self._resolve_type_by_name(name)
        if obj.type == "field_access":
            # this.service / a.b 链：取最后一跳
            fname = _text(_field(obj, "field"))
            tsimple = scope.get(fname)
            if tsimple:
                return self._resolve_type_by_name(tsimple)
            inner = _field(obj, "object")
            if inner is not None and inner.type == "identifier":
                iname = inner.text.decode()
                if iname == "this":
                    # 类内字段：已在 scope 中；若没有，则无法解析
                    return []
            return []
        if obj.type == "method_invocation":
            # 链式调用：a.b().c() —— 依赖方法返回类型，v1 不做跨类推断
            return []
        return []

    # ------------------------------------------------------------- 类型解析

    def _resolve_type(self, type_node: Optional[Node]) -> List[str]:
        """把一个类型节点解析为候选全限定名。"""
        if type_node is None:
            return []
        t = type_node.type
        if t == "type_identifier":
            return self._resolve_type_by_name(type_node.text.decode())
        if t == "scoped_type_identifier":
            return [type_node.text.decode().replace(" ", "")]
        out: List[str] = []
        for c in _named_children(type_node):
            if c.type == "type_identifier":
                out.extend(self._resolve_type_by_name(c.text.decode()))
            elif c.type == "scoped_type_identifier":
                out.append(c.text.decode().replace(" ", ""))
            elif c.type in ("generic_type", "array_type", "union_type", "intersection_type",
                            "annotated_type", "type_identifier"):
                out.extend(self._resolve_type(c))
            elif c.type == "identifier":
                out.extend(self._resolve_type_by_name(c.text.decode()))
        return out

    def _resolve_type_by_name(self, simple: str) -> List[str]:
        """简单类型名 -> 候选全限定名（import / 同包 / java.lang）。"""
        simple = simple.strip().split("<")[0].split(".")[-1]
        if not simple:
            return []
        if simple in self.import_map:
            return [self.import_map[simple]]
        out = []
        if self.package:
            out.append(f"{self.package}.{simple}")
        out.append(f"java.lang.{simple}")
        return out

    def _add_type_refs_from(self, src_id: str, type_node: Optional[Node],
                            line: int, type_text: str = "") -> None:
        if type_node is not None:
            for tq in self._resolve_type(type_node):
                self.edges.append(Edge(src=src_id, dst=tq, kind="type_ref", line=line))
            return
        # 兜底：直接按文本中的类型名解析
        if type_text:
            for seg in type_text.split(","):
                seg = seg.strip().split("<")[0].strip()
                if not seg:
                    continue
                for tq in self._resolve_type_by_name(seg):
                    self.edges.append(Edge(src=src_id, dst=tq, kind="type_ref", line=line))

    def _collect_types(self, clause: Optional[Node]) -> List[Node]:
        if clause is None:
            return []
        out = []
        for c in _named_children(clause):
            if c.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
                out.append(c)
            elif c.type in ("type_list",):
                out.extend(self._collect_types(c))
        return out

    def _parse_modifiers(self, mods: Optional[Node]) -> Tuple[List[str], Dict[str, str], List[str]]:
        annotations = []
        annotation_values = {}
        modifiers = []
        if mods is None:
            return annotations, annotation_values, modifiers
        for c in _named_children(mods):
            if c.type in ("annotation", "marker_annotation"):
                name = _annotation_name(c)
                annotations.append(name)
                annotation_values[name] = _annotation_value(c)
            elif c.type in ("public", "private", "protected", "static", "final",
                            "abstract", "synchronized", "native", "transient",
                            "volatile", "default", "sealed", "non-sealed"):
                modifiers.append(c.type)
        return annotations, annotation_values, modifiers


def analyze_java_file(rel_path: str, source: bytes) -> ParsedFile:
    return JavaFileAnalyzer(rel_path, source).analyze()
