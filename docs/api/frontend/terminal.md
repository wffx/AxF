# Terminal CLI

`frontend.terminal` 提供不依赖 Web 服务的本地任务入口。它和 Web 控制台共享 `frontend.server.build_steps()`，因此产物选择、知识库复用、Harness prompt 输入规则保持一致。

## 入口

交互模式：

```bash
python -m frontend.terminal run
```

脚本模式：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --repo ../linux-7.0 \
  --function can_send \
  --file net/can/af_can.c \
  --artifacts report_json,subsource,params,harness_generation_agent
```

脚本模式下 `--repo`、`--function`、`--artifacts` 必填。缺少任一必填项会返回非零退出码。

Terminal 和 Web 共用同一套任务执行逻辑，所以脚本模式不是“轻量版”。它同样会：

- 读取仓库根目录 `.env.local` / `.env`。
- 按本次选择生成或复用知识产物。
- 在 Windows 上执行 `rg` / Scoop 预检。
- 调用 Harness 生成 Agent。
- 读取 `harness_spec.json` 派生最终成功或失败。

## 产物选择

`--artifacts` 接收逗号分隔的产物 id 或交互菜单编号：

```text
report_md
report_json
subsource
source
calls
params
harness_generation_agent
```

语义：

- `report_md`、`source`：只生成或复用产物，不加入 Harness prompt。
- `report_json`、`subsource`、`calls`、`params`：生成或复用产物；如果同时选择 `harness_generation_agent`，会加入 Harness prompt。
- 未选择项：不生成、不复用、不传给模型。
- 只选择 `harness_generation_agent`：不会自动补齐任何 kRepo 上下文。

## 复用知识库产物

`--knowledge-dir` 指向已有任务目录或其他包含知识产物的目录：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --repo ../linux-7.0 \
  --function can_send \
  --file net/can/af_can.c \
  --knowledge-dir workspace/web/tasks/<old_task_id> \
  --artifacts report_json,params,harness_generation_agent
```

复用规则：

- 只检查本次选择的知识产物。
- 如果旧目录存在对应文件，会复制到当前任务目录并跳过 kRepo 命令。
- 如果旧目录缺少对应文件，会按正常流程重新抽取。
- Harness Agent 始终读取当前任务目录中的文件。

默认文件名：

```text
report_md   -> report.md
report_json -> report.json
source      -> <function>_source_bundle.c
subsource   -> <function>_subsource_bundle.c
calls       -> calls.txt
params      -> params.txt
```

## 异步模式

默认情况下 Terminal 会同步执行任务并等待结束。加 `--async` 后，命令会创建任务目录、写入 `config.json`，启动后台 worker 后立即返回：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --async \
  --repo ../linux-7.0 \
  --function can_send \
  --artifacts report_json
```

提交成功会打印：

```text
已提交异步任务：<task_id>
产物目录：workspace/terminal/tasks/<task_id>
后台日志：workspace/terminal/tasks/<task_id>/terminal.log
```

后台 worker 会继续写：

```text
config.json
task.json
task.log
events.jsonl
<selected knowledge artifacts>
harness/
```

异步模式下，提交命令本身返回 `0` 只表示后台任务创建成功，不代表 harness 生成成功。真实状态要看对应任务目录里的 `task.json`、`events.jsonl`、`task.log` 和 `harness/harness_spec.json`。

## 模型配置

Terminal 可以显式覆盖 `.env.local` 中的模型设置：

```bash
python -m frontend.terminal run \
  --non-interactive \
  --repo ../linux-7.0 \
  --function can_send \
  --file net/can/af_can.c \
  --artifacts report_json,params,harness_generation_agent \
  --llm-mode api \
  --model glm-5.1 \
  --chat-url https://provider.example/v1/chat/completions \
  --api-key-env API_KEY
```

API key 本身不通过命令行传值，只传环境变量名。这样 `task.log` 里不会出现真实密钥。

opencode CLI 示例：

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

`nga` / `opencode` 模式会在当前任务目录创建：

```text
harness/opencode_workspace/context/prompt.md
```

CLI 命令行里传的是短指令，完整 prompt 在这个文件里。Windows 上如果 executable 解析失败，优先把 `--opencode-executable` 改成完整 `.cmd` 路径，例如 `C:/Users/<user>/scoop/shims/nga.cmd`。

## Windows 预检

Windows 下任务开始执行步骤前会检查 `rg`：

```text
rg 已存在
  -> 继续任务。

