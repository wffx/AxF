# AxF

AxF 是一个本地运行的知识驱动 fuzz harness 生成原型。当前版本聚焦一条最小链路：

```text
函数知识抽取 -> Harness 生成 Agent -> 本地产物
```

当前支持基于 VS Code C/C++ 插件生成的 `BROWSE.VC.DB` 抽取函数级上下文，并通过兼容 OpenAI Chat Completions 的 API 或 `opencode` CLI 生成 `libFuzzer` harness。
Harness 生成 Agent 会在生成后尝试本地编译；如果编译失败，会把错误信息反馈给 LLM 继续修复，直到编译成功或达到最大修复轮数。编译成功后会默认短跑 10 秒，前端会展示运行日志和试跑结果。

任务成功标准是：`harness_spec.json` 中 `run.status == "success"`。仅生成 `harness.c` 不算成功；编译失败、试跑失败、试跑超时、只生成未编译都会在前端显示为失败，但所有产物和日志仍会保留。

## 目录

- `knowledge_base/`：函数源码、下游子函数、调用链和参数约束抽取。
- `agents/harness_generation/`：Harness 生成 Agent。
- `frontend/`：本地 Web 控制台和 Terminal CLI。
- `workspace/`：本地任务和生成产物。
- `docs/`：架构、设计和 API 文档。

## 文档

- [精简架构](docs/architecture.md)
- [详细设计](docs/design.md)
- [Harness 生成 Agent](agents/harness_generation/README.md)
- [Terminal CLI](docs/api/frontend/terminal.md)
- [知识库组件说明](docs/knowledge_base/README.md)
- [cpp_meta_query API](docs/api/knowledge_base/cpp_meta_query.md)

## 快速运行

在仓库根目录准备 `.env.local`。该文件已被 `.gitignore` 忽略，不会同步到 GitHub：

```bash
LLM_MODE=api
API_KEY=你的模型服务密钥
CHAT_COMPLETIONS_URL=https://your-provider.example/v1/chat/completions
MODEL=glm-5.1
```

也可以只配置 base URL：

```bash
LLM_MODE=api
API_KEY=你的模型服务密钥
API_BASE_URL=https://your-provider.example/v1
MODEL=glm-5.1
```

Agent 会自动补 `/chat/completions`。模型请求默认使用流式 Chat Completions；如果要临时排查非流式兼容性，可以在命令行 Agent 中加 `--no-stream`。

也可以走 `opencode` CLI：

```bash
LLM_MODE=opencode
OPENCODE_TOOL=nga
OPENCODE_EXECUTABLE=nga
OPENCODE_MODEL=anthropic/claude-sonnet-4
```

`OPENCODE_TOOL` 可选 `nga`、`opencode`、`hac`、`claude`。`OPENCODE_MODEL` 可留空，留空时使用 CLI 自己的默认模型。

`nga` / `opencode` 使用和 OpenDeepHole 接近的 code-agent 调用方式，但不会把完整 prompt 当成长命令行参数传进去，也不会通过 `--file` 传 prompt。Agent 会先创建隔离工作区：

```text
workspace/<web|terminal>/tasks/<task_id>/harness/opencode_workspace/
  TASK.md
  context/prompt.md
  context/prompt_request_<n>.md
```

然后执行类似命令：

```text
nga run --dir <task_dir>/harness/opencode_workspace --model <模型> <短指令>
```

短指令只要求 CLI 读取 `context/prompt.md` 并输出 JSON。这样可以避开 Windows “文件名或扩展名太长”的限制，也能避免 code agent 因为访问源码树外的文件触发额外权限请求。`hac` / `claude` 仍按各自 CLI 的 `-p` prompt 形态调用。

Windows 上如果 `nga` 不在前端服务进程的 `PATH` 中，按 OpenDeepHole 的方式把 `OPENCODE_EXECUTABLE` 填成完整路径，例如 `C:/tools/nga.cmd`。注意前端服务启动后不会自动读取你新开的 PowerShell PATH；如果 `Get-Command nga` 有输出但页面仍显示未找到，先把完整 `.cmd` 路径填进模型设置，保存后重启前端服务。

Web 前端顶部的 `模型设置` 按钮会打开配置弹窗。API 模式填写模型、Chat Completions URL 和 API Key；CLI 模式填写工具、executable 和可选模型。保存后会更新仓库根目录的 `.env.local` 并同步到当前前端 server 进程环境变量；密钥不会写入任务配置、命令日志或 LLM 交互记录。

启动前端：

```bash
python -m frontend.server --host 127.0.0.1 --port 8787 --open
```

Windows PowerShell 同样可以运行：

```powershell
python -m frontend.server --host 127.0.0.1 --port 8787 --open
```

本项目当前按 Windows 端优先适配：建议在 Windows 上启动前端或 Terminal，让 `BROWSE.VC.DB` 使用 Windows 本地路径。若 clang 安装在 WSL 中，前端里把 `Clang 模式` 设为 `wsl`，`Clang 路径` 填 WSL 内路径，例如 `/usr/bin/clang`。AxF 会通过 `wsl wslpath` 把 Windows 任务目录转换成 WSL 路径后编译。

Windows 任务开始前会先检查 `rg`。上层调用链抽取依赖 `ripgrep`，没有 `rg` 时纯 Python 回退会非常慢，因此 AxF 在 Windows 上会按以下顺序处理：

