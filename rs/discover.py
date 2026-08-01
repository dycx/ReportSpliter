"""Spring 入口发现：HTTP 端点 / 定时任务 / 监听器 / 应用入口 / Feign 边界。"""

from __future__ import annotations

from typing import Dict, List

from rs.indexer import ProjectIndex
from rs.models import EntryPoint, Symbol


CONTROLLER_ANNOTATIONS = {"RestController", "Controller"}
ROUTE_ANNOTATIONS = {
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping",
    "DeleteMapping", "PatchMapping", "HeadMapping", "OptionsMapping",
}
HTTP_METHOD_OF = {
    "RequestMapping": "", "GetMapping": "GET", "PostMapping": "POST",
    "PutMapping": "PUT", "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
    "HeadMapping": "HEAD", "OptionsMapping": "OPTIONS",
}
SCHEDULED_ANNOTATIONS = {"Scheduled"}
LISTENER_ANNOTATIONS = {
    "EventListener", "KafkaListener", "RabbitListener", "JmsListener",
    "RocketMQMessageListener", "StreamListener",
}
FEIGN_ANNOTATIONS = {"FeignClient"}


def _join_path(base: str, sub: str) -> str:
    base = base.strip("/")
    sub = sub.strip("/")
    if not base:
        return "/" + sub if sub else ""
    if not sub:
        return "/" + base
    return "/" + base + "/" + sub


def _entry_id(ep_type: str, http_method: str, http_path: str, symbol: str) -> str:
    if ep_type == "http_endpoint":
        return f"http:{http_method or 'ANY'}:{http_path or '/'}:{symbol}"
    return f"{ep_type}:{symbol}"


class EntryDiscoverer:
    def __init__(self, index: ProjectIndex):
        self.index = index

    def run(self) -> List[EntryPoint]:
        entries: List[EntryPoint] = []
        by_qname: Dict[str, Symbol] = {}
        methods: Dict[str, List[Symbol]] = {}
        for s in self.index.symbols.values():
            by_qname[s.qname] = s
            if s.kind in ("method", "constructor"):
                methods.setdefault(s.owner, []).append(s)

        for s in self.index.symbols.values():
            if s.kind == "class" and ("RestController" in s.annotations or "Controller" in s.annotations):
                self._controller_entries(s, methods.get(s.qname, []), entries)
            elif s.kind == "interface" and any(a in FEIGN_ANNOTATIONS for a in s.annotations):
                entries.append(EntryPoint(
                    id=_entry_id("feign_client", "", "", s.qname),
                    type="feign_client",
                    symbol=s.qname,
                    file=s.file,
                    line=s.start_line,
                    label=f"Feign 客户端 {s.qname}",
                    confidence=0.95,
                ))

        # main / 定时任务 / 监听器
        for s in self.index.symbols.values():
            if s.kind != "method":
                continue
            if s.simple == "main" and "static" in s.modifiers:
                entries.append(EntryPoint(
                    id=_entry_id("application_main", "", "", s.id),
                    type="application_main",
                    symbol=s.id,
                    file=s.file,
                    line=s.start_line,
                    label=f"应用入口 {s.qname}",
                    confidence=0.98,
                ))
            if any(a in SCHEDULED_ANNOTATIONS for a in s.annotations):
                entries.append(EntryPoint(
                    id=_entry_id("scheduled", "", "", s.id),
                    type="scheduled",
                    symbol=s.id,
                    file=s.file,
                    line=s.start_line,
                    label=f"定时任务 {s.qname}",
                    confidence=0.95,
                ))
            if any(a in LISTENER_ANNOTATIONS for a in s.annotations):
                entries.append(EntryPoint(
                    id=_entry_id("listener", "", "", s.id),
                    type="listener",
                    symbol=s.id,
                    file=s.file,
                    line=s.start_line,
                    label=f"监听器 {s.qname}",
                    confidence=0.95,
                ))
        return entries

    def _controller_entries(self, cls: Symbol, methods: List[Symbol],
                            entries: List[EntryPoint]) -> None:
        class_path = cls.annotation_values.get("RequestMapping", "").strip("\"'")
        for m in methods:
            route = [a for a in m.annotations if a in ROUTE_ANNOTATIONS]
            if not route:
                continue
            ann = route[0]
            method_path = m.annotation_values.get(ann, "").strip("\"'")
            path = _join_path(class_path, method_path)
            entries.append(EntryPoint(
                id=_entry_id("http_endpoint", HTTP_METHOD_OF.get(ann, ""), path, m.id),
                type="http_endpoint",
                symbol=m.id,
                file=m.file,
                line=m.start_line,
                http_method=HTTP_METHOD_OF.get(ann, ""),
                http_path=path,
                label=f"HTTP {HTTP_METHOD_OF.get(ann, 'ANY')} {path}",
                confidence=0.97,
            ))


def discover_entries(index: ProjectIndex) -> List[EntryPoint]:
    return EntryDiscoverer(index).run()