rg 不存在，Scoop 存在
  -> scoop install ripgrep。

rg 和 Scoop 都不存在
  -> PowerShell 安装 Scoop，再安装 ripgrep。
```

这一步是为了避免上层调用链抽取在 Windows 大源码树上退化得过慢。自动安装失败时，Terminal 会打印 `错误：...`，事件流也会记录失败原因。常见处理：

- `rg --version` 在当前 PowerShell 中没有输出：先安装 ripgrep，或让自动安装完成后重启 Terminal。
- `get.scoop.sh` 无法访问：手动安装 Scoop/ripgrep。
- 安装后仍找不到 `rg`：确认 Scoop shims 目录在 PATH 中，重新打开 PowerShell。
- Windows 仍明显慢：把源码和 `workspace/` 放本地 SSD，避免 OneDrive/网络盘，给源码目录和 workspace 加 Defender 排除项。

## 知识抽取规模参数

大仓库上耗时通常集中在知识抽取，尤其是 `calls`。Terminal 暴露了和 Web 表单一致的上限参数：

```text
--max-deps N             report/source 中跟随的依赖数量上限
--max-snippet-lines N    单个符号源码片段最大行数
--max-depth N            下游子函数递归深度
--max-functions N        下游源码包最多包含多少个函数
--call-depth N           上层调用链向上扩展深度
--max-candidates N       同名函数或调用者候选上限
```

实用建议：

- 只想快速验证 harness 链路时，先选 `report_json,params,harness_generation_agent`，不要选 `calls`。
- 目标函数同名很多时，加 `--file net/can/af_can.c`。
- Windows 上 `calls` 慢时，先降低 `--call-depth` 和 `--max-candidates`。
- 已经有旧任务产物时，用 `--knowledge-dir` 复用，避免重复跑 kRepo。

## 常用参数

```text
--repo PATH
--db PATH
--knowledge-dir PATH
--function NAME
--file PATH
--artifacts IDS
--llm-mode api|opencode
--model MODEL
--chat-url URL
--api-key-env NAME
--opencode-tool nga|opencode|hac|claude
--opencode-executable PATH
--opencode-model MODEL
--model-timeout SECONDS
--model-max-retries N
--clang PATH
--clang-mode native|wsl
--max-repair-rounds N
--compile-timeout SECONDS
--max-deps N
--max-snippet-lines N
--max-depth N
--max-functions N
--call-depth N
--max-candidates N
--workspace PATH
--non-interactive
--async
```

Windows 端如果使用 WSL 内的 clang，可以加：

```powershell
--clang-mode wsl --clang /usr/bin/clang
```

此时 Terminal 仍在 Windows 上运行，任务目录和 `BROWSE.VC.DB` 使用 Windows 路径；Harness 编译和试跑会通过 WSL 执行。

## 输出目录

默认输出目录：

```text
workspace/terminal/tasks/<task_id>/
```

可通过 `--workspace` 修改。

常见排障文件：

```text
task.log
events.jsonl
harness/harness_spec.json
harness/compile.log
harness/run.log
harness/llm_response.json
harness/llm_transcript.md
harness/nga_interactions/<attempt>/combined.rawoutput
harness/opencode_workspace/context/prompt.md
```

`llm_transcript.md` 的含义取决于模型通道：`nga` 模式会记录所有交互，包括 CLI 原始 stdout/stderr、错误和重试；API、`opencode`、`hac`、`claude` 模式只在模型回复包含 `harness.c` 时出现。`nga_interactions/<attempt>/` 参考 oss-fuzz-gen 的 raw output 做法，保存每次尝试的 `prompt.md`、`stdout.rawoutput`、`stderr.rawoutput`、`combined.rawoutput` 和 `metadata.json`。`nga` / `opencode` 模式下，如果模型说缺少目标函数信息或 kRepo/AxF 知识产物，先看 `harness/opencode_workspace/context/prompt.md`，确认实际 prompt 中是否已经包含这些信息。

## 退出码

- `0`：任务成功，或异步任务提交成功。
- `2`：参数或任务配置错误。
- `127`：异步 worker 或子进程启动失败。
- 其他非零值：pipeline 步骤失败时透传对应子进程退出码。
- `130`：同步任务被 `Ctrl+C` 中断。
