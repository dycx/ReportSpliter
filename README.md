# ReportSpliter — Java/Spring 仓库模块级划分工具

把 Java/Spring 代码仓库按功能入口（HTTP 端点 / 定时任务 / 监听器 / main）划分为
**逻辑完整的模块包**，再进一步：

- **代码 → 算法重建**（`rs algorithm`）：从代码还原算法步骤，每步带完整代码锚点；
- **算法对齐**（`rs align`）：与设计算法（`algorithms/<module>.yaml`）对比，产出差异清单
  与人工审核队列，定位业务代码与算法设计的未跟踪变动；
- **设计文档导入**（`rs design-import`）：Word / Excel / Markdown 设计算法 → 统一步骤规格；
- **报表血缘**（`rs reports` / `rs report`）：发现 Hive 表与 CSV 报告，构建列级血缘、
  报告依赖图与 git 变更追溯。

设计要点（详见 [架构设计 v1](docs/architecture-redesign.md)）：

- **不要求编译通过**。交付物是带血缘关系的模块包（`manifest.json` + `code/` + `llm-context.md`），
  目标是"这个模块完整表示某个功能怎么算出来的"。
- **模块间允许代码重复**（共享工具类各自复制），保证单模块完整。
- **面向 agent**：CLI 全 JSON 输出 + MCP stdio 服务，opencode / Claude Code 可直接接入。

## 快速开始

```bash
# 1. 安装（Python 3.10+）
python3 -m venv .venv
.venv/bin/pip install -e . --no-build-isolation

# 2. 在目标仓库生成 project.yml 并声明模块
cd /path/to/your-repo
rs init . \
  --module report-export --desc "报表导出" \
  --entry http:/api/reports

# 3. 一键跑全流程：index → discover → resolve → export → validate
rs run report-export
```

## CLI

```
rs init [dir]              # 生成 project.yml（--module / --entry 可重复）
rs index [dir]             # 构建/更新代码图（.report-spliter/index.json）
rs discover [dir]          # Spring 入口发现（HTTP/定时任务/监听器/main/Feign）
rs resolve <module>        # 模块依赖闭包（调用/类型/继承/注解边反向可达）
rs export <module>         # 物化模块包 → out/<module>/
rs validate <module>       # 结构校验与审计（缺口/未解析调用/过度提取）
rs run <module>            # 全流程
rs algorithm <module>      # 代码 → 算法重建（algorithm.md / algorithm.json）
rs align <module>          # 与设计算法对齐（差异 + 人工审核队列）
rs design-import <module> --from <doc>  # Word/Excel/Markdown 设计文档 → algorithms/<module>.yaml
rs reports [dir]           # 报表血缘注册表（Hive 表/CSV + 列级血缘 + 依赖图）
rs report <name> [dir]     # 单个报表的血缘明细
rs ls / rs show <module>   # 模块与产物状态
rs mcp                     # 启动 MCP stdio 服务
```

所有命令支持 `--json` 输出，供 agent 程序化消费。

## 算法对齐

设计算法放在 `algorithms/<module>.yaml`（步骤级规格，可手写，后续支持从文档/伪代码生成）：

```yaml
module: report-export
steps:
  - id: compute-total
    kind: transform
    label: 计算报表总额
    inputs: [base]
    outputs: [computed]
    note: 设计系数 1.15，代码中需核对
```

```bash
rs algorithm report-export   # 先重建代码侧算法
rs align report-export       # 再对齐，产出差异与审核队列
```

对齐产物：`alignment-report.md`（可读报告）、`alignment-report.json`（机器可读）、
`review-queue.json`（人工审核队列：设计有代码无 / 代码有设计无 / 低置信度匹配）。

设计算法也可以直接从文档导入（支持 Word .docx / Excel .xlsx / Markdown）：

```bash
rs design-import report-export --from docs/报表导出算法设计.docx
```

导入器自动识别编号列表/表格，抽取步骤类型（read/output/control/transform）、
输入、输出与备注，生成可直接用于 `rs align` 的规格文件。

