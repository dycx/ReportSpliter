"""算法对齐：代码重建算法 vs 设计算法，输出差异 + 人工审核队列。"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

from rs.algorithm import flatten_algorithm_steps
from rs.models import SCHEMA_ALIGNMENT


MATCH_THRESHOLD = 0.28
REVIEW_THRESHOLD = 0.5


# 中英文概念同义词（面向报表/业务领域，可扩展）
SYNONYMS = {
    "日期": {"date", "day"}, "时间": {"date", "day"},
    "报表": {"report"}, "导出": {"export"}, "列表": {"list"},
    "读取": {"find", "load", "get", "read", "query"},
    "查询": {"find", "query", "list", "search"},
    "加载": {"load"}, "计算": {"compute", "calculate", "total"},
    "总额": {"total", "amount", "sum"}, "金额": {"amount", "total"},
    "渲染": {"render"}, "输出": {"output", "render", "write"},
    "校验": {"check", "verify", "validate"},
    "权限": {"permission", "auth", "user"},
    "指标": {"metric"}, "记录": {"record", "log", "metric"},
    "解析": {"parse"}, "参数": {"param", "date", "format"},
    "区间": {"between", "range", "start", "end"},
    "获取": {"get", "find", "fetch"}, "用户": {"user", "permission"},
    "报告": {"report"}, "生成": {"generate", "build", "compute"},
}

# 数据项别名（输入/输出名归一）
IO_ALIASES = {
    "day": {"date", "日"}, "date": {"day", "日期"},
    "report": {"base", "报表", "报告"}, "base": {"report", "报表"},
    "reports": {"list", "report"}, "format": {"格式"},
    "amount": {"total", "金额"}, "total": {"amount", "总额"},
    "computed": {"total", "result"}, "result": {"computed", "report"},
    "user": {"用户", "权限", "login"}, "allowed": {"permission", "check"},
    "name": {"指标", "metric"}, "status": {"状态"},
}


def tokenize(text: str) -> Set[str]:
    """标签分词：camelCase 拆分 + 中英文混合。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(text).lower())
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", s))


def label_tokens(text: str) -> Set[str]:
    base = tokenize(text)
    expanded = set(base)
    for t in base:
        expanded |= SYNONYMS.get(t, set())
    return expanded


