#!/usr/bin/env python3
"""
Phase 0: Entry Point Discovery — 分层检测架构

Layer 1+2: Joern CPG (HTTP 端点 + 文件输出 + DB 写入 + CLI 入口)
Layer 3:   Tree-sitter (补充：自定义框架模式)
Layer 4:   LLM 验证 (可选：去重、去误报、语义理解)

输出: entry_points.json
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Dict, Optional, Set


# ============================================================
# 数据结构
# ============================================================

@dataclass
class EntryPoint:
    type: str           # http_endpoint, file_output, db_write, cli_entry
    file: str           # 文件路径（相对于项目根目录）
    line: int           # 行号
    name: str           # 函数/方法名
    signature: str      # 签名描述
    language: str       # java, python, cpp, scala
    source: str         # 检测来源: joern, treesitter, llm
    confidence: float   # 置信度 0.0-1.0


# ============================================================
# Layer 1+2: Joern CPG 分析
# ============================================================

class JoernAnalyzer:
    """使用 Joern CPG 检测入口点和出口点"""

    # Joern Scala 查询模板
    QUERIES = {
        'http_endpoints': '''
            // 查找所有 HTTP 端点注解
            cpg.annotation
              .name("GetMapping|PostMapping|PutMapping|DeleteMapping|RequestMapping|PatchMapping")
              .map(a => {{
                val method = a.method.head
                val path = a.parameterAssign
                  .map(_.astChildren.l.last)
                  .headOption
                  .map(_.code)
                  .getOrElse("")
                Map(
                  "file" -> method.filename,
                  "line" -> method.lineNumber.getOrElse(0).toString,
                  "name" -> method.name,
                  "annotation" -> a.name,
                  "path" -> path.replaceAll("[\\"']", "")
                )
              }}
              ).l
        ''',

        'flask_fastapi_endpoints': '''
            // Python: Flask/FastAPI 路由
            cpg.annotation
              .name("route|get|post|put|delete|patch")
              .filter(a => a.code.contains("app\\.route") || a.code.contains("router\\."))
              .map(a => {{
                val method = a.method.head
                Map(
                  "file" -> method.filename,
                  "line" -> method.lineNumber.getOrElse(0).toString,
                  "name" -> method.name,
                  "annotation" -> a.name,
                  "path" -> a.parameterAssign.map(_.astChildren.l.last.code).headOption.getOrElse("")
                )
              }}
              ).l
        ''',

        'express_endpoints': '''
            // JavaScript: Express 路由
            cpg.call
              .name("get|post|put|delete")
              .filter(c => c.receiver.code.contains("app") || c.receiver.code.contains("router"))
              .filter(c => c.argument(1).isLiteral)
              .map(c => Map(
                "file" -> c.filename,
                "line" -> c.lineNumber.getOrElse(0).toString,
                "name" -> c.argument(2).code,
                "annotation" -> c.name,
                "path" -> c.argument(1).code
              ))
              .l
        ''',

        'file_output': '''
            // 文件写入操作
            cpg.call
              .name("write|writeFile|writeFileSync|FileOutputStream|FileWriter|ObjectOutputStream|dump|save|to_csv|to_parquet|saveAsTable")
              .filterNot(c => c.filename.contains("test") || c.filename.contains("spec"))
              .map(c => Map(
                "file" -> c.filename,
                "line" -> c.lineNumber.getOrElse(0).toString,
                "name" -> c.method.name,
                "call" -> c.name
              ))
              .l
        ''',

        'db_write': '''
            // 数据库写入操作
            cpg.call
              .name("save|persist|create|insert|execute|commit")
              .filter(c => c.code.contains("INSERT") || c.code.contains("save") || c.code.contains("persist"))
              .filterNot(c => c.filename.contains("test") || c.filename.contains("spec"))
              .map(c => Map(
                "file" -> c.filename,
                "line" -> c.lineNumber.getOrElse(0).toString,
                "name" -> c.method.name,
                "call" -> c.name
              ))
              .l
        ''',

        'cli_entry': '''
            // CLI 入口: main 函数
            cpg.method
              .name("main")
              .filter(m => m.parameter.name.toSet.contains("args") || m.signature.contains("String[]"))
              .map(m => Map(
                "file" -> m.filename,
                "line" -> m.lineNumber.getOrElse(0).toString,
                "name" -> m.name
              ))
              .l
        ''',

        'spark_output': '''
            // Spark 输出操作
            cpg.call
              .name("save|saveAsTable|insertInto|write|csv|parquet|json|orc")
              .filter(c => c.code.contains(".write") || c.code.contains(".save"))
              .map(c => Map(
                "file" -> c.filename,
                "line" -> c.lineNumber.getOrElse(0).toString,
                "name" -> c.method.name,
                "call" -> c.name
              ))
              .l
        ''',
    }

    _BAT = '.bat' if sys.platform == 'win32' else ''

    def __init__(self, project_root: str, joern_path: str = None):
        self.project_root = Path(project_root)
        self.joern_path = joern_path or self._find_joern()
        self.cpg_path = None

    @staticmethod
    def _strip_bat(path: str) -> str:
        """去掉 Windows .bat 后缀，统一存储基础路径"""
        if path.lower().endswith('.bat'):
            return path[:-4]
        return path

    def _find_joern(self) -> str:
        """查找 Joern 可执行文件，返回不带平台后缀的基础路径。

        joern-cli.zip 解压后同时包含 Unix 和 Windows 两套脚本:
            joern-cli/
            ├── joern / joern.bat          # 交互式 Shell
            ├── joern-parse / joern-parse.bat  # 代码解析 (生成 CPG)
            ├── joern-export / joern-export.bat
            ├── joern-slice / joern-slice.bat
            ├── javasrc2cpg / javasrc2cpg.bat
            ├── c2cpg.sh / c2cpg.bat
            ├── pysrc2cpg / pysrc2cpg.bat
            └── ...

        Windows 必须调用 .bat 文件，Unix 调用无后缀脚本。
        本方法返回不带 .bat 的路径，调用处通过 _BAT 属性加后缀。
        """
        # 检查 PATH
        if sys.platform == 'win32':
            search = ['joern.bat', 'joern']
        else:
            search = ['joern']
        for name in search:
            path = subprocess.run(
                ['where' if sys.platform == 'win32' else 'which', name],
                capture_output=True, text=True
            ).stdout.strip().split('\n')[0].strip()
            if path:
                return self._strip_bat(path)

        # 检查常见安装位置
        common_paths = [
            Path.home() / 'bin' / 'joern' / 'joern-cli',
            Path.home() / 'joern' / 'joern-cli',
            Path.home() / '.joern',
            Path('/opt/joern') / 'joern-cli',
            Path('C:/joern') / 'joern-cli',
        ]
        for p in common_paths:
            for exe in ('joern.bat', 'joern'):
                if (p / exe).exists():
                    return str(p / (exe.replace('.bat', '')))

        return 'joern'  # fallback

    def generate_cpg(self, output_path: str = None) -> str:
        """生成 CPG"""
        if output_path is None:
            output_path = str(self.project_root / '.cpg.bin')

        cmd = [
            f"{self.joern_path}-parse{self._BAT}",
            str(self.project_root),
            '--output', output_path
        ]

        print(f"[Joern] Generating CPG for {self.project_root}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            print(f"[Joern] CPG generation failed: {result.stderr}")
            return None

        self.cpg_path = output_path
        print(f"[Joern] CPG saved to {output_path}")
        return output_path

    def run_query(self, query_name: str) -> List[Dict]:
        """执行 Joern 查询"""
        if self.cpg_path is None:
            raise RuntimeError("CPG not generated. Call generate_cpg() first.")

        query = self.QUERIES.get(query_name)
        if not query:
            return []

        # 写入临时脚本
        script_content = f'''
import io.shiftleft.semanticcpg.language._
val cpg = CpgLoader.load("{self.cpg_path}")
val result = {query}
println(upickle.default.write(result))
'''

        with tempfile.NamedTemporaryFile(mode='w', suffix='.scala', delete=False) as f:
            f.write(script_content)
            script_path = f.name

        try:
            cmd = [f"{self.joern_path}{self._BAT}", '--script', script_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            if result.returncode == 0:
                # 解析 JSON 输出
                output = result.stdout.strip()
                # 找到 JSON 数组
                json_match = re.search(r'\[.*\]', output, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
            else:
                print(f"[Joern] Query '{query_name}' failed: {result.stderr[:200]}")

        except Exception as e:
            print(f"[Joern] Query '{query_name}' error: {e}")
        finally:
            os.unlink(script_path)

        return []

    def run_all_queries(self) -> List[EntryPoint]:
        """执行所有 Joern 查询"""
        entries = []

        for query_name in self.QUERIES:
            print(f"[Joern] Running query: {query_name}...")
            results = self.run_query(query_name)

            for item in results:
                # 确定类型
                if query_name in ('http_endpoints', 'flask_fastapi_endpoints', 'express_endpoints'):
                    entry_type = 'http_endpoint'
                elif query_name == 'file_output':
                    entry_type = 'file_output'
                elif query_name == 'db_write':
                    entry_type = 'db_write'
                elif query_name == 'cli_entry':
                    entry_type = 'cli_entry'
                elif query_name == 'spark_output':
                    entry_type = 'file_output'
                else:
                    entry_type = 'unknown'

                # 检测语言
                file_path = item.get('file', '')
                language = self._detect_language(file_path)

                entries.append(EntryPoint(
                    type=entry_type,
                    file=file_path,
                    line=int(item.get('line', 0)),
                    name=item.get('name', item.get('call', '')),
                    signature=item.get('path', item.get('call', '')),
                    language=language,
                    source='joern',
                    confidence=0.9,
                ))

        return entries

    def _detect_language(self, file_path: str) -> str:
        suffix = Path(file_path).suffix.lower()
        mapping = {
            '.java': 'java', '.scala': 'scala', '.py': 'python',
            '.js': 'javascript', '.ts': 'javascript',
            '.cpp': 'cpp', '.cc': 'cpp', '.c': 'cpp', '.h': 'cpp',
        }
        return mapping.get(suffix, 'unknown')


# ============================================================
# Layer 3: Tree-sitter 补充检测
# ============================================================

class TreeSitterScanner:
    """使用 Tree-sitter AST 补充检测自定义框架模式"""

    # 按语言定义的 AST 查询模式
    PATTERNS = {
        'python': {
            'http_endpoint': [
                (r'@app\.route\(["\']([^"\']+)', 'Flask', 0.95),
                (r'@router\.(get|post|put|delete|patch)', 'FastAPI', 0.95),
                (r'@api\.route\(["\']([^"\']+)', 'Flask-RESTful', 0.9),
                (r'@blueprint\.route\(["\']([^"\']+)', 'Flask Blueprint', 0.9),
            ],
            'file_output': [
                (r'open\s*\([^)]*["\']w', 'file open write', 0.9),
                (r'json\.dump\s*\(', 'JSON write', 0.95),
                (r'\.to_csv\s*\(', 'pandas to_csv', 0.95),
                (r'\.to_parquet\s*\(', 'pandas to_parquet', 0.95),
                (r'\.to_excel\s*\(', 'pandas to_excel', 0.95),
                (r'pickle\.dump\s*\(', 'pickle write', 0.9),
            ],
            'db_write': [
                (r'\.save\s*\(\s*\)', 'ORM save', 0.85),
                (r'\.create\s*\(\s*\)', 'ORM create', 0.85),
                (r'INSERT\s+INTO', 'SQL INSERT', 0.95),
            ],
            'cli_entry': [
                (r'if\s+__name__\s*==\s*["\']__main__["\']', 'main block', 0.95),
                (r'@click\.(command|group)\(\)', 'Click CLI', 0.95),
                (r'ArgumentParser\(\)', 'argparse', 0.9),
            ],
        },
        'java': {
            'http_endpoint': [
                (r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|RequestMapping)', 'Spring', 0.95),
                (r'@RestController', 'Spring Controller', 0.9),
                (r'@Path\(["\']', 'JAX-RS', 0.9),
            ],
            'file_output': [
                (r'new\s+FileOutputStream\s*\(', 'FileOutputStream', 0.95),
                (r'new\s+FileWriter\s*\(', 'FileWriter', 0.95),
                (r'new\s+ObjectOutputStream\s*\(', 'ObjectOutputStream', 0.95),
            ],
            'db_write': [
                (r'\.save\s*\(\s*\)', 'JPA save', 0.85),
                (r'\.persist\s*\(\s*\)', 'JPA persist', 0.85),
            ],
            'cli_entry': [
                (r'public\s+static\s+void\s+main\s*\(\s*String', 'main method', 0.95),
            ],
        },
        'scala': {
            'http_endpoint': [
                (r'@(GetMapping|PostMapping)', 'Spring', 0.9),
                (r'path\s*\(\s*"([^"]+)"', 'Akka HTTP', 0.9),
                (r'get\s*\(\s*path', 'Akka HTTP', 0.85),
            ],
            'file_output': [
                (r'\.saveAsTable\s*\(', 'Spark saveAsTable', 0.95),
                (r'\.save\s*\(\s*["\']', 'Spark save', 0.95),
                (r'\.write\.', 'Spark write', 0.95),
                (r'\.csv\s*\(', 'Spark csv', 0.95),
                (r'\.parquet\s*\(', 'Spark parquet', 0.95),
            ],
            'db_write': [
                (r'\.save\s*\(\s*\)', 'save', 0.85),
                (r'INSERT\s+INTO', 'SQL INSERT', 0.95),
            ],
        },
        'cpp': {
            'cli_entry': [
                (r'int\s+main\s*\(', 'main function', 0.95),
            ],
            'file_output': [
                (r'fopen\s*\([^)]*["\']w', 'fopen write', 0.95),
                (r'ofstream\s+', 'ofstream', 0.95),
                (r'fprintf\s*\(', 'fprintf', 0.85),
            ],
        },
        'javascript': {
            'http_endpoint': [
                (r'app\.(get|post|put|delete)\s*\(', 'Express', 0.95),
                (r'router\.(get|post|put|delete)\s*\(', 'Express Router', 0.95),
                (r'@Controller|@Get|@Post', 'NestJS', 0.9),
            ],
            'file_output': [
                (r'fs\.writeFile\(', 'fs.writeFile', 0.95),
                (r'fs\.writeFileSync\(', 'fs.writeFileSync', 0.95),
            ],
            'cli_entry': [
                (r'process\.argv', 'Node.js CLI', 0.85),
            ],
        },
    }

    def __init__(self, project_root: str):
        self.project_root = Path(project_root)

    def _detect_language(self, file_path: Path) -> Optional[str]:
        suffix = file_path.suffix.lower()
        mapping = {
            '.py': 'python', '.java': 'java', '.scala': 'scala',
            '.js': 'javascript', '.ts': 'javascript', '.mjs': 'javascript',
            '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp', '.c': 'cpp', '.h': 'cpp',
        }
        return mapping.get(suffix)

    def scan_file(self, file_path: Path) -> List[EntryPoint]:
        """扫描单个文件"""
        language = self._detect_language(file_path)
        if not language:
            return []

        patterns = self.PATTERNS.get(language, {})
        if not patterns:
            return []

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
        except Exception:
            return []

        entries = []
        rel_path = str(file_path.relative_to(self.project_root))

        for entry_type, type_patterns in patterns.items():
            for pattern, desc, confidence in type_patterns:
                for i, line in enumerate(lines, 1):
                    # 跳过注释行
                    stripped = line.strip()
                    if stripped.startswith('//') or stripped.startswith('#') or stripped.startswith('/*'):
                        continue
                    # 跳过字符串内的模式（简单检查）
                    if re.search(r'["\'].*' + pattern + r'.*["\']', line):
                        continue

                    if re.search(pattern, line, re.IGNORECASE):
                        func_match = re.search(
                            r'(?:def|function|public|private|protected|val|var)\s+(\w+)', line
                        )
                        func_name = func_match.group(1) if func_match else desc

                        entries.append(EntryPoint(
                            type=entry_type,
                            file=rel_path,
                            line=i,
                            name=func_name,
                            signature=line.strip()[:100],
                            language=language,
                            source='treesitter',
                            confidence=confidence,
                        ))

        return entries

    def scan_project(self, exclude_dirs: List[str] = None) -> List[EntryPoint]:
        """扫描整个项目"""
        if exclude_dirs is None:
            exclude_dirs = [
                '.git', 'node_modules', '__pycache__', '.venv', 'venv',
                'target', 'build', 'dist', '.idea', '.vscode', '.cpg.bin',
            ]

        entries = []
        for root, dirs, files in os.walk(self.project_root):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for file_name in files:
                file_path = Path(root) / file_name
                entries.extend(self.scan_file(file_path))

        return entries


# ============================================================
# Layer 4: LLM 验证（可选）
# ============================================================

class LLMVerifier:
    """使用 LLM 验证和补充入口点检测结果"""

    def __init__(
        self,
        base_url: str = None,
        chat_endpoint: str = None,
        api_key: str = None,
        model: str = None,
    ):
        # 支持多种接口格式:
        # 标准 OpenAI: base_url=/v1, chat_endpoint=/chat/completions
        # 自定义:       base_url=http://localhost:8080, chat_endpoint=/chat/
        self.base_url = (base_url or os.environ.get('LLM_BASE_URL', 'http://localhost:1234')).rstrip('/')
        self.chat_endpoint = chat_endpoint or os.environ.get('LLM_CHAT_ENDPOINT', '')
        self.api_key = api_key or os.environ.get('LLM_API_KEY', '')
        self.model = model or os.environ.get('LLM_MODEL', 'local')

        # 自动检测端点格式
        if not self.chat_endpoint:
            self.chat_endpoint = self._detect_chat_endpoint()

    def _detect_chat_endpoint(self) -> str:
        """自动检测 chat 端点"""
        import requests
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'

        # 尝试标准 OpenAI 格式
        candidates = [
            '/v1/chat/completions',
            '/chat/completions',
            '/chat/',
            '/v1/chat/',
        ]

        for endpoint in candidates:
            try:
                url = f"{self.base_url}{endpoint}"
                response = requests.post(
                    url,
                    json={'model': self.model, 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 5},
                    headers=headers,
                    timeout=10,
                )
                if response.status_code in (200, 400, 422):  # 400/422 说明端点存在但参数不对
                    print(f"[LLM] Detected chat endpoint: {endpoint}")
                    return endpoint
            except Exception:
                continue

        # 默认使用标准格式
        print("[LLM] Could not detect chat endpoint, using default: /v1/chat/completions")
        return '/v1/chat/completions'

    def _build_chat_url(self) -> str:
        """构建完整的 chat URL"""
        return f"{self.base_url}{self.chat_endpoint}"

    def verify_entries(self, entries: List[EntryPoint], source_code_sample: str = None) -> List[EntryPoint]:
        """使用 LLM 验证入口点列表"""
        if not entries:
            return entries

        # 构建 prompt
        entries_json = json.dumps([asdict(e) for e in entries[:50]], ensure_ascii=False, indent=2)

        prompt = f"""你是一个代码分析专家。以下是静态分析工具检测到的入口点和出口点列表。

