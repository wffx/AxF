# AxF 详细设计

本文档描述 AxF 当前版本的工程设计。README 只保留快速入口；本文件记录具体架构、任务流、文件协议、Agent 约定和后续扩展方式。

## 1. 当前目标

AxF 当前版本不追求完整 fuzzing 平台，而是先跑通一个最小可用闭环：

```text
用户选择函数
  -> 知识库抽取函数上下文
  -> Harness 生成 Agent 调用 LLM
  -> 生成 libFuzzer harness 产物
  -> 本地编译验证
  -> 编译失败时反馈给 LLM 修复
  -> 编译成功后短跑 10 秒
```

第一阶段由 Harness 生成 Agent 负责生成、编译修复和一次短时间可运行性验证。它不负责长时间 fuzz 运行、覆盖率统计和 crash 分析；这些能力以后作为独立 Agent 加入。

### 1.1 当前已实现能力

当前代码已经实现以下能力：

- 本地 Web 控制台和 Terminal CLI，用户可以提交目标源码目录、可选复用知识库目录、函数名、文件过滤和 LLM 配置。
- kRepo/AxF 知识抽取和 AxF 后续流程在前端表单中分区展示。
- kRepo 知识产物包括 Markdown 报告、JSON 报告、源码分析包、下游源码包、上层调用链和入参约束。
- Harness 生成 Agent 从用户已选择的 kRepo 知识产物构造 prompt，并通过 API 模式或 opencode CLI 模式调用模型。
- LLM 输出 machine-readable JSON，Agent 从中写出 `harness.c`、`mocks.h`、`mocks.c`、构建脚本、字典和规格文件。
- Agent 自动使用 clang 编译生成的 harness；Windows 端可以选择 `native` 或 `wsl` 编译模式。
- 编译失败时，Agent 会把编译诊断和当前生成文件反馈给 LLM 修复，最多按配置重试。
- 编译成功后，Agent 默认运行生成的 libFuzzer 二进制 10 秒。
- 任务事件会展示 kRepo 抽取、Harness 编译和 libFuzzer 试跑的摘要。
- 任务状态会根据 `harness_spec.json` 派生为 `抽取完成`、`Harness 已生成`、`编译通过`、`编译失败`、`试跑失败` 等细状态；运行成功时前端显示为 `编译通过`。
- 产物页按来源分组为 `kRepo 知识产物` 和 `Harness 生成 Agent 产物`。
- `nga` 模式下所有模型交互都会写入 `harness/llm_transcript.md`；其他模型通道只记录真正生成了 `harness.c` 的有效 harness 输出。

## 2. 非目标

当前版本明确不做：

- 不修改 Linux 源码。
- 不修改 kRepo 来源代码。
- 不引入数据库服务、消息队列、用户权限系统或多租户能力。
- 不把前端做成完整云端平台。
- 不在 README 中堆放详细设计。
- 不把长时间运行 fuzz、覆盖率统计和 crash 分析混进 Harness 生成 Agent。

这些能力后续可以加，但要以独立 Agent 或独立工具层加入，避免第一版架构变重。

## 3. 顶层结构

```text
AxF/
  agents/
    harness_generation/
      agent.py
      README.md
  frontend/
    server.py
    static/
      index.html
      app.js
      styles.css
  knowledge_base/
    src/
      cpp_meta_query.py
      cpp_meta/
  docs/
    architecture.md
    design.md
  tests/
  workspace/
```

每层职责：

- `frontend/`：本地 Web 控制台、Terminal CLI 和轻量任务调度。
- `knowledge_base/`：函数级知识抽取。
- `agents/`：面向 fuzzing 生命周期的 Agent。
- `workspace/`：所有运行时任务和产物。
- `docs/`：设计、架构和 API 说明。

## 4. 数据边界

AxF 使用文件作为组件之间的协议。这样做是为了保持早期系统简单、可调试、可复制。

任务目录是唯一共享状态：

```text
workspace/web/tasks/<task_id>/
```

前端、知识库和 Agent 都只通过这个目录交换输入和输出。

### 4.1 任务元数据

`task.json` 记录任务配置和实际执行步骤：

```json
{
  "id": "9ca6df37c93c",
  "config": {
    "repo": "../linux-7.0",
    "knowledge_dir": "workspace/web/tasks/<old_task_id>",
    "function": "can_send",
    "file": "net/can/af_can.c",
    "artifacts": ["report_json", "subsource", "calls", "params", "harness_generation_agent"]
  },
  "steps": []
}
```

`events.jsonl` 记录面向 UI 的事件流：

```json
{"ts": 1780110187.69, "phase": "report_json", "message": "JSON 报告 已完成", "artifact": "report_json"}
```

Harness 生成 Agent 内部完成后，调度层会读取 `harness_spec.json`，把编译结果和 libFuzzer 10 秒运行结果展开成事件，例如 `Harness 编译通过`、`Harness 编译通过（libFuzzer 已运行 10 秒）`。完整输出仍以 `compile.log` 和 `run.log` 为准。

`task.log` 记录实际执行命令和进程输出。

## 5. 前端、Terminal 与任务调度

Web 前端入口：

```bash
python -m frontend.server --host 127.0.0.1 --port 8787 --open
```

Terminal 入口：

```bash
python -m frontend.terminal run
```

核心文件：

- `frontend/server.py`
- `frontend/terminal.py`
- `frontend/static/index.html`
- `frontend/static/app.js`
- `frontend/static/styles.css`

### 5.1 前端职责

前端只做：

- 收集用户输入。
- 调用本地 API 创建任务。
- 展示任务状态、事件、日志和产物。
- 读取已生成的产物文件。

前端不做：

- 不直接调用 LLM。
- 不直接拼 prompt。
- 不解析模型 JSON。
- 不编译 fuzz target。
- 不修改 Linux 源码。

### 5.2 后端 API

当前本地 HTTP API 很小：

```text
GET  /api/defaults
GET  /api/tasks
GET  /api/tasks/<task_id>
GET  /api/tasks/<task_id>/artifact?name=<artifact>
POST /api/tasks
POST /api/tasks/<task_id>/cancel
```

这些 API 只服务本地 Web UI，不作为长期稳定的公开 API。

### 5.3 任务步骤构造

`build_steps(config, task_dir)` 将一次任务转换为线性步骤。

如果只勾选知识产物：

```text
report_md
report_json
subsource
calls
params
```

如果勾选 `Harness 生成 Agent`，调度层不会自动补齐 kRepo 上下文。它只会把用户已经勾选的 kRepo 产物生成出来，并传给 Agent 作为 prompt 上下文：

