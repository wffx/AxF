# Harness 生成 Agent

该 Agent 负责把任务目录里的目标函数信息和可选知识产物转换为 `libFuzzer` harness。前端和 Terminal 不直接拼 prompt、不解析模型 JSON、不编译 fuzz target；它们只负责把当前任务选择的文件路径传给 Agent，并读取 Agent 写出的产物。

完整链路：

```text
目标函数信息 + 可选 kRepo/AxF 知识产物
  -> 构造 harness prompt
  -> API 或 opencode CLI 调用模型
  -> 解析 JSON
  -> 写出 harness.c / mocks / build scripts / spec
  -> clang 编译
  -> 编译失败时让模型修复
  -> 编译成功后 libFuzzer 短跑
```

## 输入边界

必填输入：

- `--function`：目标函数名。
- `--repo`：源码根目录。
- `--task-dir`：当前任务目录。
- `--out`：harness 输出目录，通常是 `workspace/<web|terminal>/tasks/<task_id>/harness`。
- `--artifact`：汇总预览文件，通常是 `workspace/<web|terminal>/tasks/<task_id>/generated_harness.txt`。

可选输入：

- `--file`：目标函数所在文件过滤，用于区分同名函数。
- `--report-json`：结构化函数报告。
- `--subsource`：目标函数和下游子函数源码包。
- `--calls`：上层调用链。
- `--params`：参数约束。

只有用户在本次任务中勾选的知识产物才会传进来。配置了 `knowledge_dir` 时，复用发生在调度层：旧目录里的文件会先复制到当前任务目录，Agent 仍只读取当前任务目录中的路径。Agent 不会自己去旧任务目录找文件，也不会自动补齐用户没勾选的产物。

没有任何 kRepo/AxF 上下文时，Agent 仍会调用模型。Prompt 会明确告诉模型：目标函数名、文件路径和源码根目录已经给出，额外知识产物只是可选参考，不能因为“缺少 kRepo/AxF 知识产物”或“缺少 harness 规则库路径”直接拒绝生成。

## 模型模式

### API 模式

默认是 OpenAI-compatible Chat Completions：

```text
LLM_MODE=api
API_KEY=...
CHAT_COMPLETIONS_URL=https://provider.example/v1/chat/completions
MODEL=glm-5.1
```

也可以只配置 base URL：

```text
API_BASE_URL=https://provider.example/v1
```

Agent 会自动补 `/chat/completions`。默认请求体包含 `"stream": true`，并按 SSE `data:` chunk 拼接 `choices[].delta.content`。如果服务端流式兼容性有问题，可以加 `--no-stream` 临时走非流式。

### opencode CLI 模式

CLI 模式不读取 API key：

```text
LLM_MODE=opencode
OPENCODE_TOOL=nga
OPENCODE_EXECUTABLE=nga
OPENCODE_MODEL=anthropic/claude-sonnet-4
```

`OPENCODE_TOOL` 支持：

- `nga`
- `opencode`
- `hac`
- `claude`

`nga` 和 `opencode` 会使用隔离 code-agent 工作区。Agent 不把完整 prompt 塞进命令行参数，也不使用 `--file` 传 prompt，而是创建：

```text
harness/opencode_workspace/
  TASK.md
  context/
    prompt.md
    prompt_request_<n>.md
```

实际命令形态：

```text
nga run --dir <task_dir>/harness/opencode_workspace --model <模型> <短指令>
opencode run --dir <task_dir>/harness/opencode_workspace --model <模型> <短指令>
```

`context/prompt.md` 是完整 prompt。`TASK.md` 和命令行里的短指令只要求 CLI 读取 `context/prompt.md` 并输出 JSON。这样可以避免 Windows 命令行过长，也可以减少 code agent 访问源码树外路径时触发的权限请求。

`hac` 和 `claude` 当前仍走直接 prompt：

```text
hac --model <模型> -p <prompt>
claude -p --model <模型> <prompt>
```

Windows 上如果 `OPENCODE_EXECUTABLE=nga` 找不到，填完整 `.cmd` 路径，例如：

```text
OPENCODE_EXECUTABLE=C:/Users/<user>/scoop/shims/nga.cmd
```

这里用的是启动 AxF 的进程 PATH，不是后来新开的 PowerShell PATH。`Get-Command nga` 有输出但页面仍提示未找到时，优先填完整 `.cmd` 路径并重启前端服务。

## Prompt 约束

Agent 发送给模型的 prompt 包含：