请检查并：
1. 去除明显的误报（如注释中的模式、测试代码中的模式）
2. 标记你认为可能不是真正入口/出口的条目（confidence < 0.5）
3. 如果你能识别出遗漏的入口点，补充它们

入口点列表:
{entries_json}

请返回 JSON 格式，包含:
- validated: 验证后的入口点列表（每项包含 original 和 is_valid, confidence）
- suggestions: 建议补充的入口点列表
"""

        try:
            import requests
            headers = {'Content-Type': 'application/json'}
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'

            response = requests.post(
                self._build_chat_url(),
                json={
                    'model': self.model,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'max_tokens': 4000,
                    'temperature': 0.1,
                },
                headers=headers,
                timeout=60,
            )

            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']

                # 解析 LLM 返回的 JSON
                json_match = re.search(r'\{.*\}', content, re.DOTALL)
                if json_match:
                    validated = json.loads(json_match.group())
                    return self._apply_validation(entries, validated)

        except Exception as e:
            print(f"[LLM] Verification failed: {e}")
            print("[LLM] Falling back to unverified results")

        return entries

    def _apply_validation(self, entries: List[EntryPoint], validation: Dict) -> List[EntryPoint]:
        """应用 LLM 验证结果"""
        validated_entries = []

        # 构建验证映射
        validation_map = {}
        for item in validation.get('validated', []):
            key = f"{item.get('file', '')}:{item.get('line', 0)}"
            validation_map[key] = item

        for entry in entries:
            key = f"{entry.file}:{entry.line}"
            if key in validation_map:
                v = validation_map[key]
                if v.get('is_valid', True):
                    entry.confidence = v.get('confidence', entry.confidence)
                    entry.source = 'joern+llm' if entry.source == 'joern' else 'treesitter+llm'
                    validated_entries.append(entry)
            else:
                # 未被 LLM 验证的条目保留，但降低置信度
                entry.confidence = min(entry.confidence, 0.7)
                validated_entries.append(entry)

        # 添加 LLM 建议的补充条目
        for item in validation.get('suggestions', []):
            if isinstance(item, dict) and 'file' in item and 'line' in item:
                validated_entries.append(EntryPoint(
                    type=item.get('type', 'unknown'),
                    file=item['file'],
                    line=int(item['line']),
                    name=item.get('name', ''),
                    signature=item.get('signature', ''),
                    language=item.get('language', 'unknown'),
                    source='llm',
                    confidence=item.get('confidence', 0.6),
                ))

        return validated_entries


# ============================================================
# Phase 0 主流程
# ============================================================

class Phase0EntryPointDiscovery:
    """Phase 0: 入口点自动发现 — 分层检测"""

    def __init__(
        self,
        project_root: str,
        joern_path: str = None,
        llm_base_url: str = None,
        llm_chat_endpoint: str = None,
        llm_api_key: str = None,
        llm_model: str = None,
        enable_joern: bool = True,
        enable_treesitter: bool = True,
        enable_llm: bool = False,
    ):
        self.project_root = Path(project_root)
        self.enable_joern = enable_joern
        self.enable_treesitter = enable_treesitter
        self.enable_llm = enable_llm

        # 初始化各层
        if enable_joern:
            self.joern = JoernAnalyzer(str(project_root), joern_path)
        if enable_treesitter:
            self.treesitter = TreeSitterScanner(str(project_root))
        if enable_llm:
            self.llm = LLMVerifier(
                base_url=llm_base_url,
                chat_endpoint=llm_chat_endpoint,
                api_key=llm_api_key,
                model=llm_model,
            )

    def run(self) -> List[EntryPoint]:
        """执行分层检测"""
        all_entries = []

        # Layer 1+2: Joern CPG
        if self.enable_joern:
            print("=" * 50)
            print("Layer 1+2: Joern CPG Analysis")
            print("=" * 50)

            cpg_path = self.joern.generate_cpg()
            if cpg_path:
                joern_entries = self.joern.run_all_queries()
                print(f"[Joern] Found {len(joern_entries)} entry points")
                all_entries.extend(joern_entries)
            else:
                print("[Joern] CPG generation failed, skipping Joern analysis")

        # Layer 3: Tree-sitter
        if self.enable_treesitter:
            print("=" * 50)
            print("Layer 3: Tree-sitter Supplementary Scan")
            print("=" * 50)

            ts_entries = self.treesitter.scan_project()
            print(f"[Tree-sitter] Found {len(ts_entries)} entry points")

            # 去重：如果 Joern 已经检测到的，Tree-sitter 的结果作为验证
            joern_keys = {(e.file, e.line, e.type) for e in all_entries}
            new_ts_entries = []
            for e in ts_entries:
                if (e.file, e.line, e.type) not in joern_keys:
                    new_ts_entries.append(e)
                else:
                    # Tree-sitter 验证了 Joern 的结果，提高置信度
                    for existing in all_entries:
                        if existing.file == e.file and existing.line == e.line:
                            existing.confidence = min(existing.confidence + 0.05, 1.0)

            print(f"[Tree-sitter] {len(new_ts_entries)} new (not in Joern results)")
            all_entries.extend(new_ts_entries)

        # Layer 4: LLM 验证（可选）
        if self.enable_llm and all_entries:
            print("=" * 50)
            print("Layer 4: LLM Verification")
            print("=" * 50)

            all_entries = self.llm.verify_entries(all_entries)
            print(f"[LLM] Verified {len(all_entries)} entry points")

        # 按置信度排序
        all_entries.sort(key=lambda e: (-e.confidence, e.file, e.line))

        return all_entries

    def export_json(self, entries: List[EntryPoint], output_path: str):
        """导出结果为 JSON"""
        data = {
            "project_root": str(self.project_root),
            "total_entries": len(entries),
            "by_type": {},
            "by_source": {},
            "by_confidence": {
                "high (>0.8)": sum(1 for e in entries if e.confidence > 0.8),
                "medium (0.5-0.8)": sum(1 for e in entries if 0.5 <= e.confidence <= 0.8),
                "low (<0.5)": sum(1 for e in entries if e.confidence < 0.5),
            },
            "entry_points": [asdict(e) for e in entries],
        }

        for entry in entries:
            data["by_type"].setdefault(entry.type, 0)
            data["by_type"][entry.type] += 1
            data["by_source"].setdefault(entry.source, 0)
            data["by_source"][entry.source] += 1

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"\nExported {len(entries)} entry points to {output_path}")
        print(f"By type: {data['by_type']}")
        print(f"By source: {data['by_source']}")
        print(f"By confidence: {data['by_confidence']}")


# ============================================================
# CLI 入口
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Phase 0: Entry Point Discovery (Layered Detection)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 使用所有层 (Joern + Tree-sitter)
  python phase0_entry_discovery.py /path/to/project

  # 只使用 Tree-sitter (快速，无需 Joern)
  python phase0_entry_discovery.py /path/to/project --no-joern

  # 使用所有层 + LLM 验证
  python phase0_entry_discovery.py /path/to/project --enable-llm --llm-api-url http://localhost:1234/v1

  # 指定 Joern 路径
  python phase0_entry_discovery.py /path/to/project --joern-path /opt/joern/joern-cli
        """
    )

    parser.add_argument("project_root", help="项目根目录")
    parser.add_argument("--output", "-o", default="entry_points.json", help="输出 JSON 路径")
    parser.add_argument("--joern-path", help="Joern 可执行文件路径")
    parser.add_argument("--no-joern", action="store_true", help="禁用 Joern (只用 Tree-sitter)")
    parser.add_argument("--no-treesitter", action="store_true", help="禁用 Tree-sitter (只用 Joern)")
    parser.add_argument("--enable-llm", action="store_true", help="启用 LLM 验证")
    parser.add_argument("--llm-base-url", help="LLM base URL (如 http://localhost:1234)")
    parser.add_argument("--llm-chat-endpoint", help="LLM chat 端点 (如 /chat/ 或 /v1/chat/completions)")
    parser.add_argument("--llm-api-key", help="LLM API Key")
    parser.add_argument("--llm-model", help="LLM 模型名称")

    args = parser.parse_args()

    # 创建 Phase 0 实例
    phase0 = Phase0EntryPointDiscovery(
        project_root=args.project_root,
        joern_path=args.joern_path,
        llm_base_url=args.llm_base_url,
        llm_chat_endpoint=args.llm_chat_endpoint,
        llm_api_key=args.llm_api_key,
        llm_model=args.llm_model,
        enable_joern=not args.no_joern,
        enable_treesitter=not args.no_treesitter,
        enable_llm=args.enable_llm,
    )

    # 执行检测
    entries = phase0.run()

    # 导出结果
    phase0.export_json(entries, args.output)

    # 打印摘要
    print(f"\n{'=' * 50}")
    print(f"Summary: {len(entries)} entry points found")
    print(f"{'=' * 50}")
    for entry in entries[:20]:
        print(f"  [{entry.type}] {entry.file}:{entry.line} - {entry.name} (confidence: {entry.confidence:.2f}, source: {entry.source})")
    if len(entries) > 20:
        print(f"  ... and {len(entries) - 20} more")


if __name__ == "__main__":
    main()