```text
report_json   # 可选，勾选才生成并进入 prompt
subsource     # 可选，勾选才生成并进入 prompt
calls         # 可选，勾选才生成并进入 prompt
params        # 可选，勾选才生成并进入 prompt
harness_generation_agent
```

因此只勾选 `Harness 生成 Agent` 时，步骤只有：

```text
harness_generation_agent
```

Agent 仍会收到目标函数名、文件路径和源码根目录，并会直接调用 LLM 尝试生成 harness；不会因为未勾选 kRepo 上下文而在本地提前判定 unsupported。以下文件是推荐上下文，不是硬性必需输入：

- `report.json`
- `<function>_subsource_bundle.c`
- `calls.txt`
- `params.txt`

如果 `config.knowledge_dir` 非空，调度层会先在该目录查找用户已勾选的知识产物：

```text
report_md   -> report.md
report_json -> report.json
source      -> <function>_source_bundle.c
subsource   -> <function>_subsource_bundle.c
calls       -> calls.txt
params      -> params.txt
```

找到文件时，该步骤会变成 `reuse` 步骤：把旧文件复制到当前任务目录，并跳过对应 kRepo 命令。未勾选的知识产物不会被复用；目录中缺失的已勾选产物仍按原流程抽取。Harness Agent 始终读取当前任务目录中的文件，因此复用和新抽取对 Agent 是同一种输入形态。`task.json` 中的 step 会记录 `reuse_from`，事件流会显示 `正在复用`。

### 5.4 前端表单分区

前端表单按照职责分成五块：

`目标代码`
: 配置源码根目录、可选 `BROWSE.VC.DB`、可选复用知识库目录、函数名和文件过滤。文件过滤用于解决同名函数问题，例如 `net/can/af_can.c`。

`LLM 生成`
: 配置模型超时秒数、模型重试次数、clang 路径、clang 模式、最大修复轮数和编译超时秒数。API / opencode 调用方式、模型名、Chat Completions URL、API Key 或 opencode CLI 参数通过主页顶部的 `模型设置` 弹窗填写，并保存到 `.env.local`。

`kRepo 知识抽取`
: 只包含 kRepo/AxF 知识产物选择，例如 Markdown 报告、JSON 报告、下游源码包、上层调用链和入参约束。这里不再混入 Harness Agent。

`AxF 后续流程`
: 当前包含 `生成 Fuzz Harness`。未来新增执行、覆盖率、crash 分析等 Agent 时，应放在这里或拆成新的 AxF 流程区。

`kRepo 抽取限制`
: 只影响知识抽取阶段，例如依赖上限、片段行数、下游深度、函数上限、调用链深度和同名候选上限。

### 5.5 Terminal CLI

Terminal CLI 使用同一个 `build_steps(config, task_dir)` 构造 pipeline，因此产物选择语义与前端一致：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --repo ../linux-7.0 \
  --function can_send \
  --file net/can/af_can.c \
  --artifacts report_json,subsource,params,harness_generation_agent
```

交互模式使用 Python 标准库 `input()`，不引入第三方依赖。脚本模式加 `--non-interactive`；此时 `--repo`、`--function`、`--artifacts` 必填，缺失会直接报错。

Terminal 默认同步执行，不依赖 Web 服务启动。输出目录与 Web 任务分开：

```text
workspace/terminal/tasks/<task_id>/
```

目录结构仍包含 `task.json`、`events.jsonl`、`task.log`、kRepo 产物和 `harness/` 产物。`Ctrl+C` 会终止当前子进程，写入取消事件，并返回非零退出码。

Terminal 也支持复用已有知识产物：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --repo ../linux-7.0 \
  --function can_send \
  --knowledge-dir workspace/web/tasks/<old_task_id> \
  --artifacts report_json,params,harness_generation_agent
```

加 `--async` 时，Terminal 会创建任务目录、写入 `config.json`，然后启动后台 worker：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --async \
  --repo ../linux-7.0 \
  --function can_send \
  --artifacts report_json
