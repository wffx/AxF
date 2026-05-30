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

- 本地 Web 控制台，用户可以提交目标源码目录、函数名、文件过滤和 LLM 配置。
- kRepo/AxF 知识抽取和 AxF 后续流程在前端表单中分区展示。
- kRepo 知识产物包括 Markdown 报告、JSON 报告、源码分析包、下游源码包、上层调用链和入参约束。
- Harness 生成 Agent 从 kRepo 知识产物构造 prompt，并调用兼容 Chat Completions 的 LLM 服务。
- LLM 输出 machine-readable JSON，Agent 从中写出 `harness.c`、`mocks.h`、`mocks.c`、构建脚本、字典和规格文件。
- Agent 自动使用 clang 编译生成的 harness。
- 编译失败时，Agent 会把编译诊断和当前生成文件反馈给 LLM 修复，最多按配置重试。
- 编译成功后，Agent 默认运行生成的 libFuzzer 二进制 10 秒。
- 任务事件会展示 kRepo 抽取、Harness 编译和 libFuzzer 试跑的摘要。
- 任务状态会根据 `harness_spec.json` 派生为 `抽取完成`、`Harness 已生成`、`编译通过`、`编译失败`、`试跑通过`、`试跑失败` 等细状态。
- 产物页按来源分组为 `kRepo 知识产物` 和 `Harness 生成 Agent 产物`。
- LLM 交互内容会写入 `harness/llm_transcript.md`，方便审计 prompt 和模型回复。

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

- `frontend/`：本地 Web 控制台和轻量任务调度。
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
    "function": "can_send",
    "file": "net/can/af_can.c",
    "artifacts": ["report_json", "subsource", "calls", "params", "harness_generation_agent"],
    "model": "glm-5.1",
    "api_key_env": "API_KEY"
  },
  "steps": []
}
```

`events.jsonl` 记录面向 UI 的事件流：

```json
{"ts": 1780110187.69, "phase": "report_json", "message": "JSON 报告 已完成", "artifact": "report_json"}
```

Harness 生成 Agent 内部完成后，调度层会读取 `harness_spec.json`，把编译结果和 libFuzzer 10 秒试跑结果展开成事件，例如 `Harness 编译通过`、`libFuzzer 试跑通过`。完整输出仍以 `compile.log` 和 `run.log` 为准。

`task.log` 记录实际执行命令和进程输出。

## 5. 前端与任务调度

入口：

```bash
python -m frontend.server --host 127.0.0.1 --port 8787 --open
```

核心文件：

- `frontend/server.py`
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

如果勾选 `Harness 生成 Agent`，调度层会自动补齐上下文步骤：

```text
report_json
subsource
calls
params
harness_generation_agent
```

这是因为 Harness 生成 Agent 依赖这四个输入文件：

- `report.json`
- `<function>_subsource_bundle.c`
- `calls.txt`
- `params.txt`

### 5.4 前端表单分区

前端表单按照职责分成五块：

`目标代码`
: 配置源码根目录、可选 `BROWSE.VC.DB`、函数名和文件过滤。文件过滤用于解决同名函数问题，例如 `net/can/af_can.c`。

`LLM 生成`
: 配置模型名、Chat Completions URL、API key 环境变量名、模型超时秒数、模型重试次数、clang 路径、最大修复轮数和编译超时秒数。API key 的值不在前端输入，只通过 `.env.local` 或环境变量读取。

`kRepo 知识抽取`
: 只包含 kRepo/AxF 知识产物选择，例如 Markdown 报告、JSON 报告、下游源码包、上层调用链和入参约束。这里不再混入 Harness Agent。

`AxF 后续流程`
: 当前包含 `生成 Fuzz Harness`。未来新增执行、覆盖率、crash 分析等 Agent 时，应放在这里或拆成新的 AxF 流程区。

`kRepo 抽取限制`
: 只影响知识抽取阶段，例如依赖上限、片段行数、下游深度、函数上限、调用链深度和同名候选上限。

### 5.5 前端任务状态

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
试跑通过
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
[libFuzzer 试跑] libFuzzer 试跑通过（10 秒），日志见 10 秒运行日志
```

完整命令输出写入 `task.log`。Harness Agent 内部的长输出不直接刷满事件页：

- 编译完整输出看 `harness/compile.log` 和 `harness/compile_attempt_<n>.log`。
- libFuzzer 完整输出看 `harness/run.log`。
- LLM 完整交互看 `harness/llm_transcript.md`。

