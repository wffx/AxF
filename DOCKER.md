# Docker 使用说明

本文档说明 AxF 的 Docker 开发环境、Web UI 运行方式、LLM 网络要求，以及 Codex CLI 的边界。

## 能力范围

Docker 镜像包含：

- Python 3.12
- clang-14 / lld-14 / llvm-cov / llvm-profdata / libFuzzer runtime
- build-essential / make / git
- ripgrep
- Codex CLI
- Python 标准库 sqlite3 支持

容器用于运行：

- AxF Web UI
- Terminal CLI
- 知识抽取
- Harness 生成 Agent
- 本地测试
- clang 编译和 libFuzzer smoke test
- 短跑后的覆盖率计算

## 文件

```text
docker/
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
└── README.md
DOCKER.md
```

镜像不会内置 `.env.local`、`workspace/`、`state/runs/`、`linux-7.0/`、外部
`kRepo/` 或生成产物。Compose 会把 AxF 仓库挂载到：

```text
/workspace
```

默认还会把宿主机上的 `../../linux-7.0` 挂载到：

```text
/linux-7.0
```

如果宿主机存在外部 kRepo，Compose 还会只读挂载：

```text
/kRepo
```

容器内会设置 `KREPO_ROOT=/kRepo`。AxF 的 kRepo adapter 只有在该路径下存在可用
`src/cpp_meta_query.py` 时才使用 external provider；否则回退到内置
`knowledge_base` provider。通过 Docker 环境变量自动发现的 external provider 如果
对某个目标执行失败，也会回退到内置 provider；显式选择 external provider 时不会回退。

容器启动时会读取 `/linux-7.0/.vscode/BROWSE.VC.DB` 的 `files` 表。如果索引里记录的是宿主机绝对路径，例如：

```text
/Users/yufei/Documents/llm fuzzing/linux-7.0
```

entrypoint 会在容器内自动创建这个路径到 `/linux-7.0` 的符号链接。这样 kRepo 不需要改代码，也能读取索引里记录的源码路径。

如果你的 Linux 源码不在这个位置，启动前设置：

```bash
export LINUX_ROOT=/absolute/path/to/linux-7.0
```

## 网络和 LLM

Compose 默认不注入显式代理变量：

```text
HTTP_PROXY
HTTPS_PROXY
ALL_PROXY
NO_PROXY
```

如果 Web 页面里点“生成 Fuzz Harness”并使用远端模型，容器必须能访问外网 HTTPS。推荐在 Clash Verge 中开启 TUN，让 Docker Desktop 的 Linux VM 流量被系统级接管。

如果 TUN 没有接管 Docker 容器流量，也可以显式让 AxF 容器走宿主机代理。Clash Verge 常见端口是 `7897`：

```bash
AXF_DOCKER_PROXY=http://host.docker.internal:7897 \
  docker --context desktop-linux compose -f docker/docker-compose.yml up -d axf-web
```

进入 dev 容器时同样可以带上：

```bash
AXF_DOCKER_PROXY=http://host.docker.internal:7897 \
  docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev
```

离线功能不需要外网，包括：

- 启动 Web UI。
- 跑测试。
- 读取 `BROWSE.VC.DB`。
- 抽取 report / calls / params / subsource。
- 使用已有知识产物。
- clang 编译和 smoke test。

需要外网的功能包括：

- 第一次构建镜像或无缓存重建。
- apt 安装系统依赖。
- 可选安装 Codex CLI 或其他 npm / pip 依赖。
- 调用 Chat Completions API。
- 使用需要联网的 `nga` / `opencode` / `claude` / Codex CLI。

验证容器 HTTPS 出站：

```bash
AXF_DOCKER_PROXY=http://host.docker.internal:7897 \
  docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev \
  python -c "import urllib.request; print(urllib.request.urlopen('https://pypi.tuna.tsinghua.edu.cn/simple/setuptools/', timeout=15).status)"
```

能输出 `200`，说明容器出站 HTTPS 可用。若出现 `SSL: UNEXPECTED_EOF_WHILE_READING`，通常是 Clash TUN 没有接管 Docker 容器流量。

## 配置模型

在 AxF 仓库根目录准备 `.env.local`：

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

`.env.local` 已被 `.gitignore` 忽略，不会打进镜像，也不会提交到 Git。

