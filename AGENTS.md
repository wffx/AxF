# AGENTS.md

## 项目概览

AxF 是一个面向 C 代码的 LLM fuzz harness 生成与验证工具。项目包含后端
agent、Web 前端、Docker 开发环境和测试套件，用于生成 libFuzzer harness、
编译运行、收集结果，并在前端展示任务状态、artifact、失败原因和覆盖率信息。

## 优先工作流

默认优先使用 Docker 运行开发、测试和 Web UI。只有在用户明确要求，或 Docker
不可用时，才使用宿主机环境。

常用命令：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml build dev
docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev bash
docker --context desktop-linux compose -f docker/docker-compose.yml up -d axf-web
```

运行测试：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev \
  env -u CLANG python -m unittest
```

如果模型服务需要宿主机代理，优先通过 `AXF_DOCKER_PROXY` 注入：

```bash
AXF_DOCKER_PROXY=http://host.docker.internal:7897 \
  docker --context desktop-linux compose -f docker/docker-compose.yml up -d axf-web
```

## 仓库结构

- `agents/harness_generation/agent.py`：harness 生成、编译、运行、覆盖率统计和模型请求逻辑。
- `frontend/server.py`：Web 后端、任务管理、artifact 暴露和事件流。
- `frontend/static/`：前端页面、任务状态和覆盖率展示。
- `docker/`：AxF Docker 镜像和 compose 配置。
- `tests/`：unittest 测试。
- `docs/`：设计说明和架构文档。

## 代码约束

- 变更应聚焦在用户请求的行为上，不做无关重构。
- 不要提交 `.env.local`、任务输出、coverage artifact、缓存、虚拟环境或本地日志。
- 修改生成、编译、运行、覆盖率或前端展示逻辑时，应同步更新测试。
- 对模型请求失败、网络失败、编译失败和 fuzzer timeout 要保留可诊断日志。
- 优先复用现有 dataclass、任务状态和 artifact 结构，不随意新增平行格式。

## Fuzzing 注意事项

- 默认 smoke run 不应启动长时间 fuzzing，除非用户明确要求。
- `-fsanitize=fuzzer,address,undefined` 在部分 Docker/平台组合下可能运行异常或超时；
  如运行阶段卡住，可以降级到 `-fsanitize=fuzzer` 做轻量验证和覆盖率统计。
- 覆盖率相关输出应放在任务 artifact 目录下，并通过前端 artifact 列表展示。

## Agent 协作建议

- 修改前先查看 `git status --short --branch`，避免覆盖用户未提交改动。
- 阅读相关代码后再编辑，优先遵循现有模块边界和命名风格。
- 对较大的实现，可以让多个 subagent 分别做代码阅读、实现方案、测试补充和 review；
  最终仍由主 agent 统一集成、运行测试并给出结果。