1. 当前进程能找到 `rg`：直接运行任务。
2. 找不到 `rg`，但能找到 Scoop：执行 `scoop install ripgrep`。
3. `rg` 和 Scoop 都找不到：用 PowerShell 执行 `https://get.scoop.sh` 安装 Scoop，然后再安装 `ripgrep`。

自动安装失败时任务会停止，并把具体错误写进 `task.log` 和事件流。常见原因是 PowerShell 不存在、公司策略禁止执行安装脚本、网络无法访问 `get.scoop.sh`，或安装后当前前端进程的 PATH 还没刷新。安装完成但仍提示找不到 `rg` 时，重启前端服务或重新打开 Terminal。

页面中填入源码根目录、函数名、文件过滤，勾选 `生成 Fuzz Harness` 后新建任务。`复用知识库目录` 可以填写以前任务的目录，例如 `workspace/web/tasks/<old_task_id>`；系统只会复用本次勾选的知识产物，未勾选项不会生成、不会复用，也不会进入 Harness prompt。产物写入：

```text
workspace/web/tasks/<task_id>/
```

也可以不启动前端，直接使用 Terminal 入口。默认是交互式选择：

```bash
python -m frontend.terminal run
```

脚本化运行时用 `--artifacts` 指定产物 id：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --repo ../linux-7.0 \
  --function can_send \
  --file net/can/af_can.c \
  --artifacts report_json,subsource,params,harness_generation_agent \
  --model glm-5.1
```

Terminal 也可以显式使用 opencode：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --repo ../linux-7.0 \
  --function can_send \
  --file net/can/af_can.c \
  --artifacts report_json,params,harness_generation_agent \
  --llm-mode opencode \
  --opencode-tool nga \
  --opencode-executable nga \
  --opencode-model anthropic/claude-sonnet-4
```

Windows 上使用 WSL clang 的脚本化示例：

```powershell
python -m frontend.terminal run `
  --non-interactive `
  --repo C:\Users\yufei\linux-7.0 `
  --function can_send `
  --file net/can/af_can.c `
  --artifacts report_json,params,harness_generation_agent `
  --model glm-5.1 `
  --clang-mode wsl `
  --clang /usr/bin/clang
```

如果已经有之前任务抽取出的知识产物，可以指定目录复用，避免重复跑 kRepo：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --repo ../linux-7.0 \
  --function can_send \
  --file net/can/af_can.c \
  --knowledge-dir workspace/web/tasks/<old_task_id> \
  --artifacts report_json,params,harness_generation_agent
```

这会复用目录中的 `report.json` 和 `params.txt`，复制到新任务目录后再传给 Harness Agent。未在 `--artifacts` 中选择的知识产物不会复用，也不会进入 prompt。

Terminal 默认同步等待任务结束；加 `--async` 可以提交后台任务后立即返回：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --async \
  --repo ../linux-7.0 \
  --function can_send \
  --artifacts report_json
```

Terminal 产物写入：

```text
workspace/terminal/tasks/<task_id>/
```

核心产物按勾选项生成。`report_json`、`subsource`、`calls`、`params` 勾选后会加入 Harness prompt；`report_md` 和 `source` 只生成产物，不加入 Harness prompt；未勾选项不会生成，也不会发送给模型。指定 `--knowledge-dir` 时，这些规则保持不变，只是已存在的同名知识产物会被复制到新任务目录并跳过抽取。

常见产物：

- `report.json`
- `<function>_subsource_bundle.c`
- `calls.txt`
- `params.txt`
- `generated_harness.txt`
- `harness/harness.c`
- `harness/mocks.h`
- `harness/mocks.c`
- `harness/harness_spec.json`
- `harness/compile.log`
- `harness/run.log`
- `harness/llm_transcript.md`（`nga` 模式记录所有交互；其他模式仅当模型回复中包含 `harness.c` 时写入）
- `harness/nga_interactions/<attempt>/combined.rawoutput`（仅 `nga` 模式，按 oss-fuzz-gen 风格保存每次原始输出）
- `harness/opencode_workspace/context/prompt.md`（仅 `nga` / `opencode` 模式）

各产物用途见 [详细设计：产物说明](docs/design.md#8-产物说明)。

调试一次任务时，优先看：

1. `harness/harness_spec.json`：最终状态、编译状态和试跑状态。
2. `harness/compile.log`：编译失败原因。
3. `harness/run.log`：10 秒 libFuzzer 试跑输出。
4. `harness/opencode_workspace/context/prompt.md`：`nga` / `opencode` 模式下实际交给 code agent 读取的完整 prompt。
5. `harness/nga_interactions/<attempt>/`：`nga` 模式按 oss-fuzz-gen 的 raw output 思路保存每次尝试的 `prompt.md`、`stdout.rawoutput`、`stderr.rawoutput`、`combined.rawoutput` 和 `metadata.json`。
6. `harness/llm_transcript.md`：`nga` 模式会记录每次请求、CLI 原始输出、错误和重试；其他模式只记录“模型返回了 `harness.c`”的有效 harness 输出。非 `nga` 模式下这个文件不存在时，不一定代表没有调用模型，更常见的是模型只返回了错误说明、无效 JSON、权限提示或没有 `harness.c` 的 JSON。

如果改完代码后页面状态仍然像旧版本一样显示，请重启前端服务；已启动的本地服务进程不会自动加载新代码。

## 测试

```bash
python -m unittest discover -s tests -v
```

如果要运行依赖 Linux 源码树的知识库测试，可以设置：

```powershell
$env:KREPO_TEST_REPO='F:\path\to\linux-7.0'
python -m unittest tests.knowledge_base.test_cpp_meta_query -v
```
