#!/usr/bin/env python3
"""
Phase 4: AI-Assisted Code Completion
使用本地 LLM 为切片提取的代码生成 Mock/Stub 和构建文件。

支持标准 OpenAI 兼容接口: baseurl/v1/chat/completions
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional


class AICompleter:
    """AI 辅助代码补全"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        chat_endpoint: str = None,
        timeout: int = 120,
    ):
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        
        # 支持自定义 chat 端点
        if chat_endpoint:
            endpoint = chat_endpoint.lstrip('/')
            if endpoint.startswith('v1/'):
                self.chat_url = f"{self.base_url}/{endpoint}"
            else:
                self.chat_url = f"{self.base_url}/v1/{endpoint}"
        else:
            self.chat_url = f"{self.base_url}/v1/chat/completions"

    def _call_llm(self, prompt: str, system: str = None, max_tokens: int = 4000) -> str:
        """调用 LLM"""
        import requests

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        response = requests.post(
            self.chat_url,
            json={
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.1,
            },
            headers=headers,
            timeout=self.timeout,
        )

        if response.status_code != 200:
            raise RuntimeError(f"LLM API error {response.status_code}: {response.text[:200]}")

        result = response.json()
        content = result["choices"][0]["message"]["content"]

        # 处理推理模型: 如果 content 为空，尝试 reasoning_content
        if not content or not content.strip():
            content = result["choices"][0]["message"].get("reasoning_content", "")

        return content

    def scan_module(self, module_dir: str) -> Dict:
        """扫描提取的模块，收集信息"""
        module_path = Path(module_dir)

        info = {
            "language": "unknown",
            "files": [],
            "imports": [],
            "classes": [],
            "functions": [],
            "external_refs": [],
        }

        # 检测语言
        py_files = list(module_path.rglob("*.py"))
        java_files = list(module_path.rglob("*.java"))
        cpp_files = list(module_path.rglob("*.cpp")) + list(module_path.rglob("*.h"))

        if java_files:
            info["language"] = "java"
            info["files"] = [str(f.relative_to(module_path)) for f in java_files]
        elif py_files:
            info["language"] = "python"
            info["files"] = [str(f.relative_to(module_path)) for f in py_files]
        elif cpp_files:
            info["language"] = "cpp"
            info["files"] = [str(f.relative_to(module_path)) for f in cpp_files]

        # 提取 import 和外部引用
        for src_file in module_path.rglob("*"):
            if src_file.is_file() and src_file.suffix in ('.py', '.java', '.scala', '.cpp', '.h'):
                try:
                    content = src_file.read_text(encoding='utf-8', errors='ignore')
                    rel_path = str(src_file.relative_to(module_path))

                    # 提取 import
                    for line in content.splitlines():
                        line = line.strip()
                        m = re.match(r'^(?:from|import)\s+(\S+)', line)
                        if m:
                            info["imports"].append({"file": rel_path, "module": m.group(1)})
                        m = re.match(r'^import\s+([\w.]+)\s*;', line)
                        if m:
                            info["imports"].append({"file": rel_path, "module": m.group(1)})
                        m = re.match(r'#include\s+[<"]([^>"]+)', line)
                        if m:
                            info["imports"].append({"file": rel_path, "module": m.group(1)})

                    # 提取类定义
                    for m in re.finditer(r'(?:public\s+)?class\s+(\w+)', content):
                        info["classes"].append({"file": rel_path, "name": m.group(1)})
                    for m in re.finditer(r'def\s+(\w+)\s*\(', content):
                        info["functions"].append({"file": rel_path, "name": m.group(1)})

                except Exception:
                    continue

        return info

    def generate_mocks(
        self,
        module_dir: str,
        deps_report: str = None,
        language: str = None,
    ) -> Dict:
        """生成 Mock/Stub 代码"""
        module_path = Path(module_dir)

        # 扫描模块信息
        module_info = self.scan_module(module_dir)

        if not language:
            language = module_info["language"]

        # 加载依赖报告
        missing_deps = []
        if deps_report and Path(deps_report).exists():
            with open(deps_report) as f:
                deps = json.load(f)
                missing_deps = deps.get("missing_dependencies", [])

        # 收集文件内容（截断过长的文件）
        file_contents = {}
        for f in module_info["files"]:
            fp = module_path / f
            if fp.exists():
                content = fp.read_text(encoding='utf-8', errors='ignore')
                if len(content) > 3000:
                    content = content[:3000] + "\n... (truncated)"
                file_contents[f] = content

        # 构建 prompt
        prompt = f"""你是一个代码分析和补全专家。以下是一个通过程序切片提取的 {language} 子模块。

模块文件列表:
{json.dumps(module_info['files'], ensure_ascii=False)}

缺失的外部依赖:
{json.dumps(missing_deps, ensure_ascii=False)}

模块代码:
{json.dumps(file_contents, ensure_ascii=False, indent=2)}

请完成以下任务:

1. **生成 Mock/Stub 代码**: 为代码中引用但未定义的外部类、接口、函数生成简单的存根实现。
2. **生成构建文件**: 生成最小化的构建配置。

请返回 JSON 格式:
{{
  "mock_files": [
    {{
      "path": "relative/path/to/MockClass.java",
      "content": "完整文件内容"
    }}
  ],
  "build_file": {{
    "path": "pom.xml 或 requirements.txt 或 CMakeLists.txt",
    "content": "完整文件内容"
  }},
  "notes": "补充说明"
}}

要求:
- Mock 代码必须实现原接口/类的所有方法签名
- Mock 返回值使用合理的默认值（null/0/false/空字符串）
- 构建文件只包含必要的依赖
- 不要修改模块的核心计算逻辑
- 确保生成的代码语法正确
"""

        system = "你是一个代码补全专家。只返回 JSON，不要有其他内容。"

        print(f"[AI] Calling LLM ({self.model})...")
        response = self._call_llm(prompt, system, max_tokens=8000)
        print(f"[AI] Got response ({len(response)} chars)")

        # 解析 JSON
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group())
                return result
            except json.JSONDecodeError as e:
                print(f"[AI] JSON parse error: {e}")
                return {"error": "JSON parse failed", "raw_response": response[:500]}
        else:
            return {"error": "No JSON in response", "raw_response": response[:500]}

    def write_completion(self, module_dir: str, completion: Dict) -> List[str]:
        """将 AI 生成的代码写入模块目录"""
        module_path = Path(module_dir)
        written = []

        # 写入 Mock 文件
        for mock_file in completion.get("mock_files", []):
            file_path = module_path / mock_file["path"]
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(mock_file["content"], encoding='utf-8')
            written.append(str(file_path))
            print(f"[AI] Created mock: {mock_file['path']}")

        # 写入构建文件
        build_file = completion.get("build_file")
        if build_file and build_file.get("path") and build_file.get("content"):
            file_path = module_path / build_file["path"]
            file_path.write_text(build_file["content"], encoding='utf-8')
            written.append(str(file_path))
            print(f"[AI] Created build file: {build_file['path']}")

        return written


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phase 4: AI-Assisted Code Completion")
    parser.add_argument("--module-dir", required=True, help="提取的模块目录")
    parser.add_argument("--deps-report", help="Phase 3 输出的依赖报告 JSON")
    parser.add_argument("--language", choices=["java", "scala", "python", "cpp", "auto"], default="auto")
    parser.add_argument("--base-url", required=True, help="LLM API 地址 (如 http://localhost:1234/v1)")
    parser.add_argument("--chat-endpoint", help="自定义 chat 端点 (如 /chat/ 或 /v1/chat/completions)")
    parser.add_argument("--model", required=True, help="模型名称 (如 gpt-4o, qwen3.5)")
    parser.add_argument("--api-key", default="", help="API Key (也支持环境变量 LLM_API_KEY)")
    parser.add_argument("--output", default="completion_result.json", help="补全结果输出路径")
    parser.add_argument("--dry-run", action="store_true", help="只生成不写入")

    args = parser.parse_args()

    completer = AICompleter(
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        chat_endpoint=args.chat_endpoint,
    )

    language = args.language if args.language != "auto" else None

    print(f"[Phase 4] Scanning module: {args.module_dir}")
    module_info = completer.scan_module(args.module_dir)
    print(f"[Phase 4] Language: {module_info['language']}, Files: {len(module_info['files'])}")

    print(f"[Phase 4] Generating mocks and build files...")
    completion = completer.generate_mocks(
        module_dir=args.module_dir,
        deps_report=args.deps_report,
        language=language,
    )

    # 保存结果
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(completion, f, ensure_ascii=False, indent=2)
    print(f"[Phase 4] Result saved to {args.output}")

    # 写入文件
    if not args.dry_run and "error" not in completion:
        written = completer.write_completion(args.module_dir, completion)
        print(f"[Phase 4] Written {len(written)} files")
    elif "error" in completion:
        print(f"[Phase 4] Error: {completion['error']}")

    # 打印说明
    if completion.get("notes"):
        print(f"[Phase 4] Notes: {completion['notes']}")


if __name__ == "__main__":
    main()
