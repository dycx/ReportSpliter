# ReportSpliter — 代码子模块高精度切片与剥离工具

> 将多语言项目中的特定功能，精准剥离为逻辑完整、可独立运行的子模块。

## 快速开始

```bash
# 一键全流程
python src/orchestrator.py /path/to/project --language java

# 带 LLM (启用 AI 补全)
python src/orchestrator.py /path/to/project --language java \
  --llm-base-url http://localhost:8080 --llm-model qwen3.5

# 只用 Tree-sitter (不需要 Joern，更快)
python src/orchestrator.py /path/to/project --no-joern --language python
```

## 项目结构

```
src/
├── orchestrator.py                 # 全流程编排器
├── phase0_entry_discovery.py       # 入口发现 (Joern + Tree-sitter + LLM)
├── phase1_slicing.py               # 静态切片 (Joern/WALA/SQLGlot)
├── phase2_extract_code.py          # 代码提取
├── phase3_dependency_analysis.py   # 依赖分析
├── phase4_ai_completion.py         # AI 补全
└── phase5_verify.py                # 编译验证
config/
├── exclusions_java.txt             # WALA 排除配置模板
docker/
├── Dockerfile                      # 分析环境容器
```

## 工作流程

```
Phase 0: 入口发现 → entry_points.json
Phase 1: 静态切片 → slice_result.json
Phase 2: 代码提取 → extracted_module/
Phase 3: 依赖分析 → deps_report.json
Phase 4: AI 补全  → Mock/Stub + 构建文件
Phase 5: 编译验证 → verify_result.json
```

## 分步执行

```bash
# Phase 0: 入口发现
python src/phase0_entry_discovery.py /path/to/project -o output/entry_points.json

# Phase 1: 切片
python src/phase1_slicing.py --project-root /path/to/project \
  --entry-points output/entry_points.json --engine joern

# Phase 2: 提取
python src/phase2_extract_code.py --slice output/slice_result.json \
  --source-root /path/to/project/src --output output/extracted_module

# Phase 3: 依赖分析
python src/phase3_dependency_analysis.py --code-dir output/extracted_module \
  --language java --build-file /path/to/project/pom.xml

# Phase 4: AI 补全
python src/phase4_ai_completion.py --module-dir output/extracted_module \
  --base-url http://localhost:8080 --model qwen3.5

# Phase 5: 验证
python src/phase5_verify.py --module-dir output/extracted_module --language java
```

## 支持语言

| 语言 | Phase 0 | Phase 1 | Phase 5 |
|------|---------|---------|---------|
| Java | ✅ Joern/Tree-sitter | ✅ Joern/WALA | ✅ Maven |
| Scala | ✅ Joern/Tree-sitter | ✅ Joern/WALA | ✅ SBT |
| Python | ✅ Joern/Tree-sitter | ✅ Joern | ✅ py_compile |
| C++ | ✅ Joern/Tree-sitter | ✅ Joern/SVF | ✅ CMake |
| Spark SQL | ✅ Tree-sitter | ✅ SQLGlot | - |

## Orchestrator 参数

```
python src/orchestrator.py <project_root> [options]

必选:
  project_root              项目根目录

可选:
  --output-dir              输出目录 (默认: project_root/output)
  --language                语言: java|scala|python|cpp (默认: java)
  --build-file              构建文件路径 (pom.xml 等)

Phase 0:
  --no-joern                禁用 Joern (只用 Tree-sitter)
  --no-treesitter           禁用 Tree-sitter (只用 Joern)
  --joern-path              Joern 可执行文件路径

Phase 1:
  --slicing-engine          切片引擎: joern|wala|sqlglot|auto (默认: auto)
  --skip-phase1             跳过 Phase 1 (使用已有切片结果)

Phase 4 (AI 补全):
  --llm-base-url            LLM API 地址 (如 http://localhost:8080/v1)
  --llm-chat-endpoint       自定义 chat 端点 (默认: 自动探测)
  --llm-model               模型名称 (如 gpt-4o, qwen3.5, deepseek-chat)
  --llm-api-key             API Key (也支持环境变量 LLM_API_KEY)
  --skip-phase4             跳过 Phase 4
```

### Phase 4 单独运行

```bash
python src/phase4_ai_completion.py --module-dir output/extracted_module \
  --base-url http://localhost:8080/v1 \
  --chat-endpoint /chat/completions \
  --model qwen3.5 \
  --api-key YOUR_KEY
```

## 环境要求

- Python 3.10+
- `pip install requests` (Phase 4 需要)

可选依赖:
- Joern (Phase 0/1): https://github.com/joernio/joern
- WALA (Phase 1 Java): https://github.com/valerioancona/wala
- SQLGlot (Phase 1 SQL): `pip install sqlglot`

### 安装 Joern

Joern 需要 **JDK 21**，请先安装: https://adoptium.net/

#### Windows

**方式一: 下载 zip（推荐）**

```powershell
# 下载最新版 joern-cli.zip
Invoke-WebRequest -Uri "https://github.com/joernio/joern/releases/latest/download/joern-cli.zip" -OutFile joern-cli.zip

# 解压到 C:\joern
Expand-Archive -Path joern-cli.zip -DestinationPath C:\joern

# 添加到 PATH（当前会话）
$env:PATH += ";C:\joern\joern-cli\bin"

# 验证
joern-cli --version
```

永久加入 PATH: 系统属性 → 环境变量 → Path → 新建 → `C:\joern\joern-cli\bin`

**方式二: WSL**

```bash
# 在 WSL 中执行
wget https://github.com/joernio/joern/releases/latest/download/joern-install.sh
chmod +x ./joern-install.sh
sudo ./joern-install.sh
```

**方式三: Docker**

```powershell
docker run --rm -it -v ${PWD}:/app:rw -w /app ghcr.io/joernio/joern joern
```

#### macOS / Linux

```bash
wget https://github.com/joernio/joern/releases/latest/download/joern-install.sh
chmod +x ./joern-install.sh
sudo ./joern-install.sh
joern-cli --version
```

#### 指定 Joern 路径

如果 Joern 不在 PATH 中，可通过 `--joern-path` 手动指定:

```bash
python src/orchestrator.py /path/to/project --joern-path /path/to/joern-cli/bin/joern-cli
```

> `--joern-path` 指向 `joern-cli` 可执行文件，代码会自动拼接 `-parse` 后缀调用 `joern-cli-parse`。
>
> 自动搜索顺序: `joern` → `joern-cli`（PATH）→ `~/joern/joern-cli` → `~/.joern` → `/opt/joern` → `C:/joern`

## 详细文档

- [完整技术文档](docs/technical-design.md) — 包含技术背景、工具对比、实施细节
- [调研记录](docs/design-trace.md) — 与 Gemini 的原始调研对话

## License

MIT
