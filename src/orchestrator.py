#!/usr/bin/env python3
"""
ReportSpliter Orchestrator
串联所有 Phase，自动执行完整的代码切片和剥离流程。

Phase 0: 入口发现 → Phase 1: 切片 → Phase 2: 提取 → Phase 3: 依赖分析 → Phase 4: AI 补全 → Phase 5: 验证
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional


class Orchestrator:
    """全流程编排器"""

    def __init__(
        self,
        project_root: str,
        output_dir: str = None,
        # Phase 0 参数
        enable_joern: bool = True,
        enable_treesitter: bool = True,
        enable_llm: bool = False,
        joern_path: str = None,
        # Phase 1 参数
        slicing_engine: str = "auto",
        # Phase 4 参数
        llm_base_url: str = None,
        llm_chat_endpoint: str = None,
        llm_model: str = None,
        llm_api_key: str = "",
    ):
        self.project_root = Path(project_root).resolve()
        self.output_dir = Path(output_dir or self.project_root / "output").resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Phase 0 参数
        self.enable_joern = enable_joern
        self.enable_treesitter = enable_treesitter
        self.enable_llm = enable_llm
        self.joern_path = joern_path

        # Phase 1 参数
        self.slicing_engine = slicing_engine

        # Phase 4 参数
        self.llm_base_url = llm_base_url
        self.llm_chat_endpoint = llm_chat_endpoint
        self.llm_model = llm_model
        self.llm_api_key = llm_api_key

        # 中间结果路径
        self.entry_points_path = self.output_dir / "entry_points.json"
        self.slice_result_path = self.output_dir / "slice_result.json"
        self.extracted_dir = self.output_dir / "extracted_module"
        self.deps_report_path = self.output_dir / "deps_report.json"
        self.completion_path = self.output_dir / "completion_result.json"
        self.verify_result_path = self.output_dir / "verify_result.json"

    def run_phase0(self) -> bool:
        """Phase 0: 入口点发现"""
        print("=" * 60)
        print("Phase 0: Entry Point Discovery")
        print("=" * 60)

        sys.path.insert(0, str(Path(__file__).parent))
        from phase0_entry_discovery import Phase0EntryPointDiscovery

        phase0 = Phase0EntryPointDiscovery(
            project_root=str(self.project_root),
            joern_path=self.joern_path,
            llm_base_url=self.llm_base_url,
            llm_chat_endpoint=self.llm_chat_endpoint,
            llm_model=self.llm_model,
            llm_api_key=self.llm_api_key,
            enable_joern=self.enable_joern,
            enable_treesitter=self.enable_treesitter,
            enable_llm=self.enable_llm,
        )

        entries = phase0.run()
        phase0.export_json(entries, str(self.entry_points_path))

        if not entries:
            print("[Phase 0] WARNING: No entry points found!")
            return False

        print(f"[Phase 0] Found {len(entries)} entry points")
        return True

    def run_phase1(self) -> bool:
        """Phase 1: 静态切片"""
        print("=" * 60)
        print("Phase 1: Static Slicing")
        print("=" * 60)

        if not self.entry_points_path.exists():
            print("[Phase 1] ERROR: entry_points.json not found")
            return False

        sys.path.insert(0, str(Path(__file__).parent))
        from phase1_slicing import JoernSlicer, WalaSlicer, SqlGlotSlicer

        # 加载入口点
        with open(self.entry_points_path) as f:
            ep_data = json.load(f)
        entry_points = ep_data.get('entry_points', [])

        # 选择引擎
        engine_name = self.slicing_engine
        if engine_name == "auto":
            languages = set(ep.get('language', '') for ep in entry_points)
            if languages & {'java', 'scala', 'cpp'}:
                engine_name = 'joern'
            else:
                engine_name = 'joern'

        print(f"[Phase 1] Engine: {engine_name}, Entry points: {len(entry_points)}")

        if engine_name == 'joern':
            engine = JoernSlicer(str(self.project_root), self.joern_path)
        elif engine_name == 'wala':
            engine = WalaSlicer(str(self.project_root))
        elif engine_name == 'sqlglot':
            engine = SqlGlotSlicer(str(self.project_root))
        else:
            print(f"[Phase 1] Unknown engine: {engine_name}")
            return False

        result = engine.slice(entry_points)
        engine.export_json(str(self.slice_result_path))

        if not result:
            print("[Phase 1] WARNING: No slice results!")
            return False

        print(f"[Phase 1] Sliced {len(result)} files/entries")
        return True

    def run_phase2(self, language: str = "java") -> bool:
        """Phase 2: 代码提取"""
        print("=" * 60)
        print("Phase 2: Code Extraction")
        print("=" * 60)

        if not self.slice_result_path.exists():
            print("[Phase 2] ERROR: slice_result.json not found")
            return False

        sys.path.insert(0, str(Path(__file__).parent))
        from phase2_extract_code import load_slice_result, extract_files

        slice_result = load_slice_result(str(self.slice_result_path))
        print(f"[Phase 2] Loaded slice result: {len(slice_result)} classes")

        extracted = extract_files(
            slice_result=slice_result,
            source_root=str(self.project_root),
            export_root=str(self.extracted_dir),
            language=language,
        )

        if not extracted:
            print("[Phase 2] WARNING: No files extracted!")
            return False

        print(f"[Phase 2] Extracted {len(extracted)} files to {self.extracted_dir}")
        return True

    def run_phase3(self, language: str = "java", build_file: str = None) -> bool:
        """Phase 3: 依赖分析"""
        print("=" * 60)
        print("Phase 3: Dependency Analysis")
        print("=" * 60)

        if not self.extracted_dir.exists():
            print("[Phase 3] ERROR: extracted_module directory not found")
            return False

        sys.path.insert(0, str(Path(__file__).parent))
        from phase3_dependency_analysis import DependencyAnalyzer

        analyzer = DependencyAnalyzer(str(self.extracted_dir))
        result = analyzer.analyze(language, build_file)

        with open(self.deps_report_path, 'w') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        print(f"[Phase 3] Missing dependencies: {len(result.get('missing_dependencies', []))}")
        return True

    def run_phase4(self, language: str = "java") -> bool:
        """Phase 4: AI 补全"""
        print("=" * 60)
        print("Phase 4: AI-Assisted Completion")
        print("=" * 60)

        if not self.llm_base_url or not self.llm_model:
            print("[Phase 4] SKIPPED: --llm-base-url and --llm-model required")
            return True  # 跳过不算失败

        if not self.extracted_dir.exists():
            print("[Phase 4] ERROR: extracted_module directory not found")
            return False

        sys.path.insert(0, str(Path(__file__).parent))
        from phase4_ai_completion import AICompleter

        completer = AICompleter(
            base_url=self.llm_base_url,
            model=self.llm_model,
            api_key=self.llm_api_key,
            chat_endpoint=self.llm_chat_endpoint,
        )

        deps_report = str(self.deps_report_path) if self.deps_report_path.exists() else None

        print(f"[Phase 4] Generating mocks for {self.extracted_dir}...")
        completion = completer.generate_mocks(
            module_dir=str(self.extracted_dir),
            deps_report=deps_report,
            language=language,
        )

        with open(self.completion_path, 'w') as f:
            json.dump(completion, f, ensure_ascii=False, indent=2)

        if "error" not in completion:
            written = completer.write_completion(str(self.extracted_dir), completion)
            print(f"[Phase 4] Written {len(written)} files")
        else:
            print(f"[Phase 4] Error: {completion['error']}")
            return False

        return True

    def run_phase5(self, language: str = "java") -> bool:
        """Phase 5: 验证"""
        print("=" * 60)
        print("Phase 5: Verification")
        print("=" * 60)

        if not self.extracted_dir.exists():
            print("[Phase 5] ERROR: extracted_module directory not found")
            return False

        sys.path.insert(0, str(Path(__file__).parent))
        from phase5_verify import ModuleVerifier

        verifier = ModuleVerifier(str(self.extracted_dir))
        result = verifier.verify(language)

        with open(self.verify_result_path, 'w') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        success = result.get('overall_pass', False)
        print(f"[Phase 5] Compile: {'PASS' if result.get('compile_success') else 'FAIL'}")
        print(f"[Phase 5] Overall: {'PASS' if success else 'FAIL'}")

        return success

    def run_all(
        self,
        language: str = "java",
        build_file: str = None,
        skip_phase1: bool = False,
        skip_phase4: bool = False,
    ) -> bool:
        """执行全流程"""
        start_time = time.time()
        results = {}

        # Phase 0
        results['phase0'] = self.run_phase0()
        if not results['phase0']:
            print("\n[ABORT] Phase 0 failed, cannot continue")
            return False

        # Phase 1 (可跳过，如果已有切片结果)
        if not skip_phase1:
            results['phase1'] = self.run_phase1()
        else:
            print("[Phase 1] SKIPPED (using existing slice_result.json)")
            results['phase1'] = self.slice_result_path.exists()

        if not results['phase1']:
            print("\n[ABORT] Phase 1 failed, cannot continue")
            return False

        # Phase 2
        results['phase2'] = self.run_phase2(language)
        if not results['phase2']:
            print("\n[ABORT] Phase 2 failed, cannot continue")
            return False

        # Phase 3
        results['phase3'] = self.run_phase3(language, build_file)

        # Phase 4 (可选)
        if not skip_phase4:
            results['phase4'] = self.run_phase4(language)
        else:
            print("[Phase 4] SKIPPED")
            results['phase4'] = True

        # Phase 5
        results['phase5'] = self.run_phase5(language)

        # 汇总
        elapsed = time.time() - start_time
        print("\n" + "=" * 60)
        print("Summary")
        print("=" * 60)
        for phase, ok in results.items():
            status = "PASS" if ok else "FAIL"
            print(f"  {phase}: {status}")
        print(f"\nElapsed: {elapsed:.1f}s")
        print(f"Output: {self.output_dir}")

        return all(results.values())


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="ReportSpliter Orchestrator: 全流程代码切片与剥离",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 基本用法 (Joern + Tree-sitter)
  python orchestrator.py /path/to/project --language java

  # 指定 LLM (启用 Phase 4)
  python orchestrator.py /path/to/project --language java \\
    --llm-base-url http://localhost:8080 --llm-model qwen3.5

  # 跳过 Phase 1 (已有切片结果)
  python orchestrator.py /path/to/project --language java --skip-phase1

  # 只用 Tree-sitter (不需要 Joern)
  python orchestrator.py /path/to/project --no-joern --language python
        """
    )

    parser.add_argument("project_root", help="项目根目录")
    parser.add_argument("--output-dir", help="输出目录 (默认: project_root/output)")
    parser.add_argument("--language", default="java", choices=["java", "scala", "python", "cpp"])
    parser.add_argument("--build-file", help="构建文件路径 (pom.xml 等)")

    # Phase 0 参数
    parser.add_argument("--no-joern", action="store_true", help="禁用 Joern")
    parser.add_argument("--no-treesitter", action="store_true", help="禁用 Tree-sitter")
    parser.add_argument("--joern-path", help="Joern 路径")

    # Phase 1 参数
    parser.add_argument("--slicing-engine", default="auto", choices=["joern", "wala", "sqlglot", "auto"])
    parser.add_argument("--skip-phase1", action="store_true", help="跳过 Phase 1 (使用已有切片结果)")

    # Phase 4 参数
    parser.add_argument("--llm-base-url", help="LLM API 地址 (如 http://localhost:1234/v1)")
    parser.add_argument("--llm-chat-endpoint", help="自定义 chat 端点 (如 /chat/ 或 /v1/chat/completions)")
    parser.add_argument("--llm-model", help="模型名称 (如 gpt-4o, qwen3.5)")
    parser.add_argument("--llm-api-key", default="", help="API Key (也支持环境变量 LLM_API_KEY)")
    parser.add_argument("--skip-phase4", action="store_true", help="跳过 Phase 4")

    args = parser.parse_args()

    orchestrator = Orchestrator(
        project_root=args.project_root,
        output_dir=args.output_dir,
        enable_joern=not args.no_joern,
        enable_treesitter=not args.no_treesitter,
        joern_path=args.joern_path,
        slicing_engine=args.slicing_engine,
        llm_base_url=args.llm_base_url,
        llm_chat_endpoint=args.llm_chat_endpoint,
        llm_model=args.llm_model,
        llm_api_key=args.llm_api_key,
    )

    success = orchestrator.run_all(
        language=args.language,
        build_file=args.build_file,
        skip_phase1=args.skip_phase1,
        skip_phase4=args.skip_phase4,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