## 报表血缘

```bash
rs reports                # 发现 Hive 表/CSV 报告，产出血缘注册表与依赖图
rs report monthly_sales   # 查看单个报告：定义锚点、上游表、列级血缘
rs reports --with-history # 附加 git 变更追溯（哪些提交动过报告的生产代码）
```

血缘来源：

- 独立 `.sql` 文件与宿主代码（Scala/Java/Python）内嵌 `spark.sql(...)` 中的
  `INSERT OVERWRITE TABLE` / `CREATE TABLE AS` → Hive 表报告，SQLGlot 列级血缘；
- `saveAsTable / insertInto / write.csv / to_csv` → 表或 CSV 报告，锚定宿主代码写点，
  上游为该文件内的 SQL/表读取（近似，标注低置信度）；
- 报告依赖图：`monthly_sales → sales_agg → orders/customers`，跨报告链路一目了然。

## project.yml

```yaml
project:
  root: .
  language: java
  exclude_dirs: [target, build, .git]
modules:
  - name: report-export
    description: 报表导出功能
    entries:
      - type: http_endpoint
        path: /api/reports        # 按路径前缀匹配，也支持 method + path
      # - type: symbol
      #   name: com.acme.report.controller.ReportExportController
      # - type: scheduled
      # - type: application_main
      # - type: feign_client
    resources:
      - src/main/resources/**
```

## 模块包产物（out/<module>/）

| 产物 | 说明 |
|---|---|
| `code/` | 按原项目相对路径复制的源码与资源 |
| `manifest.json` | 机器可读清单：入口、文件、命中符号（含行号）、调用边、外部依赖、未解析调用 |
| `llm-context.md` | 面向大模型的上下文：入口表、文件清单、逐文件符号、模块内调用链、外部依赖 |
| `audit-report.json` | 审计：引用缺口、未解析调用、内部调用缺失、过度提取率 |

## Agent 接入（opencode / Claude Code）

通过 MCP 接入（`rs mcp` 是纯 stdio JSON-RPC 服务，无额外依赖）：

```json
{
  "mcpServers": {
    "report-spliter": {
      "command": "/abs/path/to/repo/.venv/bin/rs",
      "args": ["mcp"]
    }
  }
}
```

暴露的工具：`rs_init` / `rs_index` / `rs_discover` / `rs_modules` / `rs_resolve` /
`rs_export` / `rs_validate` / `rs_run` / `rs_algorithm` / `rs_align` /
`rs_reports` / `rs_report` / `rs_design_import`。

## 测试

```bash
.venv/bin/python tests/e2e_test.py
```

基于 `tests/fixtures/java-spring-demo/`（双功能 Spring 小仓）验证：
模块划分正确性、交叉泄漏、共享代码重复纳入。

## 目录结构

```
rs/
├── cli.py             # click CLI
├── java_analyzer.py   # tree-sitter Java 解析（符号/调用/类型边）
├── indexer.py         # 项目代码图
├── discover.py        # Spring 入口发现
├── resolve.py         # 模块依赖闭包
├── algorithm.py       # 代码 → 算法重建
├── align.py           # 算法对齐 + 人工审核队列
├── design_import.py   # Word/Excel/Markdown 设计文档导入
├── lineage.py         # 报表血缘（Hive/CSV + 列级血缘 + git 追溯）
├── package.py         # 模块物化 + llm-context
├── validate.py        # 审计
├── mcp_server.py      # MCP stdio 服务
└── pipeline.py        # 端到端编排
tests/
├── fixtures/java-spring-demo/   # 测试用 Spring 小仓（模块划分 + 算法对齐）
└── fixtures/spark-sql-demo/     # 测试用 Spark SQL/CSV 小仓（报表血缘）
docs/architecture-redesign.md    # 架构设计 v1
```

> `src/` 下的旧 phase0–5 为遗留实现（不可运行/伪切片），新架构见 `rs/`。

## License

MIT
