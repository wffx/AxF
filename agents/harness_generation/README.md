# Harness 生成 Agent

该 Agent 负责把用户选择的知识库产物转换为 `libFuzzer` fuzz 驱动。前端不会直接拼 prompt 或解析模型响应，只负责把任务目录中的上下文文件传给 Agent。
Agent 生成初稿后会立即尝试本地编译；如果编译失败，会把编译错误和当前核心文件片段反馈给 LLM，继续生成修复版，直到编译成功或达到最大修复轮数。修复轮允许 LLM 只返回需要修改的文件，Agent 会和上一轮产物合并，避免第二轮请求和响应都过大。编译成功后，Agent 会默认运行生成的 fuzzer 10 秒，确认二进制至少可以启动执行。

模型调用默认使用 OpenAI-compatible Chat Completions 的流式响应。`.env.local` 中可以配置完整 `CHAT_COMPLETIONS_URL`，也可以使用 `API_BASE_URL`/`BASE_URL` 形式，Agent 会自动补 `/chat/completions`。

可选输入。只有前端勾选并生成的文件会加入 Harness prompt；未勾选时不会生成，也不会发送给模型：

- `report.json`
- `<function>_subsource_bundle.c`
- `calls.txt`
- `params.txt`

如果任务配置了 `knowledge_dir`，复用逻辑由调度层完成：旧目录中的知识产物会先复制到当前任务目录，然后再把当前任务目录中的文件路径传给 Agent。Agent 不直接读取旧目录，也不会自动补齐未勾选的知识产物。

没有任何 kRepo 上下文时，Agent 仍会调用 LLM，并要求模型基于目标函数名、文件路径和通用 C/libFuzzer 经验生成最小可编译 harness。`unsupported` 不再作为“上下文不足”的默认结果；只有模型没有生成 `harness.c` 时，编译才会被跳过。

输出：

- `harness/harness.c`
- `harness/mocks.h`
- `harness/mocks.c`
- `harness/build.sh`
- `harness/build.ps1`
- `harness/harness_spec.json`
- `harness/compile.log`
- `harness/run.log`
- `harness/llm_transcript.md`
- `generated_harness.txt`

成功判定：

- `harness_spec.json` 中 `compile.status == "success"` 表示编译通过。
- `harness_spec.json` 中 `run.status == "success"` 表示 10 秒 libFuzzer 运行完成；前端成功态显示为 `编译通过`。
- `status == "compile_failed"`、`run.status == "failed"`、`run.status == "timeout"`、`status == "generated"` 都不会被前端标记为完整成功。

调试顺序：

1. 先看 `harness_spec.json` 的 `status`、`compile`、`run`。
2. 编译失败看 `compile.log` 和 `compile_attempt_<n>.log`。
3. LLM 生成或修复异常看 `llm_transcript.md`。
4. 需要手动复现编译时再看 `build.sh` 或 `build.ps1`。

命令行入口：

```bash
python -m agents.harness_generation.agent --help
```

最小调用形态可以不传任何 kRepo 上下文，只依赖函数名、源码根目录和可选文件过滤：

```bash
python -m agents.harness_generation.agent \
  --function can_send \
  --repo ../linux-7.0 \
  --file net/can/af_can.c \
  --task-dir workspace/web/tasks/<task_id> \
  --out workspace/web/tasks/<task_id>/harness \
  --artifact workspace/web/tasks/<task_id>/generated_harness.txt
```

带知识上下文时只传用户选择的文件：

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

常用模型和编译相关参数：

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

`--clang-mode native` 是默认值，在当前系统直接运行 clang。Windows 端如果前端和任务目录在 Windows、clang 在 WSL 中，使用 `--clang-mode wsl --clang /usr/bin/clang`；Agent 会通过 `wsl wslpath` 转换任务目录，并在 WSL 内生成 Linux 二进制 `fuzzer`。