- 目标函数名、文件路径、源码根目录。
- 可选 `report.json`、`subsource`、`calls.txt`、`params.txt`。
- JSON 输出 schema。
- 必须生成的文件清单。
- 禁止因为缺少知识产物而拒绝生成的规则。

上下文长度上限：

```text
report.json: 50000 chars
subsource:   90000 chars
calls:       24000 chars
params:      24000 chars
```

超过上限时会截断，并在 prompt 中标记 omitted chars。截断不代表丢失目标函数基本信息，目标函数名、文件路径和源码根目录始终会保留。

如果模型返回 JSON 但没有 `harness.c`，且理由是“需要目标函数信息”“没有找到 kRepo/AxF 知识产物”“缺少 harness 生成规则知识库路径”这类上下文拒绝，Agent 会触发一次更强约束的重试。只有真实依赖不可模拟硬件、真实内核并发语义或无法近似的函数指针分派时，`unsupported` / `needs_manual_fixture` 才是合理结果。

## 模型输出协议

模型必须输出一个 JSON 对象。稳定路径下不要输出 Markdown、解释文字或多个 JSON。

核心字段：

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

解析器会容忍少量常见问题：

- 外层 Markdown code fence。
- JSON 前后有 CLI 日志。
- 裸 key、单引号字符串、尾逗号。
- 字符串里出现未转义控制字符。

解析器会从混合输出中提取括号平衡的 JSON object 再尝试解析。它无法修复空输出、完全自然语言输出、被截断的 JSON，或第一个 JSON 对象不是 harness payload 的情况。

## 编译与修复

生成初稿后，Agent 会把 `files` 写入 `harness/`，再用 clang 编译：

```text
clang -std=gnu11 -fsanitize=fuzzer,address,undefined harness.c mocks.c -I. -o fuzzer
```

默认 `--clang-mode native`。Windows 上如果 clang 在 WSL 里，使用：

```text
--clang-mode wsl --clang /usr/bin/clang
```

WSL 模式会执行 `wsl wslpath -a <task dir>`，再在 WSL 内进入 harness 目录编译和试跑。native Windows 模式生成 `fuzzer.exe`；WSL 模式生成 Linux 二进制 `fuzzer`。

编译失败时，Agent 会把这些内容发回模型：

- 编译命令。
- 返回码。
- 编译诊断。
- 当前 `harness.c`、`mocks.h`、`mocks.c` 的短片段。
- `calls.txt` 和 `params.txt` 的裁剪片段。

修复轮只要求模型返回需要修改的文件。Agent 会把修复 JSON 和上一轮产物合并，再次编译。默认最多修复 3 轮，可用 `--max-repair-rounds` 调整。

编译成功后默认运行：

```text
./fuzzer -max_total_time=10 -max_len=4096 seed_corpus
```

可用 `--run-seconds` 调整时长，用 `--skip-run` 跳过短跑。

## 输出文件

常见输出：