```

后台 worker 继续写同一套 `task.json`、`events.jsonl`、`task.log` 和产物文件；提交命令立即返回任务目录和 `terminal.log` 路径。

### 5.6 前端任务状态

后端任务的原始状态仍然很少：

```text
queued
running
cancelling
cancelled
completed
failed
```

前端展示时不会直接把 `completed` 显示成泛泛的 `已完成`。它会读取任务产物和 `harness_spec.json` 派生更具体的状态：

```text
抽取完成
Harness 已生成
编译通过
编译失败
编译通过
试跑失败
试跑超时
未编译
未试跑
不支持
需手工 Fixture
```

如果任务选择了 `生成 Fuzz Harness`，调度层会把 `harness_spec.json` 作为最终判定来源。只有 `run.status == success` 才算完整成功；编译失败、试跑失败、试跑超时、只生成未编译、`unsupported` 或 `needs_manual_fixture` 都会把外层任务标记为 `failed`，但产物和日志仍然保留。页面暂时没有读到 `harness_spec.json` 但已经存在 Harness 相关产物时，状态才会回退为 `Harness 已生成`，不会显示成 `抽取完成`。

### 5.6 事件与日志

事件页展示的是面向用户的阶段摘要，例如：

```text
[JSON 报告] JSON 报告 已完成
[Harness 编译] Harness 编译通过（1 次尝试）
[libFuzzer 试跑] Harness 编译通过（libFuzzer 已运行 10 秒），日志见 10 秒运行日志
```

完整命令输出写入 `task.log`。Harness Agent 内部的长输出不直接刷满事件页：

- 编译完整输出看 `harness/compile.log` 和 `harness/compile_attempt_<n>.log`。
- libFuzzer 完整输出看 `harness/run.log`。
- `nga` 模式的所有模型交互看 `harness/llm_transcript.md`；其他模式的有效 harness 输出交互也看这个文件。
- `nga` / `opencode` 模式下实际 prompt 看 `harness/opencode_workspace/context/prompt.md`。
- CLI 启动失败、权限提示、非法 JSON 和重试次数看 `task.log`。

### 5.7 产物分组

产物页按来源分组：

`kRepo 知识产物`
: `report.md`、`report.json`、源码分析包、下游源码包、调用链和入参约束。

`Harness 生成 Agent 产物`
: `generated_harness.txt`、`harness.c`、mock 文件、构建脚本、`harness_spec.json`、`dict.txt`、编译日志、试跑日志、opencode prompt 工作区和存在时的 LLM 交互日志。

`其他产物`
: 未登记到上述两类的新产物会临时放在这里，便于后续新增 Agent 时渐进接入。

## 6. 知识库层设计

入口：

```bash
python knowledge_base/src/cpp_meta_query.py <command> <function> --repo <repo> --file <file>
```

当前使用的命令：

```text
report --format json
subsource
calls
params
```

### 6.1 输入

- Linux 或其他 C/C++ 项目源码根目录。
- VS Code C/C++ 插件生成的 `.vscode/BROWSE.VC.DB`。
- 目标函数名。
- 可选文件过滤，例如 `net/can/af_can.c`。

### 6.2 输出

知识库层输出纯上下文，不生成 harness：

```text
report.json
<function>_subsource_bundle.c
calls.txt
params.txt
```

### 6.3 路径兼容

文件过滤需要兼容 macOS/Linux 的 `/` 和 Windows 的 `\`。因此查询函数时要同时接受两种路径形式。

## 7. Harness 生成 Agent

位置：

```text
agents/harness_generation/
```

入口：

```bash
python -m agents.harness_generation.agent --help
```

### 7.1 职责

Harness 生成 Agent 负责：

- 读取知识库产物。
- 构造 LLM prompt。
- 调用兼容 Chat Completions 的模型服务。
- 解析模型返回的 JSON。
- 写出 harness 文件和规格文件。
- 使用 clang 编译 `harness.c` 和 `mocks.c`。
- 编译失败时把错误信息和当前生成文件反馈给 LLM，继续修复。
- 编译成功后运行生成的 fuzzer 10 秒，验证二进制至少可以启动执行。
- 将编译状态写入 `harness_spec.json` 和 `compile.log`。
- 将短跑状态写入 `harness_spec.json` 和 `run.log`。
- 写出便于 UI 预览的 `generated_harness.txt`。

它不负责：

- 长时间运行 libFuzzer。
- 覆盖率统计。
- 分析 crash。
- 管理长期知识库。

这些能力应由后续 Agent 负责。

### 7.2 CLI 参数

Harness 生成 Agent 的典型命令形式：

```bash
python -m agents.harness_generation.agent \
  --function can_send \
  --file net/can/af_can.c \
  --repo ../linux-7.0 \
  --task-dir workspace/web/tasks/<task_id> \
  --report-json workspace/web/tasks/<task_id>/report.json \
  --subsource workspace/web/tasks/<task_id>/can_send_subsource_bundle.c \
  --calls workspace/web/tasks/<task_id>/calls.txt \
  --params workspace/web/tasks/<task_id>/params.txt \
  --out workspace/web/tasks/<task_id>/harness \
  --artifact workspace/web/tasks/<task_id>/generated_harness.txt
```

可选参数：

```text
--model
--chat-url
--api-key-env
--llm-mode
--opencode-tool
--opencode-executable
--opencode-model
--timeout
--max-retries
--no-stream
--clang
--clang-mode
--max-repair-rounds
--compile-timeout
--skip-compile
--run-seconds
--skip-run
```

### 7.3 环境变量

默认读取仓库根目录下的 `.env.local` 和 `.env`：

```text
LLM_MODE=api
API_KEY=...
CHAT_COMPLETIONS_URL=...
MODEL=glm-5.1
```

也可以使用 opencode CLI：

```text
LLM_MODE=opencode
OPENCODE_TOOL=nga
OPENCODE_EXECUTABLE=nga
OPENCODE_MODEL=anthropic/claude-sonnet-4
```

也支持 shell 风格：

```bash
export API_KEY=...
```

密钥不进入任务目录，不进入日志，不进入 Git。

LLM 配置在前端中以 `LLM 生成` 区域呈现，含义如下：

```text
模型超时秒数      -> --timeout，默认 300
模型重试次数      -> --max-retries，默认 2
```

模型连接参数通过主页顶部的 `模型设置` 弹窗配置。API 模式填写模型、Chat Completions URL 和 API Key；CLI 模式填写工具、executable 和可选模型。保存后会写入仓库根目录的 `.env.local` 并同步当前 server 进程环境变量；任务配置中不保存真实密钥。

模型请求默认使用 OpenAI-compatible Chat Completions 的流式响应，即请求体包含 `"stream": true`，并按 SSE `data:` chunk 拼接 `choices[].delta.content`。这对 harness 修复轮很重要：修复轮输出可能较长，非流式调用必须等完整响应结束才返回，容易触发 read timeout。需要排查服务端兼容性时，可在命令行加 `--no-stream` 临时退回非流式。

URL 也做了兼容处理：如果配置的是完整 `.../chat/completions`，Agent 原样使用；如果配置的是 `API_BASE_URL`/`BASE_URL` 或类似 `https://host/api/v1` 的 base URL，Agent 会自动补 `/chat/completions`。

当 `LLM_MODE=opencode` 时，Harness 生成 Agent 不读取 API Key，而是调用：

```text
<AI CLI executable> ...
```

`OPENCODE_TOOL` 可选 `nga`、`opencode`、`hac`、`claude`。`OPENCODE_MODEL` 为空时不传 `--model`，由 CLI 使用自己的默认模型。

`nga` 和 `opencode` 的处理方式最接近 OpenDeepHole 的 code-agent 通道，但做了两点本地适配：

- 不把完整 prompt 作为命令行长参数传入。
- 不让 code agent 的工作目录直接指向 Linux 源码根目录。

Agent 会先创建隔离工作区：

```text
workspace/<web|terminal>/tasks/<task_id>/harness/opencode_workspace/
  TASK.md
  context/
    prompt.md
    prompt_request_<n>.md
```

`context/prompt.md` 是完整 harness prompt；`prompt_request_<n>.md` 保留每次请求时的快照，便于比较重试之间的差异。`TASK.md` 是给 code agent 的短任务说明，要求它读取 `context/prompt.md`，不要询问目标函数信息、kRepo/AxF 知识产物路径或 harness 规则库路径，并且只输出 JSON。

随后 `nga` / `opencode` 命令形态为：

```text
nga run --dir <task_dir>/harness/opencode_workspace --model <模型> <短指令>
opencode run --dir <task_dir>/harness/opencode_workspace --model <模型> <短指令>
```

这里的 `<短指令>` 不是完整 prompt，只是一段固定说明。这样做是为了解决三个实际问题：

- Windows 命令行长度限制会导致 `文件名或扩展名太长`。
- 某些 CLI 的 `--file` 语义不是“读取 prompt 文件”，会返回 `File not found: 请读取本次命令通过 --file 附加的 prompt 文件` 一类错误。
- code agent 如果工作目录是源码根目录，可能会请求读取或写入源码树权限；隔离工作区只包含 prompt 和任务说明，避免不必要的权限请求。