### 5.7 产物分组

产物页按来源分组：

`kRepo 知识产物`
: `report.md`、`report.json`、源码分析包、下游源码包、调用链和入参约束。

`Harness 生成 Agent 产物`
: `generated_harness.txt`、`harness.c`、mock 文件、构建脚本、`harness_spec.json`、`dict.txt`、编译日志、试跑日志和 LLM 交互日志。

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

Harness 生成 Agent 的最小命令形式：

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
--timeout
--max-retries
--no-stream
--clang
--max-repair-rounds
--compile-timeout
--skip-compile
--run-seconds
--skip-run
```

### 7.3 环境变量

默认读取仓库根目录下的 `.env.local` 和 `.env`：

```text
API_KEY=...
CHAT_COMPLETIONS_URL=...
MODEL=glm-5.1
```

也支持 shell 风格：

```bash
export API_KEY=...
```

密钥不进入任务目录，不进入日志，不进入 Git。

LLM 配置在前端中以 `LLM 生成` 区域呈现，含义如下：

```text
模型              -> --model，也可来自 MODEL
Chat Completions URL -> --chat-url，也可来自 CHAT_COMPLETIONS_URL/API_BASE_URL/BASE_URL
API Key 环境变量  -> --api-key-env，默认 API_KEY
模型超时秒数      -> --timeout，默认 300
模型重试次数      -> --max-retries，默认 2
```

这里参考 OpenDeepHole 一类工具把模型连接参数收敛到一个配置区，但 AxF 不直接在前端保存 API key 值，只保存环境变量名。这样 `.env.local` 可以留在本地并被 `.gitignore` 忽略。

模型请求默认使用 OpenAI-compatible Chat Completions 的流式响应，即请求体包含 `"stream": true`，并按 SSE `data:` chunk 拼接 `choices[].delta.content`。这对 harness 修复轮很重要：修复轮输出可能较长，非流式调用必须等完整响应结束才返回，容易触发 read timeout。需要排查服务端兼容性时，可在命令行加 `--no-stream` 临时退回非流式。

URL 也做了兼容处理：如果配置的是完整 `.../chat/completions`，Agent 原样使用；如果配置的是 `API_BASE_URL`/`BASE_URL` 或类似 `https://host/api/v1` 的 base URL，Agent 会自动补 `/chat/completions`。

和编译修复相关的配置也在同一区域：

```text
Clang 路径       -> --clang，留空时使用 CLANG 或 PATH 中的 clang
最大修复轮数     -> --max-repair-rounds，默认 3
编译超时秒数     -> --compile-timeout，默认 60
```

### 7.4 Prompt 输入

Agent 发送给模型的上下文包含：

- 目标函数名。
- 文件路径。
- 源码根目录。
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

### 7.4.1 LLM 交互日志

每次请求 LLM 时，Agent 都会追加写入：

```text
harness/llm_transcript.md
```

该文件包含：

- 本轮 interaction 名称，例如 `initial generation` 或 `repair attempt 1`。
- 模型名。
- system 消息原文。
- user prompt 原文。
- assistant 返回内容。
- 如果请求失败，记录错误信息。

不写入：

- API key。
- Authorization header。
- Chat Completions URL。

同时，`task.log` 中会打印交互日志路径，例如：

```text
LLM 交互 [initial generation] 请求已记录：.../harness/llm_transcript.md
LLM 交互 [initial generation] 响应已记录：.../harness/llm_transcript.md
```

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

### 7.6 编译与修复循环

Agent 写出初稿后，会在 `harness/` 目录下执行编译验证：

```text
clang -std=gnu11 -fsanitize=fuzzer,address,undefined harness.c mocks.c -I. -o fuzzer
```

`--clang` 可以指定 clang 路径；未指定时依次使用环境变量 `CLANG` 和 PATH 中的 `clang`。

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

每个 LLM 请求默认超时 300 秒，并在超时或网络 timeout 类错误时最多重试 2 次。每次尝试都会写入 `llm_transcript.md`，例如 `repair attempt 1 request 1/3`、`repair attempt 1 request 2/3`。

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

如果模型明确返回 `unsupported` 或没有生成 `harness.c`，编译会标记为 `skipped`。

### 7.7 10 秒试跑

如果编译成功，Agent 默认在 `harness/` 目录下执行：

```text
./fuzzer -max_total_time=10 -max_len=4096 seed_corpus
```