## Codex CLI

AxF Docker 镜像默认安装 Codex CLI，但不挂载宿主机 `~/.codex`。

原因：

- 本机 Codex App 的可执行文件是 macOS Mach-O，不能直接复制到 Linux 容器运行。
- `~/.codex` 里包含 `auth.json`、本地状态、日志和插件缓存，直接挂进 Web 服务容器会扩大凭据暴露面。
- AxF Web UI 当前调用的是 OpenAI-compatible Chat Completions API，不是 Codex App 的会话。

如果只是要让 Web 页面点生成，推荐继续使用 `.env.local` 中的 `CHAT_COMPLETIONS_URL` 和 `API_KEY`。

构建镜像后可以验证：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev codex --version
```

进入容器后自行登录：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev codex login
```

这条路径需要容器 HTTPS 出站正常。

如果需要临时禁用 Codex CLI 安装，可以覆盖 build arg：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml build \
  --build-arg INSTALL_CODEX_CLI=0 dev
```

## 构建镜像

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml build
```

如果当前 Docker context 已经能连接 Docker Desktop，也可以省略 `--context desktop-linux`。

## 启动 Web UI

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml up -d axf-web
```

打开：

```text
http://127.0.0.1:8787
```

如果本机 `8787` 已被占用，可以换宿主机端口：

```bash
AXF_WEB_PORT=8788 docker --context desktop-linux compose -f docker/docker-compose.yml up -d axf-web
```

此时打开：

```text
http://127.0.0.1:8788
```

查看日志：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml logs --no-color --tail=120 axf-web
```

停止：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml down
```

## 进入开发容器

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev
```

容器内检查：

```bash
python --version
clang-14 --version
rg --version
codex --version
```

运行测试：

```bash
python -m unittest discover -s tests -v
```

运行默认 workflow：

```bash
python -m runner.workflow_runner fuzzing-pipeline.yaml \
  --repo /linux-7.0 \
  --function can_send \
  --file net/can/af_can.c
```

## 业务日志

Web 页面任务日志和 `workspace/*/tasks/<task-id>/task.log` 会在任务开始时写入运行环境摘要，包括：

- 是否运行在 Docker 容器中。
- 项目目录和任务目录。
- Linux 源码挂载路径是否存在。
- Python / rg / clang 可见性。
- LLM 模式、模型、Chat Completions URL 和 API key 环境变量是否已设置。

日志只记录 API key 环境变量名和“已设置/未设置”状态，不记录密钥值。

## Terminal CLI 示例

离线抽取知识产物：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev \
  python -m frontend.terminal run \
    --non-interactive \
    --repo /linux-7.0 \
    --function can_send \
    --file net/can/af_can.c \
    --artifacts report_json,subsource,params
```

调用模型生成 harness：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev \
  python -m frontend.terminal run \
    --non-interactive \
    --repo /linux-7.0 \
    --function can_send \
    --file net/can/af_can.c \
    --artifacts report_json,subsource,params,harness_generation_agent \
    --llm-mode api \
    --model glm-5.1 \
    --model-url https://provider.example/v1/chat/completions \
    --api-key sk-your-key \
    --clang /usr/bin/clang-14
```

Compose 已给 `dev` 和 `axf-web` 设置 `CLANG=/usr/bin/clang-14`。命令里不写
`--clang` 时，Terminal 和 Web 任务也会默认使用容器内的 `/usr/bin/clang-14`。

## 当前本地验证状态

截至本次本地测试：

- AxF Web UI 可在容器中启动。
- 容器不注入显式 proxy 变量。
- `.env.local` 可由 AxF 服务加载。
- kRepo/知识抽取路径可读取本地 Linux 源码。
- `python -m unittest discover -s tests -v` 在容器内通过：`119 tests, OK, 9 skipped`。
- `http://127.0.0.1:8788/api/defaults` smoke test 通过。
- 容器 HTTPS 出站测试通过，`https://pypi.org/simple/` 和 `https://example.com/` 均返回 `200`。
- `https://api.xixixixi.cloud/v1/chat/completions` 的 POST 请求可以到达服务端；若返回 `401 无效的令牌`，说明网络已通，需要更新 `.env.local` 中的 `API_KEY`。