`hac` 和 `claude` 暂时仍走各自的直接 prompt 形态：

```text
hac --model <模型> -p <prompt>
claude -p --model <模型> <prompt>
```

Windows 上如果只填 `nga` 找不到，需要把 `OPENCODE_EXECUTABLE` 填成完整路径，例如 `C:/tools/nga.cmd`。这里检查的是 AxF 前端或 Terminal 进程的 PATH，不是你新开的 PowerShell 会话的 PATH；如果 `Get-Command nga` 有输出但页面仍显示未找到，通常是前端服务启动得更早，尚未继承新的 PATH。处理顺序是：先填完整 `.cmd` 路径，再保存模型设置，仍失败时重启前端服务。

和编译修复相关的配置也在同一区域：

```text
Clang 路径       -> --clang，留空时使用 CLANG 或 PATH 中的 clang
最大修复轮数     -> --max-repair-rounds，默认 3
编译超时秒数     -> --compile-timeout，默认 60
```

### 7.4 Prompt 输入

Agent 发送给模型的上下文包含基础目标信息：

- 目标函数名。
- 文件路径。
- 源码根目录。

这三项是硬输入，始终会出现在 prompt 顶部。即使用户没有勾选任何 kRepo 知识产物，Agent 也会要求模型先基于目标函数标识和通用 C/libFuzzer 经验生成最小可编译 harness，而不是直接返回“缺少目标函数信息”。

此外，只有用户勾选对应 kRepo 产物时，prompt 才会包含：

- `report.json` 内容。
- `subsource` 源码包。
- 上层调用链。
- 参数约束。

为了避免上下文过长，Agent 对输入做长度限制：

```text
report.json: 50000 chars
subsource:   90000 chars
calls:       24000 chars
params:      24000 chars
```

超过限制时会截断，并在 prompt 中标记 omitted chars。

Prompt 中还会显式约束模型：

- 本 prompt 已经包含全部可用输入。
- kRepo/AxF 知识产物是可选参考，不是必须存在的外部知识库路径。
- 不要要求用户补充“目标函数信息”“AxF/kRepo 知识产物”“harness 生成规则知识库路径”。
- 只有目标必须依赖真实硬件、真实内核并发语义或无法近似的函数指针分派时，才允许返回 `unsupported` 或 `needs_manual_fixture`。
- 只要不是上述真实不可模拟场景，`files` 必须包含 `harness.c`、`mocks.h`、`mocks.c`、`build.sh`、`build.ps1`。

如果模型返回 JSON 但没有 `harness.c`，且 `unsupported_reason` 或诊断里出现“缺少上下文/目标函数信息/kRepo/AxF 知识产物/知识库路径/规则库路径”这一类理由，Agent 会把该理由视为无效拒绝，并发起一次更强约束的 JSON 重试。

### 7.4.1 LLM 交互日志

生产任务的交互日志策略按模型通道区分：

- `nga`：记录所有交互，包括每次请求、CLI 原始 stdout/stderr、非零退出码、非法 JSON、权限提示、超时和重试。
- API、`opencode`、`hac`、`claude`：只记录“模型确实返回了 harness 文件”的交互，也就是 assistant 内容能被解析成 JSON，并且 `files` 中包含 `harness.c`。

日志追加写入：

```text
harness/llm_transcript.md
```

该文件用于审计模型生成或修复上下文，包含：

- 本轮 interaction 名称，例如 `initial generation` 或 `repair attempt 1`。
- 模型名。
- system 消息原文。
- user prompt 原文。
- assistant 返回内容。
- `nga` 模式下的 CLI 错误或非零退出码。

不写入：

- API key。
- Authorization header。
- Chat Completions URL。
- API、`opencode`、`hac`、`claude` 模式下没有 `harness.c` 的拒绝 JSON。
- API、`opencode`、`hac`、`claude` 模式下的 CLI 权限提示、安装提示、普通日志、非法 JSON 错误。
- API 模式 JSON 重试时的中间失败文本，除非重试最终返回了 `harness.c`。

因此非 `nga` 模式下 `harness/llm_transcript.md` 不存在时，不应该立刻理解为“没有调用模型”。更准确的含义是：本轮没有出现可记录的 harness 输出。`nga` 模式下如果已经实际启动 CLI，则该文件应该存在；如果不存在，优先怀疑 executable 解析失败发生在更早阶段，或任务还没有进入 Agent 调用。排查时按顺序看：

1. `harness/harness_spec.json`：是否生成了 spec，状态是什么。
2. `harness/llm_response.json` 或 `llm_repair_response_<n>.json`：本地保存的结构化模型响应。
3. `harness/opencode_workspace/context/prompt.md`：`nga` / `opencode` 模式下实际让 code agent 读取的 prompt。
4. `harness/nga_interactions/<attempt>/combined.rawoutput`：`nga` 模式下按 oss-fuzz-gen 风格保存每次尝试的原始输出。
5. `harness/llm_transcript.md`：`nga` 模式下保存每次交互的人读版。
6. `task.log`：CLI 启动失败、权限请求、JSON 解析失败和重试次数。

`task.log` 中会打印模型请求发送和返回事件，例如：

```text
LLM 交互 [initial generation] 请求已发送
LLM 交互 [initial generation] 响应已返回
```

这两条日志只表示请求流程发生过。`nga` 模式下请求进入 CLI 后会同时写入 `llm_transcript.md`；其他模式不表示一定写入了 transcript。

### 7.5 模型输出协议

模型必须返回 JSON 对象。推荐 schema：

```json
{
  "classification": "byte_parser|skb_handler|sock_msg|net_device_state|unsupported|needs_manual_fixture",
  "unsupported_reason": "",
  "mock_rationale": "",
  "seed_hints": [],
  "files": [
    {"path": "harness.c", "content": ""},
    {"path": "mocks.h", "content": ""},
    {"path": "mocks.c", "content": ""},
    {"path": "build.sh", "content": ""},
    {"path": "build.ps1", "content": ""},
    {"path": "dict.txt", "content": ""}
  ],
  "harness_spec": {
    "function": {"name": "can_send", "file": "net/can/af_can.c"},
    "classification": "skb_handler",
    "input_plan": [],
    "status": "generated",
    "diagnostics": []
  }
}
```

Agent 会接受带 Markdown code fence 的 JSON，例如：

````text
```json
{...}
```
````

但最终会只解析其中的 JSON 对象。

解析器会按以下顺序尽量提取 JSON：