Windows 环境下会执行 `.\fuzzer.exe`。试跑日志写入：

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
llm_transcript.md
llm_prompt.txt
llm_response.json
llm_repair_prompt_<n>.txt
llm_repair_response_<n>.json
seed_corpus/README.txt
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
- `LLM 交互日志`

## 8. 产物说明

一次任务会产生两类产物：知识库上下文和 Agent 生成结果。

### 8.1 知识库上下文

`report.json`
: 结构化函数报告。包含目标函数位置、源码、参数、依赖、调用信息等。主要给 Agent 当机器可读上下文。

`<function>_subsource_bundle.c`
: 目标函数和下游子函数源码包。Harness 生成 Agent 主要依赖它判断目标函数内部会调用什么、需要构造什么输入、需要 mock 什么内核依赖。

`calls.txt`
: 上层调用链。用于理解目标函数的真实调用场景，例如谁会调用它、调用路径大致是什么。

`params.txt`
: 参数约束。用于记录参数类型、空指针检查、长度字段、分支条件和从源码中推断出的输入限制。

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

`harness/llm_transcript.md`
: LLM 交互日志。记录每次请求中的 system/user 消息和模型返回的 assistant 内容，包括首次生成和后续修复轮。API key 不写入该文件，Chat Completions URL 不写入该文件。

### 8.3 优先查看顺序

调试一次生成结果时，推荐按以下顺序查看：

1. `generated_harness.txt`：先看 Agent 的总体判断。
2. `harness/harness.c`：确认 fuzz 入口和目标函数调用。
3. `harness/mocks.c` / `harness/mocks.h`：确认 mock 是否过度简化或明显错误。
4. `harness/harness_spec.json`：确认分类、输入计划和状态。
5. `harness/compile.log`：确认编译是否成功；失败时看具体错误。
6. `harness/run.log`：编译成功后确认 10 秒试跑是否通过。
7. `harness/llm_transcript.md`：确认 Agent 给 LLM 的完整 prompt 和模型返回内容。
8. `build.sh` 或 `build.ps1`：需要手动复现编译时再看。

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
- 函数名找不到。
- 同名函数未用 `--file` 精确过滤。
- `API_KEY` 没有设置。
- `CHAT_COMPLETIONS_URL` 没有设置。
- 模型返回不是合法 JSON。
- 模型输出的文件路径非法。
- 编译失败后请求 LLM 修复超时。
- 生成的 harness 编译失败。
- 编译通过后 10 秒试跑失败或超时。

Agent 会拒绝写出逃逸输出目录的路径，例如：

```text
../../bad.c
```

## 10. Windows 支持

当前 Windows 支持范围：

- 前端服务可用 `python -m frontend.server` 启动。
- `.env.local` 可直接放在仓库根目录。
- 也可用 PowerShell 环境变量：

```powershell
$env:API_KEY='...'
$env:CHAT_COMPLETIONS_URL='...'
$env:MODEL='glm-5.1'
```

Agent 会要求模型同时输出：

- `build.sh`
- `build.ps1`

知识库查询的文件过滤需要兼容：

```text
net/can/af_can.c
net\can\af_can.c
```

自动编译验证运行在当前执行 Agent 的机器上；如果 Agent 在 macOS/Linux 上运行，它只验证当前平台的 clang 编译结果。Windows 下可用 `build.ps1` 手动复现，或后续增加跨平台执行 Agent。

## 11. 安全与隐私边界

- `.env.local` 被 `.gitignore` 忽略。
- 不打印 API key。
- 任务日志记录命令参数，但不记录 `Authorization` header。
- `llm_transcript.md` 记录 prompt 和模型回复，但不记录 API key、Authorization header 或 Chat Completions URL。
- 只有勾选 `Harness 生成 Agent` 时，知识库上下文才会发送给模型服务。
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

### 13.2 覆盖率与到达性 Agent

职责：

- 读取 fuzzer 二进制和运行产物。
- 记录 edge count、coverage artifact 或等价覆盖指标。
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
- 勾选 Harness 生成 Agent 时是否自动补齐上下文步骤。
- 模型返回 fenced JSON 时是否能解析。
- Agent 成功后是否把 `harness.c` 等文件映射为前端产物。
- 编译结果和 libFuzzer 试跑结果是否能转换成前端事件。
- 任务完成文案是否能显示具体状态，例如 `Harness 编译失败`。
- LLM 交互日志是否记录 system/user/assistant 内容且不包含 API key。
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
