#!/usr/bin/env python3
"""
Phase 1: Static Slicing — 统一封装
调用 Joern/WALA/SVF/SQLGlot 执行切片，监控进度，输出结果。

支持的切片引擎:
- joern: 多语言统一方案
- wala: Java/Scala (字节码)
- svf: C++ (LLVM IR)
- sqlglot: Spark SQL (数据血缘)
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional


class SlicingEngine:
    """切片引擎基类"""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self.results = []

    def slice(self, entry_points: List[Dict]) -> Dict:
        """执行切片，返回 {类名: [行号]} 格式"""
        raise NotImplementedError

    def export_json(self, output_path: str):
        """导出切片结果"""
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)
        print(f"[Slice] Exported to {output_path}")


class JoernSlicer(SlicingEngine):
    """Joern CPG 切片"""

    _BAT = '.bat' if sys.platform == 'win32' else ''

    def __init__(self, project_root: str, joern_path: str = None):
        super().__init__(project_root)
        # 去掉 .bat 后缀统一存储，拼接命令时加 _BAT
        path = joern_path or 'joern'
        if path.lower().endswith('.bat'):
            path = path[:-4]
        self.joern_path = path
        self.cpg_path = None

    def generate_cpg(self) -> str:
        """生成 CPG"""
        self.cpg_path = str(self.project_root / '.cpg.bin')
        print(f"[Joern] Generating CPG for {self.project_root}...")

        cmd = [f"{self.joern_path}-parse{self._BAT}", str(self.project_root), '--output', self.cpg_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            print(f"[Joern] CPG generation failed: {result.stderr[:300]}")
            return None

        size_mb = os.path.getsize(self.cpg_path) / (1024 * 1024)
        print(f"[Joern] CPG generated: {self.cpg_path} ({size_mb:.1f} MB)")
        return self.cpg_path

    def slice(self, entry_points: List[Dict]) -> Dict:
        """对每个入口点执行向后切片"""
        if not self.cpg_path:
            if not self.generate_cpg():
                return {}

        slice_result = {}
        total = len(entry_points)

        for i, ep in enumerate(entry_points, 1):
            file_path = ep.get('file', '')
            line = ep.get('line', 0)
            name = ep.get('name', '')

            print(f"[Joern] [{i}/{total}] Slicing: {file_path}:{line} ({name})...")

            # 构建切片查询
            query = f'''
import io.shiftleft.semanticcpg.language._
val cpg = CpgLoader.load("{self.cpg_path}")

// 查找目标语句
val targetCalls = cpg.call
  .file(".*{Path(file_path).stem}.*")
  .lineNumber({line})

// 对每个目标执行向后切片
val results = targetCalls.flatMap {{ call =>
  val slice = call.repeat(_.inAst)(_.until(_.method.name("main|init|setup")))
    .dedup
    .collect {{
      case n if n.filename.nonEmpty =>
        Map("file" -> n.filename.head, "line" -> n.lineNumber.getOrElse(0))
    }}
  slice
}}.l

println(upickle.default.write(results))
'''

            with tempfile.NamedTemporaryFile(mode='w', suffix='.scala', delete=False) as f:
                f.write(query)
                script_path = f.name

            try:
                cmd = [f"{self.joern_path}{self._BAT}", '--script', script_path]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

                if result.returncode == 0:
                    output = result.stdout.strip()
                    json_match = r'\\[.*\\]'
                    import re
                    match = re.search(r'\[.*\]', output, re.DOTALL)
                    if match:
                        items = json.loads(match.group())
                        for item in items:
                            f = item.get('file', '')
                            l = int(item.get('line', 0))
                            if f and l > 0:
                                # 转换为相对路径
                                try:
                                    rel = str(Path(f).relative_to(self.project_root))
                                except ValueError:
                                    rel = f
                                slice_result.setdefault(rel, []).append(l)
                else:
                    print(f"[Joern] Slice failed: {result.stderr[:200]}")
            except Exception as e:
                print(f"[Joern] Error: {e}")
            finally:
                os.unlink(script_path)

        # 去重排序
        for k in slice_result:
            slice_result[k] = sorted(set(slice_result[k]))

        self.results = slice_result
        return slice_result


class WalaSlicer(SlicingEngine):
    """WALA 字节码切片 (Java/Scala)"""

    def __init__(self, project_root: str, wala_jar: str = None, exclusions: str = None):
        super().__init__(project_root)
        self.wala_jar = wala_jar
        self.exclusions = exclusions

    def compile_project(self) -> bool:
        """带参编译"""
        print("[WALA] Compiling with debug symbols...")
        cmd = [
            'mvn', 'clean', 'compile', '-q',
            '-Dmaven.compiler.debug=true',
            '-Dmaven.compiler.debuglevel=lines,vars,source'
        ]
        result = subprocess.run(cmd, cwd=self.project_root, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"[WALA] Compile failed: {result.stderr[:300]}")
            return False
        print("[WALA] Compile succeeded")
        return True

    def slice(self, entry_points: List[Dict]) -> Dict:
        """调用 WALA 执行向后切片"""
        # 编译
        if not self.compile_project():
            return {}

        # 构建 WALA 切片命令
        classes_dir = str(self.project_root / 'target' / 'classes')
        excl = self.exclusions or str(self.project_root / 'exclusions.txt')

        # 构建入口点参数
        sinks = []
        for ep in entry_points:
            if ep.get('language') in ('java', 'scala'):
                sinks.append(f"{ep['file']}:{ep['line']}")

        if not sinks:
            print("[WALA] No Java/Scala entry points found")
            return {}

        print(f"[WALA] Slicing {len(sinks)} entry points...")

        # 这里需要实际的 WALA Java 程序调用
        # 由于 WALA 是 Java 库，需要通过 Maven 插件或独立 JAR 调用
        # 简化实现：输出命令供用户手动执行
        cmd = f"""java -cp target/classes:target/dependency/* \\
  com.example.SlicerEngine \\
  --input {classes_dir} \\
  --exclusions {excl} \\
  --sinks {','.join(sinks[:10])}"""

        print(f"[WALA] Run this command manually:")
        print(cmd)
        print(f"[WALA] Or use the SlicerEngine.java from the README")

        return {}


class SqlGlotSlicer(SlicingEngine):
    """SQLGlot Spark SQL 血缘切片"""

    def __init__(self, project_root: str):
        super().__init__(project_root)

    def extract_sql_from_files(self) -> List[Dict]:
        """从源码中提取 SQL 字符串"""
        sqls = []
        for src_file in self.project_root.rglob("*.scala"):
            try:
                content = src_file.read_text(encoding='utf-8', errors='ignore')
                # 匹配 spark.sql("...")
                import re
                for m in re.finditer(r'spark\.sql\s*\(\s*"""(.*?)"""', content, re.DOTALL):
                    sqls.append({
                        'file': str(src_file.relative_to(self.project_root)),
                        'sql': m.group(1).strip(),
                    })
                for m in re.finditer(r'spark\.sql\s*\(\s*"([^"]*)"', content):
                    sqls.append({
                        'file': str(src_file.relative_to(self.project_root)),
                        'sql': m.group(1).strip(),
                    })
            except Exception:
                continue
        return sqls

    def slice(self, entry_points: List[Dict]) -> Dict:
        """对 SQL 执行字段级血缘追踪"""
        try:
            from sqlglot.lineage import lineage
        except ImportError:
            print("[SQLGlot] pip install sqlglot first")
            return {}

        sqls = self.extract_sql_from_files()
        print(f"[SQLGlot] Found {len(sqls)} SQL statements")

        slice_result = {}
        for item in sqls:
            sql = item['sql']
            file_path = item['file']

            # 找到输出表
            import re
            insert_match = re.search(r'INSERT\s+(?:INTO|OVERWRITE)\s+(\w+)', sql, re.IGNORECASE)
            if not insert_match:
                continue

            target_table = insert_match.group(1)

            # 追踪所有字段的血缘
            try:
                # 解析 SQL 找到 SELECT 的字段
                select_match = re.search(r'SELECT\s+(.*?)\s+FROM', sql, re.DOTALL | re.IGNORECASE)
                if select_match:
                    columns = [c.strip().split(' as ')[-1].strip() for c in select_match.group(1).split(',')]
                    for col in columns:
                        col = col.strip()
                        if col:
                            try:
                                node = lineage(column=col, sql=sql, dialect="spark")
                                # 收集叶子表
                                def get_leaves(n, tables):
                                    if not n.downstream:
                                        if hasattr(n.source, 'name'):
                                            tables.add(n.source.name)
                                    else:
                                        for child in n.downstream:
                                            get_leaves(child, tables)
                                tables = set()
                                get_leaves(node, tables)
                                if tables:
                                    slice_result.setdefault(file_path, []).append({
                                        'target': f"{target_table}.{col}",
                                        'sources': list(tables),
                                    })
                            except Exception:
                                continue
            except Exception as e:
                print(f"[SQLGlot] Error processing SQL in {file_path}: {e}")

        self.results = slice_result
        return slice_result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phase 1: Static Slicing")
    parser.add_argument("--project-root", required=True, help="项目根目录")
    parser.add_argument("--entry-points", required=True, help="Phase 0 输出的入口点 JSON")
    parser.add_argument("--engine", choices=["joern", "wala", "sqlglot", "auto"], default="auto")
    parser.add_argument("--output", default="slice_result.json", help="切片结果输出路径")
    parser.add_argument("--joern-path", help="Joern 可执行文件路径")

    args = parser.parse_args()

    # 加载入口点
    with open(args.entry_points) as f:
        ep_data = json.load(f)
    entry_points = ep_data.get('entry_points', [])
    print(f"[Phase 1] Loaded {len(entry_points)} entry points")

    # 选择引擎
    engine_name = args.engine
    if engine_name == "auto":
        # 根据语言自动选择
        languages = set(ep.get('language', '') for ep in entry_points)
        if 'scala' in languages and any('spark' in ep.get('signature', '').lower() for ep in entry_points):
            engine_name = 'sqlglot'
        elif languages & {'java', 'scala'}:
            engine_name = 'joern'
        elif 'cpp' in languages:
            engine_name = 'joern'
        else:
            engine_name = 'joern'

    print(f"[Phase 1] Using engine: {engine_name}")

    # 创建引擎
    if engine_name == 'joern':
        engine = JoernSlicer(args.project_root, args.joern_path)
    elif engine_name == 'wala':
        engine = WalaSlicer(args.project_root)
    elif engine_name == 'sqlglot':
        engine = SqlGlotSlicer(args.project_root)
    else:
        print(f"Unknown engine: {engine_name}")
        sys.exit(1)

    # 执行切片
    result = engine.slice(entry_points)

    # 导出
    engine.export_json(args.output)
    print(f"[Phase 1] Slice complete: {len(result)} files/entries")


if __name__ == "__main__":
    main()