1. 去掉外层 Markdown code fence。
2. 尝试直接 `json.loads`。
3. 从文本中提取第一个括号平衡的 JSON object，再尝试解析。
4. 对常见模型问题做有限修正：裸 key、单引号字符串、尾逗号、字符串内未转义控制字符。

这意味着 `nga` 或其他 CLI 在 JSON 前后打印少量日志时，只要正文中存在一个完整 JSON 对象，Agent 仍有机会解析成功。它不能修复的情况包括：

- 输出完全为空，对应 `Expecting value: line 1 column 1`。
- 输出只有权限提示、安装提示或自然语言说明，没有 `{...}`。
- JSON 被截断，括号无法平衡。
- 多个 JSON 对象互相冲突，且第一个对象不是 harness payload。

模型提示中仍要求“不要输出 Markdown 或解释”，因为容错解析只是兜底，不是稳定协议。长期稳定运行时，CLI 最好只把最终 JSON 写到 stdout。

### 7.6 编译与修复循环

Agent 写出初稿后，会在 `harness/` 目录下执行编译验证。默认 `--clang-mode native` 会在当前系统直接运行 clang：

```text
clang -std=gnu11 -fsanitize=fuzzer,address,undefined harness.c mocks.c -I. -o fuzzer
```

`--clang` 可以指定 clang 路径；未指定时依次使用环境变量 `CLANG` 和 PATH 中的 `clang`。

Windows 主机上如果 clang 安装在 WSL 中，可以使用：

```text
--clang-mode wsl --clang /usr/bin/clang
```

WSL 模式不会在 Windows PATH 中查找 clang，而是执行：

```text
wsl wslpath -a <Windows task dir>
wsl sh -lc 'cd <WSL task dir> && /usr/bin/clang -std=gnu11 -fsanitize=fuzzer,address,undefined harness.c mocks.c -I. -o fuzzer'
```

因此 Windows 端仍然负责运行前端、读取 `BROWSE.VC.DB` 和保存任务目录；编译和试跑在 WSL 内完成，产物二进制名是 Linux 形式 `fuzzer`，不是 `fuzzer.exe`。

如果编译失败，Agent 会把以下信息重新发送给 LLM：

- 编译命令。
- 编译退出码。
- 编译错误日志。
- 当前 `harness.c`、`mocks.h`、`mocks.c` 的短片段。
- 参数约束和调用链。

修复轮和首次生成不同：LLM 只需要返回本轮要修改的文件，不需要重复完整文件包。Agent 会在本地把修复 JSON 和上一轮产物合并后再次编译。这样第二轮请求不会因为“重发全量文件 + 等待全量输出”而明显慢于第一轮。

默认最多修复 3 轮，可通过 `--max-repair-rounds` 调整。

为减少多轮修复时的超时，repair prompt 会裁剪当前生成文件和上下文：

```text
单个当前生成文件: 3000 chars
params/calls 修复上下文: 2000 chars
compile diagnostics: 12000 chars
```

每个 LLM 请求默认超时 300 秒，并在超时或网络 timeout 类错误时最多重试 2 次。每次尝试都会在 `task.log` 中体现，例如 `repair attempt 1 request 1/3`、`repair attempt 1 request 2/3`。`nga` 模式下每次尝试都会追加写入 `llm_transcript.md`；其他模式只有返回内容包含 `harness.c` 时才会追加写入。

每轮编译日志写入：

```text
compile_attempt_1.log
compile_attempt_2.log
...
compile.log
```

`harness_spec.json` 中会记录：

```json
{
  "status": "compiled|compile_failed|generated",
  "compile": {
    "status": "success|failed|skipped",
    "attempts": [
      {"attempt": 1, "returncode": 1, "log": "compile_attempt_1.log"}
    ],
    "message": "compile failed"
  }
}
```

如果没有生成 `harness.c`，编译会标记为 `skipped`。如果模型返回 `unsupported` 但仍写出了 `harness.c`，Agent 仍会尝试编译，让编译结果决定后续状态。

### 7.7 10 秒试跑

如果编译成功，Agent 默认在 `harness/` 目录下执行：

```text
./fuzzer -max_total_time=10 -max_len=4096 seed_corpus
```

Windows native 模式下会执行 `.\fuzzer.exe`；Windows WSL 模式下会执行 `wsl sh -lc 'cd <WSL task dir> && ./fuzzer ...'`。试跑日志写入：

```text
run.log
```

`harness_spec.json` 中会记录：

```json
{
  "status": "run_succeeded|runtime_failed|compiled",
  "run": {
    "status": "success|failed|timeout|skipped",
    "seconds": 10,
    "returncode": 0,
    "log": "run.log"
  }
}
```

这一步只验证 harness 是否可以启动并运行短时间，不代表覆盖率充足，也不等价于 crash 分析。需要关闭时可使用 `--skip-run`，需要调整时长可使用 `--run-seconds`。

### 7.8 输出文件

Agent 输出目录：

```text
workspace/web/tasks/<task_id>/harness/
```

标准文件：

```text
harness.c
mocks.h
mocks.c
build.sh
build.ps1
dict.txt
harness_spec.json
compile.log
compile_attempt_<n>.log
run.log
llm_prompt.txt
llm_response.json
llm_repair_prompt_<n>.txt
llm_repair_response_<n>.json
seed_corpus/README.txt
```

有条件生成的文件：

```text
llm_transcript.md                         # nga 记录所有交互；其他模式只记录包含 harness.c 的回复
nga_interactions/<attempt>/prompt.md      # 仅 nga 模式
nga_interactions/<attempt>/stdout.rawoutput
nga_interactions/<attempt>/stderr.rawoutput
nga_interactions/<attempt>/combined.rawoutput
nga_interactions/<attempt>/metadata.json
opencode_workspace/TASK.md                # 仅 nga / opencode 模式
opencode_workspace/context/prompt.md      # 仅 nga / opencode 模式
opencode_workspace/context/prompt_request_<n>.md
```

汇总文件：

```text
workspace/web/tasks/<task_id>/generated_harness.txt
```

前端会按来源把产物分为 `kRepo 知识产物` 和 `Harness 生成 Agent 产物`。其中 Harness 生成 Agent 会展示：

- `Harness 生成 Agent`
- `Fuzz 驱动 harness.c`
- `Mock 头文件`
- `Mock 源文件`
- `Unix 构建脚本`
- `Windows 构建脚本`
- `Harness 规格`
- `Fuzz 字典`
- `编译日志`
- `10 秒运行日志`
- `LLM 交互日志`（存在时展示）

## 8. 产物说明

