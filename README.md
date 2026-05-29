# Knowledge-based multi-agent Fuzz harness generation platform

中文名称：基于知识库的多智能体 Fuzz Harness 生成平台

本项目是一个基于知识库的 AI 辅助 `libFuzzer` harness 生成平台，目标是围绕 C/C++ 库构建“知识库 -> harness 生成 -> 种子生成 -> 执行 -> Crash/覆盖率分析 -> 反馈”的迭代闭环。

说明：中文语境中的“Fuzz 驱动”在英文中统一表述为 `Fuzz harness`；面向 `libFuzzer` 时，即 `libFuzzer harness`。

当前阶段保持 high-level、可演进的工程骨架，优先跑通知识库组件和后续 Agent 工作流的最小闭环。

## 核心组件

- `knowledge_base/`：知识库组件代码，当前包含 C/C++ 元数据抽取能力。
- `agents/harness_generation/`：Fuzz harness 生成 Agent。
- `agents/harness_execution/`：Fuzz harness 执行 Agent。
- `agents/crash_analysis/`：Crash 分析 Agent。
- `scheduler/`：调度组件。
- `tools/`：构建、libFuzzer、覆盖率、Crash 等工具封装。
- `workspace/`：本地运行产物目录。

## 文档

- [架构文档](docs/architecture.md)
- [知识库组件说明](docs/knowledge_base/README.md)
- [API 文档索引](docs/api/README.md)
- [cpp_meta_query API](docs/api/knowledge_base/cpp_meta_query.md)

## 测试

知识库组件烟测：

```powershell
$env:KREPO_TEST_REPO='F:\AI\codexProject\kRepo\linux-7.0'
python -m unittest tests.knowledge_base.test_cpp_meta_query -v
```

如果未设置 `KREPO_TEST_REPO` 且当前目录不存在 `linux-7.0`，相关测试会自动跳过。
