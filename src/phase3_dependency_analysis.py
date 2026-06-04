#!/usr/bin/env python3
"""
Phase 3: Dependency Analysis
从构建文件和切片结果中提取最小依赖清单。
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Set, Tuple
from dataclasses import dataclass, asdict


@dataclass
class Dependency:
    name: str
    version: str = ""
    source: str = ""  # pom.xml, requirements.txt, CMakeLists.txt
    is_direct: bool = True
    is_mock_needed: bool = False


class DependencyAnalyzer:
    """分析代码依赖"""

    # Python 标准库模块（部分）
    PYTHON_STDLIB = {
        'os', 'sys', 'json', 're', 'collections', 'pathlib', 'datetime',
        'time', 'math', 'random', 'string', 'io', 'typing', 'abc',
        'functools', 'itertools', 'copy', 'pickle', 'csv', 'logging',
        'argparse', 'subprocess', 'threading', 'multiprocessing', 'socket',
        'http', 'urllib', 'email', 'html', 'xml', 'sqlite3', 'hashlib',
        'base64', 'struct', 'array', 'queue', 'heapq', 'bisect',
        'decimal', 'fractions', 'statistics', 'enum', 'dataclasses',
        'contextlib', 'weakref', 'types', 'operator', 'inspect',
    }

    # Java 标准库前缀
    JAVA_STDLIB = {
        'java.', 'javax.', 'sun.', 'com.sun.',
    }

    # C++ 标准库
    CPP_STDLIB = {
        'iostream', 'fstream', 'sstream', 'string', 'vector', 'map',
        'set', 'list', 'queue', 'stack', 'deque', 'algorithm', 'memory',
        'functional', 'utility', 'tuple', 'array', 'chrono', 'thread',
        'mutex', 'condition_variable', 'atomic', 'filesystem', 'regex',
        'cmath', 'cstdlib', 'cstdio', 'cstring', 'cassert',
    }

    def __init__(self, code_dir: str):
        self.code_dir = Path(code_dir)
        self.imports: Set[str] = set()
        self.build_deps: Set[str] = set()
        self.missing_deps: Set[str] = set()

    def extract_python_imports(self) -> Set[str]:
        """提取 Python 代码中的 import"""
        imports = set()
        for py_file in self.code_dir.rglob("*.py"):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        # import xxx
                        m = re.match(r'^import\s+(\S+)', line)
                        if m:
                            imports.add(m.group(1).split('.')[0])
                        # from xxx import yyy
                        m = re.match(r'^from\s+(\S+)\s+import', line)
                        if m:
                            imports.add(m.group(1).split('.')[0])
            except Exception:
                continue
        return imports

    def extract_java_imports(self) -> Set[str]:
        """提取 Java 代码中的 import"""
        imports = set()
        for java_file in self.code_dir.rglob("*.java"):
            try:
                with open(java_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        m = re.match(r'^import\s+([\w.]+)\s*;', line.strip())
                        if m:
                            # 取包名的前两段作为依赖标识
                            parts = m.group(1).split('.')
                            if len(parts) >= 2:
                                imports.add('.'.join(parts[:2]))
            except Exception:
                continue
        return imports

    def extract_scala_imports(self) -> Set[str]:
        """提取 Scala 代码中的 import"""
        imports = set()
        for scala_file in self.code_dir.rglob("*.scala"):
            try:
                with open(scala_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        m = re.match(r'^import\s+([\w.]+)', line.strip())
                        if m:
                            parts = m.group(1).split('.')
                            if len(parts) >= 2:
                                imports.add('.'.join(parts[:2]))
            except Exception:
                continue
        return imports

    def extract_cpp_includes(self) -> Set[str]:
        """提取 C++ 代码中的 #include"""
        imports = set()
        for cpp_file in self.code_dir.rglob("*.cpp"):
            try:
                with open(cpp_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        m = re.match(r'#include\s+[<"]([^>"]+)', line.strip())
                        if m:
                            imports.add(m.group(1))
            except Exception:
                continue
        for h_file in self.code_dir.rglob("*.h"):
            try:
                with open(h_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        m = re.match(r'#include\s+[<"]([^>"]+)', line.strip())
                        if m:
                            imports.add(m.group(1))
            except Exception:
                continue
        return imports

    def parse_pom_xml(self, pom_path: str) -> Set[str]:
        """解析 Maven pom.xml 提取依赖"""
        deps = set()
        try:
            tree = ET.parse(pom_path)
            root = tree.getroot()
            ns = {'m': 'http://maven.apache.org/POM/4.0.0'}

            for dep in root.findall('.//m:dependency', ns):
                group = dep.find('m:groupId', ns)
                artifact = dep.find('m:artifactId', ns)
                if group is not None and artifact is not None:
                    deps.add(f"{group.text}:{artifact.text}")
        except Exception as e:
            print(f"Warning: Failed to parse pom.xml: {e}")
        return deps

    def parse_requirements_txt(self, req_path: str) -> Set[str]:
        """解析 requirements.txt"""
        deps = set()
        try:
            with open(req_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # 提取包名（去掉版本号）
                        name = re.split(r'[>=<!\[]', line)[0].strip()
                        if name:
                            deps.add(name.lower())
        except Exception as e:
            print(f"Warning: Failed to parse requirements.txt: {e}")
        return deps

    def parse_cmake(self, cmake_path: str) -> Set[str]:
        """解析 CMakeLists.txt 提取依赖"""
        deps = set()
        try:
            with open(cmake_path, 'r') as f:
                content = f.read()
                # find_package
                for m in re.finditer(r'find_package\s*\(\s*(\w+)', content):
                    deps.add(m.group(1))
                # target_link_libraries
                for m in re.finditer(r'target_link_libraries\s*\([^)]+\s+(\w+)', content):
                    deps.add(m.group(1))
        except Exception as e:
            print(f"Warning: Failed to parse CMakeLists.txt: {e}")
        return deps

    def analyze(self, language: str, build_file: str = None) -> Dict:
        """执行依赖分析"""

        # 1. 提取代码中的 import
        if language == "python":
            self.imports = self.extract_python_imports()
            stdlib = self.PYTHON_STDLIB
        elif language == "java":
            self.imports = self.extract_java_imports()
            stdlib = self.JAVA_STDLIB
        elif language == "scala":
            self.imports = self.extract_scala_imports()
            stdlib = self.JAVA_STDLIB
        elif language == "cpp":
            self.imports = self.extract_cpp_includes()
            stdlib = self.CPP_STDLIB
        else:
            raise ValueError(f"Unsupported language: {language}")

        # 2. 过滤标准库
        external_imports = set()
        for imp in self.imports:
            is_stdlib = False
            for std in stdlib:
                if imp.startswith(std) or imp == std:
                    is_stdlib = True
                    break
            if not is_stdlib:
                external_imports.add(imp)

        # 3. 解析构建文件
        if build_file:
            if build_file.endswith('pom.xml'):
                self.build_deps = self.parse_pom_xml(build_file)
            elif build_file.endswith('requirements.txt'):
                self.build_deps = self.parse_requirements_txt(build_file)
            elif build_file.endswith('CMakeLists.txt'):
                self.build_deps = self.parse_cmake(build_file)

        # 4. 找出缺失的依赖
        if self.build_deps:
            # 检查哪些 import 不在构建依赖中
            for imp in external_imports:
                found = False
                for dep in self.build_deps:
                    if imp.lower() in dep.lower() or dep.lower().startswith(imp.lower()):
                        found = True
                        break
                if not found:
                    self.missing_deps.add(imp)
        else:
            self.missing_deps = external_imports

        return {
            "language": language,
            "total_imports": len(self.imports),
            "external_imports": sorted(list(external_imports)),
            "build_dependencies": sorted(list(self.build_deps)),
            "missing_dependencies": sorted(list(self.missing_deps)),
            "needs_mock": sorted(list(self.missing_deps)),
        }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phase 3: Dependency Analysis")
    parser.add_argument("--code-dir", required=True, help="提取后的代码目录")
    parser.add_argument("--language", required=True, choices=["java", "scala", "python", "cpp"])
    parser.add_argument("--build-file", help="构建文件路径 (pom.xml/requirements.txt/CMakeLists.txt)")
    parser.add_argument("--output", default="deps_report.json", help="输出报告路径")

    args = parser.parse_args()

    analyzer = DependencyAnalyzer(args.code_dir)
    result = analyzer.analyze(args.language, args.build_file)

    # 输出报告
    with open(args.output, 'w') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Dependency analysis complete:")
    print(f"  Total imports: {result['total_imports']}")
    print(f"  External imports: {len(result['external_imports'])}")
    print(f"  Missing dependencies: {len(result['missing_dependencies'])}")
    if result['missing_dependencies']:
        print(f"\nMissing dependencies that need Mock:")
        for dep in result['missing_dependencies']:
            print(f"  - {dep}")


if __name__ == "__main__":
    main()
