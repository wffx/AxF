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

## 常用参数

```text
--repo PATH
--db PATH
--knowledge-dir PATH
--function NAME
--file PATH
--artifacts IDS
--model MODEL
--chat-url URL
--api-key-env NAME
--model-timeout SECONDS
--model-max-retries N
--clang PATH
--max-repair-rounds N
--compile-timeout SECONDS
--workspace PATH
--non-interactive
--async
```

## 输出目录

默认输出目录：

```text
workspace/terminal/tasks/<task_id>/
```

可通过 `--workspace` 修改。

## 退出码

- `0`：任务成功，或异步任务提交成功。
- `2`：参数或任务配置错误。
- `127`：异步 worker 或子进程启动失败。
- 其他非零值：pipeline 步骤失败时透传对应子进程退出码。
- `130`：同步任务被 `Ctrl+C` 中断。