一次任务会产生两类产物：知识库上下文和 Agent 生成结果。

### 8.1 知识库上下文

`report.md`
: 面向人阅读的 Markdown 报告。勾选后只作为产物保存，不加入 Harness prompt。适合人工快速确认目标函数、依赖和调用摘要。

`report.json`
: 结构化函数报告。包含目标函数位置、源码、参数、依赖、调用信息等。勾选后会作为机器可读上下文加入 Harness prompt。

`<function>_source_bundle.c`
: 目标函数源码分析包。勾选后只作为产物保存，不加入 Harness prompt。它更适合人工审阅目标函数和依赖片段；Harness prompt 默认使用更聚焦的 `report.json`、`subsource`、`calls` 和 `params`。

`<function>_subsource_bundle.c`
: 目标函数和下游子函数源码包。勾选后会加入 Harness prompt，用于帮助 Agent 判断目标函数内部会调用什么、需要构造什么输入、需要 mock 什么内核依赖。

`calls.txt`
: 上层调用链。勾选后会加入 Harness prompt，用于理解目标函数的真实调用场景，例如谁会调用它、调用路径大致是什么。

`params.txt`
: 参数约束。勾选后会加入 Harness prompt，用于记录参数类型、空指针检查、长度字段、分支条件和从源码中推断出的输入限制。

指定 `knowledge_dir` 后，以上知识产物可以从旧任务目录复用。复用只是复制文件到新任务目录；是否传给 Harness Agent 仍只由本次勾选项决定。

### 8.2 Harness 生成结果

`generated_harness.txt`
: Harness 生成 Agent 的汇总预览文件。它把分类、mock/fixture 说明、未支持原因、文件清单和主要代码内容放在一起，方便在前端快速查看。它是人读预览，不是稳定机器协议。

`harness/harness.c`
: 真正的 fuzz 驱动入口文件。它应该包含 `LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size)`，是后续编译和执行最重要的文件。

`harness/mocks.h`
: mock 和 fixture 的头文件。通常包含用户态替代结构体、函数声明、常量和辅助构造函数声明。

`harness/mocks.c`
: mock 和 fixture 的实现文件。通常包含最小内核依赖模拟、内存释放函数、网络结构体构造函数等。

`harness/build.sh`
: macOS/Linux 下编译 fuzz 驱动的脚本。Agent 自身会使用内置 clang 命令做编译验证；该脚本保留给用户手动复现。

`harness/build.ps1`
: Windows PowerShell 下编译 fuzz 驱动的脚本。当前 Agent 的自动编译验证以本机运行环境为准，Windows 脚本保留给 Windows 用户手动复现。

`harness/dict.txt`
: libFuzzer 字典。当前版本由 Harness 生成 Agent 要求 LLM 在 `files` 中返回 `dict.txt`，Agent 原样写入。它通常包含协议 magic、flag、长度字段、常见枚举值等，有助于 libFuzzer 更快探索有效输入。如果模型没有返回 `dict.txt`，前端不会显示 `Fuzz 字典` 产物。后续可以增加规则抽取 Agent，从 `params.txt`、`report.json` 和源码包中稳定抽取常量，再交给 LLM 筛选。

`harness/harness_spec.json`
: 机器可读的 harness 规格。包含目标函数、分类、输入构造计划、生成状态、编译状态、短跑状态和诊断信息。后续执行或 crash 分析 Agent 应优先读取这个文件，而不是解析 `generated_harness.txt`。

`harness/compile.log`
: 编译验证汇总日志。包含每次编译尝试的命令、退出码和诊断。若最终状态为 `compile_failed`，优先看这个文件。

`harness/compile_attempt_<n>.log`
: 单轮编译日志。每次生成或修复后都会记录一次，便于对比 LLM 修复前后的错误变化。

`harness/run.log`
: 编译成功后的 10 秒试跑日志。包含 fuzzer 启动命令、退出码和 libFuzzer 输出。若 `harness_spec.json` 中 `run.status` 为 `failed` 或 `timeout`，优先看这个文件。

`harness/coverage/summary.json`
: 编译成功且 libFuzzer 短跑成功后生成的覆盖率机器摘要。Agent 会用 coverage instrumentation 重新编译一个覆盖率版本，运行同样的短跑语料，再通过 `llvm-profdata` 和 `llvm-cov export --summary-only` 计算行、函数、region、branch 覆盖率。

`harness/coverage/report.md`
: 覆盖率 Markdown 摘要，前端会作为 `覆盖率报告` 产物展示。

`harness/coverage/coverage.log`
: 覆盖率计算日志。包含 coverage binary 编译、profile merge 和 llvm-cov export 的命令与退出码。覆盖率计算失败不会把已经编译和试跑成功的 harness 任务判失败，应优先看这个日志定位工具缺失或 instrumentation 编译失败。

`harness/llm_transcript.md`
: LLM 交互日志。`nga` 模式记录所有交互，包括请求 prompt、CLI 原始输出、stderr、非零退出码、非法 JSON 和重试；API、`opencode`、`hac`、`claude` 模式只记录模型返回了 `harness.c` 的交互。API key 不写入该文件，Chat Completions URL 不写入该文件。非 `nga` 模式下这个文件不存在时，优先检查 `task.log`、`llm_response.json` 和 `opencode_workspace/context/prompt.md`，不要把它当成“模型没有被调用”的唯一证据。

`harness/nga_interactions/<attempt>/combined.rawoutput`
: `nga` 模式下的原始输出归档。这个目录参考 oss-fuzz-gen 的 response directory / `*.rawoutput` 做法，每次尝试单独保存 `prompt.md`、`stdout.rawoutput`、`stderr.rawoutput`、`combined.rawoutput` 和 `metadata.json`。`combined.rawoutput` 是 Agent 实际拿去解析 JSON 的文本；排查 `Expecting value`、`Extra data`、权限请求或 CLI stderr 时优先看它和同目录下的 stderr。

`harness/opencode_workspace/context/prompt.md`
: `nga` / `opencode` 模式下给 code agent 读取的完整 prompt。它包含目标函数信息、可选知识产物片段、JSON 输出协议和禁止缺上下文拒绝的约束。如果 CLI 输出说“没有找到 kRepo/AxF 知识产物”或“需要目标函数信息”，先看这个文件确认 prompt 是否已经包含这些信息。

`harness/opencode_workspace/TASK.md`
: `nga` / `opencode` 模式下的短任务说明。命令行参数里传的是这类短指令，不是完整 prompt；完整 prompt 在 `context/prompt.md`。

### 8.3 优先查看顺序