def io_tokens(items) -> Set[str]:
    base = set()
    for i in items:
        s = str(i).lower()
        base.add(s)
        base |= tokenize(s)
    for t in list(base):
        base |= IO_ALIASES.get(t, set())
    return base


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dice(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def pair_score(design_step: dict, code_step: dict) -> float:
    label = _dice(label_tokens(design_step.get("label", "")),
                  label_tokens(code_step.get("label", "")))
    inputs = _jaccard(io_tokens(design_step.get("inputs", [])),
                      io_tokens(code_step.get("inputs", [])))
    outputs = _jaccard(io_tokens(design_step.get("outputs", [])),
                       io_tokens(code_step.get("outputs", [])))
    kind = 0.1 if design_step.get("kind") == code_step.get("kind") else 0.0
    return 0.5 * label + 0.2 * inputs + 0.2 * outputs + kind


def _assignment_max(weights) -> List[Tuple[int, int]]:
    """匈牙利算法：最大化矩形权重矩阵的指派，返回 (row, col) 列表。"""
    n, m = len(weights), len(weights[0])
    if n == 0 or m == 0:
        return []
    # 保证 n <= m，否则转置
    transposed = n > m
    if transposed:
        weights = [[weights[j][i] for j in range(n)] for i in range(m)]
        n, m = m, n
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = -weights[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    ans = [(p[j] - 1, j - 1) for j in range(1, m + 1) if p[j]]
    if transposed:
        ans = [(c, r) for r, c in ans]
    return ans


def _design_step_key(step: dict) -> str:
    return step.get("id") or step.get("label", "")


def load_design_spec(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"设计算法文件不存在: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not data or "steps" not in data:
        raise ValueError(f"{p} 缺少 steps 列表")
    data["_source"] = str(p)
    return data


def align_algorithms(algorithm: dict, design: dict, design_source: str = "") -> dict:
    """执行对齐。design: {steps: [{id,kind,label,inputs,outputs,note}]}"""
    design_steps = design.get("steps", [])
    all_code_steps = flatten_algorithm_steps(algorithm)
    # entry/detail 是重建的结构噪声，不参与匹配与审核
    code_steps = [s for s in all_code_steps
                  if s.get("kind") not in ("entry", "detail")]

    code_index = {s.get("id", ""): i for i, s in enumerate(code_steps)}

    # 全局最优指派
    weights = [
        [pair_score(d, c) for c in code_steps]
        for d in design_steps
    ]
    assignments = [(di, ci, pair_score(design_steps[di], code_steps[ci]))
                   for di, ci in _assignment_max(weights)
                   if pair_score(design_steps[di], code_steps[ci]) >= MATCH_THRESHOLD]
    assignments.sort(key=lambda x: -x[2])
    used_d = {di for di, _, _ in assignments}
    used_c = {ci for _, ci, _ in assignments}
    matched: List[dict] = []
    for di, ci, score in assignments:
        d, c = design_steps[di], code_steps[ci]
        d_in_raw, d_out_raw = set(d.get("inputs", [])), set(d.get("outputs", []))
        c_in_raw, c_out_raw = set(c.get("inputs", [])), set(c.get("outputs", []))
        d_in, d_out = io_tokens(d.get("inputs", [])), io_tokens(d.get("outputs", []))
        c_in, c_out = io_tokens(c.get("inputs", [])), io_tokens(c.get("outputs", []))
        matched.append({
            "design_id": _design_step_key(d),
            "design_label": d.get("label", ""),
            "design_index": di,
            "code_id": c.get("id", ""),
            "code_label": c.get("label", ""),
            "code_index": ci,
            "code_anchor": f"{c.get('file', '')}:{c.get('line', 0)}",
            "score": round(score, 3),
            "kind_same": d.get("kind") == c.get("kind"),
            "io_changed": {
                "inputs": sorted(d_in_raw - c_in_raw),
                "outputs": sorted(d_out_raw - c_out_raw),
            },
            "design_note": d.get("note", ""),
            "needs_review": score < REVIEW_THRESHOLD or d.get("kind") != c.get("kind"),
        })

    missing_in_code = [
        {**d, "design_id": _design_step_key(d)}
        for i, d in enumerate(design_steps) if i not in used_d
    ]
    added_in_code = [
        c for i, c in enumerate(code_steps) if i not in used_c
    ]

    # 顺序变化：按设计顺序看代码步骤出现次序是否递增
    order_notes: List[str] = []
    order_by_design = sorted(matched, key=lambda m: m["design_index"])
    code_positions = [m["code_index"] for m in order_by_design]
    order_changed = False
    for a, b in zip(code_positions, code_positions[1:]):
        if a > b:
            order_changed = True
            order_notes.append(f"设计顺序 {a+1} → {b+1} 在代码中逆序")

    review_queue: List[dict] = []
    for m in missing_in_code:
        review_queue.append({
            "priority": "high",
            "kind": "design_missing_in_code",
            "design_id": m["design_id"],
            "label": m.get("label", ""),
            "reason": "设计算法中的步骤在代码中未找到对应实现",
            "note": m.get("note", ""),
        })
    for c in added_in_code:
        review_queue.append({
            "priority": "medium" if c.get("confidence", 1) >= 0.8 else "high",
            "kind": "code_only",
            "code_id": c.get("id", ""),
            "label": c.get("label", ""),
            "anchor": f"{c.get('file', '')}:{c.get('line', 0)}",
            "reason": "代码中存在但设计算法中未定义（可能为未跟踪变动）",
        })
    for m in matched:
        if m["needs_review"]:
            review_queue.append({
                "priority": "medium",
                "kind": "changed_or_unclear",
                "design_id": m["design_id"],
                "code_id": m["code_id"],
                "label": f"{m['design_label']} ↔ {m['code_label']}",
                "score": m["score"],
                "reason": "匹配度低或类型/输入输出不一致，需人工核对",
                "note": m.get("design_note", ""),
            })

    report = {
        "schema": SCHEMA_ALIGNMENT,
        "module": algorithm.get("module", ""),
        "design_source": design_source or design.get("_source", ""),
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "matched": matched,
        "added_in_code": added_in_code,
        "missing_in_code": missing_in_code,
        "order_changed": order_changed,
        "order_notes": order_notes,
        "review_queue": review_queue,
        "stats": {
            "design_steps": len(design_steps),
            "code_steps": len(code_steps),
            "matched": len(matched),
            "added_in_code": len(added_in_code),
            "missing_in_code": len(missing_in_code),
            "needs_review": len(review_queue),
        },
    }
    return report


def write_alignment_md(path: Path, report: dict) -> None:
    lines = [f"# 算法对齐报告：{report['module']}",
             "", f"设计算法来源：{report.get('design_source', '-')}", ""]
    st = report["stats"]
    lines.append(f"设计步骤 {st['design_steps']} / 代码步骤 {st['code_steps']} / "
                 f"匹配 {st['matched']} / 代码新增 {st['added_in_code']} / "
                 f"设计缺失 {st['missing_in_code']} / 需人工审核 {st['needs_review']}")
    lines.append("")

    if report["matched"]:
        lines.append("## 匹配的步骤")
        lines.append("")
        lines.append("| 设计 | 代码 | 锚点 | 评分 | 类型一致 |")
        lines.append("|---|---|---|---|---|")
        for m in report["matched"]:
            flag = "✓" if m["kind_same"] else "✗"
            lines.append(f"| {m['design_label']} | {m['code_label']} | "
                         f"{m['code_anchor']} | {m['score']} | {flag} |")
        lines.append("")

    if report["missing_in_code"]:
        lines.append("## 设计中存在、代码中缺失")
        lines.append("")
        for m in report["missing_in_code"]:
            lines.append(f"- **{m.get('label')}**（{m['design_id']}）"
                         f"{' — ' + m.get('note', '') if m.get('note') else ''}")
        lines.append("")

    if report["added_in_code"]:
        lines.append("## 代码中存在、设计中缺失（可能的未跟踪变动）")
        lines.append("")
        for c in report["added_in_code"]:
            lines.append(f"- **{c.get('label')}** [{c.get('kind')}] "
                         f"{c.get('file', '')}:{c.get('line', 0)} — `{c.get('snippet', '')[:80]}`")
        lines.append("")

    if report["order_changed"]:
        lines.append("## 顺序变化")
        lines.append("")
        lines.append("匹配步骤的执行顺序与设计文档不一致，需人工确认。")
        lines.append("")

    if report["review_queue"]:
        lines.append("## 人工审核队列")
        lines.append("")
        for r in report["review_queue"]:
            lines.append(f"- [{r['priority']}] {r['kind']}: {r['label']} — {r['reason']}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
