#!/usr/bin/env python3
"""
Phase 5: Verification
验证切片提取的模块是否可以独立编译/运行。
支持 Windows / macOS / Linux。
"""

import json
import os
import subprocess
import sys
import platform
from pathlib import Path
from typing import List, Tuple


IS_WINDOWS = platform.system() == "Windows"


class ModuleVerifier:
    """验证切片提取的模块"""

    def __init__(self, module_dir: str):
        self.module_dir = Path(module_dir)

    def detect_build_system(self) -> str:
        """检测构建系统"""
        if (self.module_dir / "pom.xml").exists():
            return "maven"
        if (self.module_dir / "build.gradle").exists() or (self.module_dir / "build.gradle.kts").exists():
            return "gradle"
        if (self.module_dir / "CMakeLists.txt").exists():
            return "cmake"
        if (self.module_dir / "Makefile").exists():
            return "make"
        if (self.module_dir / "requirements.txt").exists():
            return "pip"
        if (self.module_dir / "setup.py").exists():
            return "setup"
        if (self.module_dir / "pyproject.toml").exists():
            return "pyproject"
        if list(self.module_dir.rglob("*.py")):
            return "python"
        if list(self.module_dir.rglob("*.java")):
            return "java_no_build"
        return "unknown"

    def _run(self, cmd: list, cwd: Path = None, timeout: int = 120) -> Tuple[int, str, str]:
        """跨平台执行命令"""
        try:
            # Windows 下 shell=True 有助于找到 bat/cmd 脚本
            result = subprocess.run(
                cmd,
                cwd=cwd or self.module_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=IS_WINDOWS,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Command timed out"
        except FileNotFoundError:
            return -1, "", f"Command not found: {cmd[0]}"

    def _find_executable(self, name: str) -> str:
        """查找可执行文件路径"""
        # Windows 下可能有 .bat / .cmd 后缀
        if IS_WINDOWS:
            for suffix in ['', '.bat', '.cmd', '.exe']:
                path = shutil.which(name + suffix)
                if path:
                    return path
        else:
            path = shutil.which(name)
            if path:
                return path
        return name  # fallback

    def verify_maven(self) -> Tuple[bool, str]:
        """验证 Maven 项目"""
        mvn = self._find_executable("mvn")
        cmd = [mvn, "clean", "compile", "-q"]
        if IS_WINDOWS:
            # Windows 下用 mvnw.cmd 如果存在
            mvnw = self.module_dir / "mvnw.cmd"
            if mvnw.exists():
                cmd = [str(mvnw), "clean", "compile", "-q"]

        code, stdout, stderr = self._run(cmd, timeout=180)
        if code == 0:
            return True, "Maven compile succeeded"
        return False, stderr or stdout

    def verify_gradle(self) -> Tuple[bool, str]:
        """验证 Gradle 项目"""
        if IS_WINDOWS:
            gradlew = self.module_dir / "gradlew.bat"
        else:
            gradlew = self.module_dir / "gradlew"

        if gradlew.exists():
            cmd = [str(gradlew), "compileJava", "--quiet"]
        else:
            gradle = self._find_executable("gradle")
            cmd = [gradle, "compileJava", "--quiet"]

        code, stdout, stderr = self._run(cmd, timeout=180)
        if code == 0:
            return True, "Gradle compile succeeded"
        return False, stderr or stdout

    def verify_python(self) -> Tuple[bool, str]:
        """验证 Python 项目"""
        errors = []
        py_files = list(self.module_dir.rglob("*.py"))

        if not py_files:
            return False, "No .py files found"

        python = sys.executable  # 使用当前 Python 解释器
        for py_file in py_files:
            code, stdout, stderr = self._run(
                [python, "-m", "py_compile", str(py_file)],
                timeout=30
            )
            if code != 0:
                errors.append(f"{py_file.name}: {stderr.strip()}")

        if errors:
            return False, "\n".join(errors)
        return True, f"All {len(py_files)} Python files compiled successfully"

    def verify_cpp_cmake(self) -> Tuple[bool, str]:
        """验证 C++ CMake 项目"""
        build_dir = self.module_dir / "build"
        build_dir.mkdir(exist_ok=True)

        cmake = self._find_executable("cmake")
        make_cmd = self._find_executable("make")

        # Windows 下用 cmake --build 代替 make
        if IS_WINDOWS:
            code, stdout, stderr = self._run(
                [cmake, ".."], cwd=build_dir, timeout=60
            )
            if code != 0:
                return False, f"CMake configure failed: {stderr}"

            code, stdout, stderr = self._run(
                [cmake, "--build", ".", "--config", "Release"], cwd=build_dir, timeout=180
            )
        else:
            code, stdout, stderr = self._run(
                [cmake, ".."], cwd=build_dir, timeout=60
            )
            if code != 0:
                return False, f"CMake configure failed: {stderr}"

            code, stdout, stderr = self._run(
                [make_cmd, "-j4"], cwd=build_dir, timeout=180
            )

        if code == 0:
            return True, "C++ build succeeded"
        return False, stderr

    def verify_no_missing_local_imports(self, language: str) -> Tuple[bool, List[str]]:
        """检查是否有明显的缺失本地模块 import"""
        missing = []
        import re

        if language == "python":
            for py_file in self.module_dir.rglob("*.py"):
                try:
                    for line in py_file.read_text().splitlines():
                        m = re.match(r'^from\s+([\w.]+)\s+import', line.strip())
                        if m and '.' in m.group(1):
                            module = m.group(1)
                            parts = module.split('.')
                            # 检查本地模块是否存在
                            local_init = self.module_dir / '/'.join(parts) / '__init__.py'
                            local_py = self.module_dir / ('/'.join(parts) + '.py')
                            if not local_init.exists() and not local_py.exists():
                                missing.append(f"{py_file.name}: {module}")
                except Exception:
                    continue

        elif language in ("java", "scala"):
            for java_file in self.module_dir.rglob("*.java"):
                try:
                    for line in java_file.read_text().splitlines():
                        m = re.match(r'^import\s+([\w.]+)\s*;', line.strip())
                        if m:
                            imp = m.group(1)
                            # 跳过标准库
                            if any(imp.startswith(p) for p in ['java.', 'javax.', 'org.', 'com.sun.']):
                                continue
                            # 检查本地类是否存在
                            parts = imp.split('.')
                            local_java = self.module_dir / '/'.join(parts[:-1]) / (parts[-1] + '.java')
                            if not local_java.exists():
                                missing.append(f"{java_file.name}: {imp}")
                except Exception:
                    continue

        return len(missing) == 0, missing

    def verify(self, language: str = None) -> dict:
        """执行完整验证"""
        build_system = self.detect_build_system()

        if language is None:
            lang_map = {
                "maven": "java", "gradle": "java", "java_no_build": "java",
                "cmake": "cpp", "make": "cpp",
                "pip": "python", "setup": "python", "pyproject": "python", "python": "python",
            }
            language = lang_map.get(build_system, "unknown")

        result = {
            "module_dir": str(self.module_dir),
            "language": language,
            "build_system": build_system,
            "platform": platform.system(),
            "compile_success": False,
            "compile_output": "",
            "missing_imports": [],
            "overall_pass": False,
        }

        # 编译验证
        if build_system == "maven":
            success, output = self.verify_maven()
        elif build_system == "gradle":
            success, output = self.verify_gradle()
        elif build_system in ("pip", "setup", "pyproject", "python"):
            success, output = self.verify_python()
        elif build_system in ("cmake", "make"):
            success, output = self.verify_cpp_cmake()
        elif build_system == "java_no_build":
            # 没有构建文件，只做语法检查
            success, output = self._verify_java_syntax()
        else:
            success, output = False, f"Unsupported build system: {build_system}"

        result["compile_success"] = success
        result["compile_output"] = output[:2000]  # 截断过长输出

        # Import 检查
        imports_ok, missing = self.verify_no_missing_local_imports(language)
        result["missing_imports"] = missing[:50]  # 最多50个

        # 总体判断
        result["overall_pass"] = success and imports_ok

        return result

    def _verify_java_syntax(self) -> Tuple[bool, str]:
        """Java 语法检查（无构建文件时）"""
        import javalang
        errors = []
        java_files = list(self.module_dir.rglob("*.java"))
        for f in java_files:
            try:
                javalang.parse.parse(f.read_text())
            except Exception as e:
                errors.append(f"{f.name}: {str(e)[:100]}")
        if errors:
            return False, "\n".join(errors[:20])
        return True, f"All {len(java_files)} Java files parsed successfully"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phase 5: Module Verification")
    parser.add_argument("--module-dir", required=True, help="提取的模块目录")
    parser.add_argument("--language", choices=["java", "scala", "python", "cpp", "auto"],
                       default="auto", help="编程语言")
    parser.add_argument("--output", default="verify_result.json", help="验证结果输出路径")

    args = parser.parse_args()

    # 修复 Windows 路径
    module_dir = str(Path(args.module_dir).resolve())

    verifier = ModuleVerifier(module_dir)
    language = args.language if args.language != "auto" else None
    result = verifier.verify(language)

    # 输出结果
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # 打印摘要
    print(f"Verification result for {module_dir}:")
    print(f"  Platform: {result['platform']}")
    print(f"  Language: {result['language']}")
    print(f"  Build system: {result['build_system']}")
    print(f"  Compile: {'PASS' if result['compile_success'] else 'FAIL'}")
    if result['compile_output']:
        print(f"  Output: {result['compile_output'][:500]}")
    if result['missing_imports']:
        print(f"  Missing imports: {len(result['missing_imports'])}")
        for imp in result['missing_imports'][:10]:
            print(f"    - {imp}")
    print(f"\n  Overall: {'PASS' if result['overall_pass'] else 'FAIL'}")

    sys.exit(0 if result['overall_pass'] else 1)


if __name__ == "__main__":
    import shutil
    main()
