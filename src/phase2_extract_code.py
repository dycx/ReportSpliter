#!/usr/bin/env python3
"""
Phase 2: Code Extraction
根据切片结果（类名+行号），从源码中提取相关文件。
"""

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, Set


def load_slice_result(slice_file: str) -> Dict[str, Set[int]]:
    """加载切片结果"""
    with open(slice_file, 'r') as f:
        data = json.load(f)

    # 支持两种格式：
    # 1. {"ClassName": [line1, line2, ...]}
    # 2. {"entry_points": [...], "slice_result": {"ClassName": [...]}}
    if "slice_result" in data:
        raw = data["slice_result"]
    else:
        raw = data

    result = {}
    for class_name, lines in raw.items():
        result[class_name] = set(lines) if isinstance(lines, list) else lines
    return result


def jvm_class_to_path(class_name: str) -> str:
    """将 JVM 类名转换为文件路径
    例如: Lcom/yourcompany/service/ReportService -> com/yourcompany/service/ReportService.java
    """
    path = class_name.lstrip('L').replace('/', os.sep)
    if not path.endswith('.java') and not path.endswith('.scala'):
        path += '.java'
    return path


def python_module_to_path(module_name: str) -> str:
    """将 Python 模块名转换为文件路径
    例如: my_package.my_module -> my_package/my_module.py
    """
    return module_name.replace('.', os.sep) + '.py'


def cpp_class_to_path(class_name: str) -> str:
    """将 C++ 类名转换为可能的文件路径"""
    # C++ 类名格式不确定，返回可能的路径列表
    parts = class_name.split('::')
    return os.path.join(*parts[:-1], parts[-1] + '.h') if len(parts) > 1 else parts[0] + '.h'


def find_source_file(
    class_name: str,
    source_root: str,
    language: str = "java"
) -> str:
    """查找源文件路径"""
    if language == "java" or language == "scala":
        rel_path = jvm_class_to_path(class_name)
    elif language == "python":
        rel_path = python_module_to_path(class_name)
    elif language == "cpp":
        rel_path = cpp_class_to_path(class_name)
    else:
        rel_path = class_name

    full_path = os.path.join(source_root, rel_path)
    if os.path.exists(full_path):
        return full_path

    # 尝试模糊匹配
    base_name = os.path.basename(rel_path)
    for root, dirs, files in os.walk(source_root):
        if base_name in files:
            return os.path.join(root, base_name)

    return None


def extract_files(
    slice_result: Dict[str, Set[int]],
    source_root: str,
    export_root: str,
    language: str = "java",
    copy_full_file: bool = True
):
    """提取切片涉及的源文件

    Args:
        slice_result: 切片结果 {类名: 行号集合}
        source_root: 源码根目录
        export_root: 导出目录
        language: 编程语言
        copy_full_file: True=复制整个文件, False=只提取相关行
    """
    extracted = []
    warnings = []

    for class_name, lines in slice_result.items():
        src_file = find_source_file(class_name, source_root, language)

        if src_file is None:
            warnings.append(f"Source not found: {class_name}")
            continue

        # 计算目标路径
        rel_path = os.path.relpath(src_file, source_root)
        dest_file = os.path.join(export_root, rel_path)

        # 创建目标目录
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)

        if copy_full_file:
            # 复制整个文件（保留 import 和类结构）
            shutil.copy2(src_file, dest_file)
            extracted.append({
                "source": rel_path,
                "lines_hit": len(lines),
                "mode": "full_file"
            })
        else:
            # 只提取相关行（注意：可能破坏代码结构）
            with open(src_file, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()

            # 收集需要的行（包含上下文）
            needed_lines = set()
            for line_no in lines:
                # 添加上下文：前后各2行
                for offset in range(-2, 3):
                    adj_line = line_no + offset
                    if 1 <= adj_line <= len(all_lines):
                        needed_lines.add(adj_line)

            # 写入提取的行
            with open(dest_file, 'w', encoding='utf-8') as f:
                for i in sorted(needed_lines):
                    f.write(all_lines[i - 1])

            extracted.append({
                "source": rel_path,
                "lines_hit": len(lines),
                "lines_extracted": len(needed_lines),
                "mode": "partial"
            })

        print(f"Extracted: {rel_path} ({len(lines)} lines hit)")

    # 打印警告
    for w in warnings:
        print(f"Warning: {w}")

    return extracted


def export_report(extracted, output_path):
    """导出提取报告"""
    report = {
        "total_files": len(extracted),
        "files": extracted,
    }
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved to {output_path}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phase 2: Code Extraction")
    parser.add_argument("--slice", required=True, help="切片结果 JSON 文件")
    parser.add_argument("--source-root", required=True, help="源码根目录")
    parser.add_argument("--output", default="./output/extracted_module", help="导出目录")
    parser.add_argument("--language", default="java", choices=["java", "scala", "python", "cpp"])
    parser.add_argument("--partial", action="store_true", help="只提取相关行（默认复制整个文件）")
    parser.add_argument("--report", default="extraction_report.json", help="提取报告路径")

    args = parser.parse_args()

    # 加载切片结果
    slice_result = load_slice_result(args.slice)
    print(f"Loaded slice result: {len(slice_result)} classes")

    # 提取文件
    extracted = extract_files(
        slice_result=slice_result,
        source_root=args.source_root,
        export_root=args.output,
        language=args.language,
        copy_full_file=not args.partial,
    )

    # 导出报告
    export_report(extracted, args.report)

    print(f"\nExtraction complete: {len(extracted)} files exported to {args.output}")


if __name__ == "__main__":
    main()
