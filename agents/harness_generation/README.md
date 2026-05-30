# Harness 生成 Agent

该 Agent 负责把知识库产物转换为 `libFuzzer` fuzz 驱动。前端不会直接拼 prompt 或解析模型响应，只负责把任务目录中的上下文文件传给 Agent。
Agent 生成初稿后会立即尝试本地编译；如果编译失败，会把编译错误和当前核心文件片段反馈给 LLM，继续生成修复版，直到编译成功或达到最大修复轮数。修复轮允许 LLM 只返回需要修改的文件，Agent 会和上一轮产物合并，避免第二轮请求和响应都过大。编译成功后，Agent 会默认运行生成的 fuzzer 10 秒，确认二进制至少可以启动执行。

模型调用默认使用 OpenAI-compatible Chat Completions 的流式响应。`.env.local` 中可以配置完整 `CHAT_COMPLETIONS_URL`，也可以使用 `API_BASE_URL`/`BASE_URL` 形式，Agent 会自动补 `/chat/completions`。

输入：

- `report.json`
- `<function>_subsource_bundle.c`
- `calls.txt`
- `params.txt`

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
- `harness_spec.json` 中 `run.status == "success"` 表示 10 秒 libFuzzer 试跑通过，也是当前前端认为 `生成 Fuzz Harness` 完整成功的条件。
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

常用模型和编译相关参数：

```text
--timeout 300
--max-retries 2
--no-stream
--clang PATH
--max-repair-rounds 3
--compile-timeout 60
--skip-compile
--run-seconds 10
--skip-run
```