调试一次生成结果时，推荐按以下顺序查看：

1. `generated_harness.txt`：先看 Agent 的总体判断。
2. `harness/harness.c`：确认 fuzz 入口和目标函数调用。
3. `harness/mocks.c` / `harness/mocks.h`：确认 mock 是否过度简化或明显错误。
4. `harness/harness_spec.json`：确认分类、输入计划和状态。
5. `harness/compile.log`：确认编译是否成功；失败时看具体错误。
6. `harness/run.log`：编译成功后确认 10 秒试跑是否通过。
7. `harness/opencode_workspace/context/prompt.md`：`nga` / `opencode` 模式下确认 code agent 实际读取的完整 prompt。
8. `harness/nga_interactions/<attempt>/combined.rawoutput`：`nga` 模式下确认每次原始 stdout/stderr 合并输出。
9. `harness/llm_transcript.md`：`nga` 模式下看人读交互；其他模式下存在时确认最终有效 harness 输出对应的 prompt 和模型返回内容。
10. `build.sh` 或 `build.ps1`：需要手动复现编译时再看。

## 9. 任务状态与错误处理

当前任务状态：

```text
queued
running
cancelling
cancelled
completed
failed
```

任一步骤返回非零退出码，任务标记为 `failed`。

注意：Harness 生成 Agent 内部可能会把“编译失败但已有可审计产物”的情况写入 `harness_spec.json`，并以正常退出结束进程。调度层会继续读取该 spec；如果最终不是 `run.status == success`，外层任务会标记为 `failed`，同时保留 `harness.c`、编译日志和 LLM 交互日志，方便继续修复。

常见失败原因：

- 找不到 `BROWSE.VC.DB`。
- Windows 上找不到 `rg`，且自动安装 Scoop 或 ripgrep 失败。
- 函数名找不到。
- 同名函数未用 `--file` 精确过滤。
- `API_KEY` 没有设置。
- `CHAT_COMPLETIONS_URL` 没有设置。
- `LLM_MODE=opencode` 但找不到 CLI executable。
- `nga` / `opencode` CLI 启动了，但没有读取 `opencode_workspace/context/prompt.md`。
- code agent 请求额外权限，或尝试访问隔离工作区外的路径。
- 模型返回不是合法 JSON。
- 模型返回 JSON 但没有 `harness.c`，且理由是缺少目标函数信息或 kRepo/AxF 知识产物。
- 模型输出的文件路径非法。
- 编译失败后请求 LLM 修复超时。
- 生成的 harness 编译失败。
- 编译通过后 10 秒试跑失败或超时。

Agent 会拒绝写出逃逸输出目录的路径，例如：

```text
../../bad.c
```

## 10. Windows 支持

当前 Windows 是 AxF 的主要目标运行环境，支持范围：

- 前端服务可用 `python -m frontend.server` 启动。
- Terminal CLI 可用 `python -m frontend.terminal run` 启动。
- `.env.local` 可直接放在仓库根目录。
- Linux 源码目录、`BROWSE.VC.DB`、任务目录和复用目录可以使用 Windows 本地路径。
- 编译可以使用 Windows native clang，也可以通过 WSL clang 自动完成。
- 也可用 PowerShell 环境变量：

```powershell
$env:API_KEY='...'
$env:CHAT_COMPLETIONS_URL='...'
$env:MODEL='glm-5.1'
```

或：

```powershell
$env:LLM_MODE='opencode'
$env:OPENCODE_TOOL='nga'
$env:OPENCODE_EXECUTABLE='C:/tools/nga.cmd'
$env:OPENCODE_MODEL='anthropic/claude-sonnet-4'
```

Agent 会要求模型同时输出：

- `build.sh`
- `build.ps1`

知识库查询的文件过滤需要兼容：

```text
net/can/af_can.c
net\can\af_can.c
```

推荐 Windows 端配置：

```text
Clang 模式: wsl
Clang 路径: /usr/bin/clang
```

这种配置下，AxF 仍在 Windows 上运行前端和任务调度，知识库读取 Windows 路径中的 `BROWSE.VC.DB`；只有 Harness 编译和 10 秒试跑通过 `wsl sh -lc` 进入 WSL。前提是任务目录所在 Windows 路径能被 WSL 访问，`wslpath -a <task dir>` 可以返回有效路径。

如果选择 `native`，Windows 下会使用 Windows clang 并生成 `fuzzer.exe`。如果选择 `wsl`，会使用 WSL 内 clang 并生成 `fuzzer`。

### 10.1 `rg` / Scoop 预检

Windows 下耗时最明显的步骤通常是上层调用链抽取。AxF 的调用链搜索优先使用 `rg`；如果没有 `rg`，大源码树上的搜索会慢很多。因此 Web 任务和 Terminal 任务开始执行步骤前都会做一次 Windows 预检：

```text
1. shutil.which("rg") 有结果
   -> 直接继续任务。

2. 找不到 rg，但能找到 scoop
   -> 执行 scoop install ripgrep。

3. rg 和 scoop 都找不到，但能找到 PowerShell
   -> 执行 get.scoop.sh 安装 Scoop，再执行 scoop install ripgrep。

4. PowerShell 也找不到，或安装命令失败
   -> 任务失败，错误写入 task.log 和 events.jsonl。
```

安装完成后，AxF 会把常见 Scoop shims 目录加入当前进程 PATH，再检查一次 `rg`。如果仍然找不到，会提示重启 AxF。这个行为是故意的：比起悄悄回退到很慢的 Python 搜索，明确失败更容易定位 Windows 环境问题。

常见处理方式：

- 已经手动安装 `ripgrep`：确认启动 AxF 的同一个 PowerShell 中 `rg --version` 有输出。
- 刚安装 Scoop 或 ripgrep：重启前端服务，让进程继承最新 PATH。
- 公司策略禁止 `get.scoop.sh`：手动安装 Scoop/ripgrep，或把 `rg.exe` 所在目录加入系统 PATH。
- 源码目录在 OneDrive、网络盘或杀毒扫描很重的位置：迁移到本地 SSD 路径，或者为源码目录和 `workspace/` 配置 Defender 排除项。

### 10.2 Windows 性能差异

同一套流程在 macOS 上 2 分钟、Windows 上 20 分钟，通常不是 LLM 生成本身慢，而是前面的知识抽取慢，尤其是上层调用链：