```text
generated_harness.txt
harness/
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

有条件输出：

```text
harness/llm_transcript.md
harness/nga_interactions/<attempt>/prompt.md
harness/nga_interactions/<attempt>/stdout.rawoutput
harness/nga_interactions/<attempt>/stderr.rawoutput
harness/nga_interactions/<attempt>/combined.rawoutput
harness/nga_interactions/<attempt>/metadata.json
harness/opencode_workspace/TASK.md
harness/opencode_workspace/context/prompt.md
harness/opencode_workspace/context/prompt_request_<n>.md
```

`llm_transcript.md` 的记录策略按模型通道区分。`nga` 模式会记录所有交互，包括每次请求、CLI 原始 stdout/stderr、非零退出码、非法 JSON、权限提示和重试。API、`opencode`、`hac`、`claude` 模式只在模型回复包含 `harness.c` 时写入；这些模式下 transcript 不存在时，说明没有出现可记录的 harness 输出，不等于模型没有被调用。

`nga_interactions/` 参考 oss-fuzz-gen 的 raw output 做法，把每次尝试拆成独立目录，例如 `01_initial_generation/`。其中 `prompt.md` 是本次实际请求内容，`stdout.rawoutput` 和 `stderr.rawoutput` 分别是 CLI 原始 stdout/stderr，`combined.rawoutput` 是 Agent 参与 JSON 解析的合并输出，`metadata.json` 记录命令、模型、退出码和错误。

排查 CLI 启动失败、权限提示、非法 JSON 和上下文拒绝时，优先看 `task.log`、`llm_response.json` 和 `opencode_workspace/context/prompt.md`；如果是 `nga` 模式，也直接看 `llm_transcript.md` 和 `nga_interactions/<attempt>/combined.rawoutput`，里面会保留原始交互。

## 成功判定

- `harness_spec.json` 中 `compile.status == "success"` 表示编译通过。
- `harness_spec.json` 中 `run.status == "success"` 表示 10 秒 libFuzzer 运行完成。
- 前端完整成功标准是 `run.status == "success"`。
- `compile_failed`、`run.status == "failed"`、`run.status == "timeout"`、`generated`、`unsupported`、`needs_manual_fixture` 都不会被前端标记为完整成功。

## 排障顺序

1. 看 `harness/harness_spec.json`：确认 `status`、`compile`、`run`。
2. 编译失败看 `harness/compile.log` 和 `harness/compile_attempt_<n>.log`。
3. 试跑失败看 `harness/run.log`。
4. `nga` / `opencode` 模式看 `harness/opencode_workspace/context/prompt.md`，确认目标函数信息和可选知识产物是否已经在 prompt 里。
5. `nga` 模式直接看 `harness/nga_interactions/<attempt>/combined.rawoutput` 和 `harness/llm_transcript.md`；其他模式在模型输出包含 `harness.c` 时看 transcript。
6. CLI 未启动、权限请求、非法 JSON、空输出或重试次数看 `task.log`。
7. 需要手动复现编译时再看 `build.sh` 或 `build.ps1`。

常见现象：

- `保存失败: 未找到`：模型设置里的 CLI executable 对当前前端进程不可见，填完整 `.cmd` 路径并重启服务。
- `WinError 2`：Windows 进程找不到 executable。不要只看新 PowerShell 的 `Get-Command`，要保证 AxF 进程 PATH 或完整路径正确。
- `文件名或扩展名太长`：通常说明还在跑旧版本，或某个工具仍把完整 prompt 当命令行参数。更新后 `nga` / `opencode` 应通过 `opencode_workspace/context/prompt.md` 传 prompt。
- `File not found: 请读取本次命令通过 --file 附加的 prompt 文件`：不要给 `nga` / `opencode` 传 `--file`。当前实现使用工作区里的 `context/prompt.md`。
- `Expecting value: line 1 column 1`：CLI stdout 为空，或只有非 JSON 内容。先看 `task.log` 里的 CLI stderr。
- `Extra data`：通常是 JSON 后面夹了日志；当前解析器会尝试提取第一个平衡 JSON。如果仍失败，说明可提取对象不是合法 harness payload。
- 模型说需要目标函数信息或 kRepo/AxF 知识产物：先看 `opencode_workspace/context/prompt.md`，这些信息通常已经在 prompt 中；如果模型仍拒绝，Agent 会按无效拒绝重试。

## 命令行入口

查看参数：

```bash
python -m agents.harness_generation.agent --help
```

最小调用形态：

```bash
python -m agents.harness_generation.agent \
  --function can_send \
  --repo ../linux-7.0 \
  --file net/can/af_can.c \
  --task-dir workspace/web/tasks/<task_id> \
  --out workspace/web/tasks/<task_id>/harness \
  --artifact workspace/web/tasks/<task_id>/generated_harness.txt
```

带知识上下文：

```bash
python -m agents.harness_generation.agent \
  --function can_send \
  --repo ../linux-7.0 \
  --file net/can/af_can.c \
  --task-dir workspace/web/tasks/<task_id> \
  --out workspace/web/tasks/<task_id>/harness \
  --artifact workspace/web/tasks/<task_id>/generated_harness.txt \
  --report-json workspace/web/tasks/<task_id>/report.json \
  --params workspace/web/tasks/<task_id>/params.txt
```

opencode CLI 模式：

```bash
python -m agents.harness_generation.agent \
  --function can_send \
  --repo ../linux-7.0 \
  --file net/can/af_can.c \
  --task-dir workspace/web/tasks/<task_id> \
  --out workspace/web/tasks/<task_id>/harness \
  --artifact workspace/web/tasks/<task_id>/generated_harness.txt \
  --llm-mode opencode \
  --opencode-tool nga \
  --opencode-executable nga \
  --opencode-model anthropic/claude-sonnet-4
```

常用参数：

```text
--timeout 300
--max-retries 2
--no-stream
--clang PATH
--clang-mode native|wsl
--max-repair-rounds 3
--compile-timeout 60
--skip-compile
--run-seconds 10
--skip-run
```