- Windows 文件系统上大量小文件搜索比 macOS/Linux 慢。
- Defender 或第三方杀毒会扫描源码树、SQLite DB 和 workspace 临时产物。
- 源码放在 OneDrive、同步盘、网络盘时，每次读取都会有额外延迟。
- 没有 `rg` 时调用链搜索会退化，耗时会成倍增加。
- WSL 编译会多一次 Windows 路径到 WSL 路径转换，但这通常不是主要瓶颈。

优先优化顺序：

1. 确认 `rg --version` 在启动 AxF 的同一环境里可用。
2. 把 Linux 源码和 AxF `workspace/` 放到本地 SSD。
3. 给源码目录和 `workspace/` 加 Defender 排除项。
4. 如果不需要上层调用链，先不要勾选 `calls`。
5. 缩小 `--file` 过滤，避免同名函数和大范围搜索。
6. 降低 `--call-depth`、`--max-candidates`、`--max-functions` 等搜索上限。

## 11. 安全与隐私边界

- `.env.local` 被 `.gitignore` 忽略。
- 不打印 API key。
- 任务日志记录命令参数，但不记录 `Authorization` header。
- 前端 `模型设置` 弹窗保存的 API key 只写入 `.env.local`，不写入 `task.json`、`events.jsonl`、`task.log` 或 `llm_transcript.md`；opencode 模式不要求 API key。
- `llm_transcript.md` 在 `nga` 模式下记录所有交互；API、`opencode`、`hac`、`claude` 模式只记录包含 `harness.c` 的有效 harness 输出对应的 prompt 和模型回复。它不记录 API key、Authorization header 或 Chat Completions URL。
- `opencode_workspace/context/prompt.md` 会包含目标函数上下文和用户勾选的知识产物片段；它保存在本地任务目录，不会写入源码树。
- 只有同时勾选 `Harness 生成 Agent` 和具体 kRepo 产物时，该 kRepo 产物才会发送给模型服务。
- Linux 源码和 kRepo 来源代码不被修改。
- 生成物只写入 `workspace/`。

## 12. 如何新增 Agent

新增 Agent 的推荐步骤：

1. 在 `agents/<agent_name>/` 下创建 `agent.py`。
2. 提供命令行入口：

```bash
python -m agents.<agent_name>.agent --help
```

3. 使用文件作为输入输出协议。
4. 在 `frontend/server.py` 中增加一个 artifact 常量。
5. 在 `build_steps()` 中将用户选择映射为 Agent 命令。
6. 在 `_artifact_label()` 和 `_extra_artifacts_for_step()` 中登记产物展示。
7. 增加单元测试验证命令构造和产物映射。
8. 在 `docs/design.md` 记录 Agent 协议。

Agent 之间不要直接互相 import。调度层用文件和命令行连接它们。

## 13. 后续 Agent 建议

### 13.1 长时间 Fuzz 执行 Agent

职责：

- 读取已经生成并短跑验证过的 harness。
- 运行更长时间的 libFuzzer 任务，例如按时间、轮数或语料目录运行。
- 记录 sanitizer 输出。
- 判断 target function 是否实际到达。
- 输出执行报告。

建议输出：

```text
execution/build.log
execution/run.log
execution/result.json
```

### 13.2 长时间覆盖率与到达性 Agent

职责：

- 在现有 10 秒短跑覆盖率基础上，读取 fuzzer 二进制和运行产物。
- 针对更长 fuzz 任务记录 edge count、coverage artifact 或等价覆盖指标。
- 判断目标函数是否实际被调用。
- 禁止把只调用 mock、没有进入目标函数的 harness 标为成功。

建议输出：

```text
coverage/summary.json
coverage/report.md
```

### 13.3 Seed 生成 Agent

职责：

- 从 `params.txt`、`harness_spec.json` 和 `dict.txt` 生成初始语料。
- 输出 `seed_corpus/`。
- 后续根据覆盖率反馈优化 seed。

### 13.4 字典抽取 Agent

职责：

- 从 `report.json`、`params.txt`、源码包和枚举/常量定义中规则抽取 libFuzzer 字典候选。
- 让 LLM 只做筛选和解释，而不是完全凭空生成字典。
- 输出 `dict.txt` 和 `dict_report.md`。

### 13.5 Crash 分析 Agent

职责：

- 读取 sanitizer 日志、crash input、调用栈和 harness 代码。
- 判断真实缺陷、harness 误用、环境问题或低价值异常。
- 输出结构化 crash 报告。

## 14. 测试策略

当前测试重点：

- `build_steps()` 是否正确构造知识抽取命令。
- Harness 生成 Agent 是否只使用用户勾选的上下文步骤。
- `knowledge_dir` 是否只复用用户勾选的知识产物，并把复用文件复制到当前任务目录。
- Terminal CLI 是否能解析 `--artifacts`、校验非交互必填项，并同步写出 `task.json/events.jsonl/task.log`。
- Terminal `--async` 是否能创建任务目录、写入 `config.json` 并启动后台 worker。
- 模型返回 fenced JSON 时是否能解析。
- Agent 成功后是否把 `harness.c` 等文件映射为前端产物。
- 编译结果和 libFuzzer 试跑结果是否能转换成前端事件。
- 任务完成文案是否能显示具体状态，例如 `Harness 编译失败`。
- LLM 交互日志是否在 `nga` 模式下记录所有交互，在其他模式下只记录 assistant 返回 `harness.c` 的 system/user/assistant 内容，且不包含 API key。
- `nga` / `opencode` 模式是否创建隔离工作区，并用短指令让 CLI 读取 `context/prompt.md`。
- Windows 预检是否在缺少 `rg` 时优先用 Scoop 安装 ripgrep，缺少 Scoop 时先安装 Scoop。
- 编译失败后的 LLM 修复请求超时时，是否保留 `compile_failed` 结果和日志。

运行：

```bash
python -m unittest discover -s tests -v
```

带真实 Linux 源码树的知识库测试可设置：

```powershell
$env:KREPO_TEST_REPO='F:\path\to\linux-7.0'
python -m unittest tests.knowledge_base.test_cpp_meta_query -v
```

## 15. 当前已知限制

- 没有覆盖率统计。
- 没有 crash 分析。
- 没有自动判断 target function 是否实际到达。
- 编译成功不代表 harness 语义正确，只代表生成文件通过本地 clang 编译。
- 10 秒试跑只验证 fuzzer 可以启动并短时间运行，不代表 fuzzing 充分。
- `dict.txt` 当前主要由 LLM 根据上下文生成，还没有规则抽取兜底。
- `generated_harness.txt` 是预览文件，不是稳定机器协议。
- 模型输出质量会直接影响 harness 可用性。

这些限制应通过后续 Agent 逐步补齐，而不是继续扩大前端或知识库层职责。
