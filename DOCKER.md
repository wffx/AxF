# Docker 使用说明

本文档说明 AxF 的 Docker 开发环境、Web UI 运行方式、LLM 网络要求，以及 Codex CLI 的边界。

## 能力范围

Docker 镜像包含：

- Python 3.12
- clang-14 / lld-14 / libFuzzer runtime
- build-essential / make / git
- ripgrep
- Python 标准库 sqlite3 支持

容器用于运行：

- AxF Web UI
- Terminal CLI
- 知识抽取
- Harness 生成 Agent
- 本地测试
- clang 编译和 libFuzzer smoke test

## 文件

```text
docker/
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
└── README.md
DOCKER.md
```

镜像不会内置 `.env.local`、`workspace/`、`linux-7.0/` 或生成产物。Compose 会把 AxF 仓库挂载到：

```text
/workspace
```

默认还会把宿主机上的 `../../linux-7.0` 挂载到：

```text
/linux-7.0
```

如果你的 Linux 源码不在这个位置，启动前设置：

```bash
export LINUX_ROOT=/absolute/path/to/linux-7.0
```

## 网络和 LLM

Compose 不注入显式代理变量：

```text
HTTP_PROXY
HTTPS_PROXY
ALL_PROXY
NO_PROXY
```

如果 Web 页面里点“生成 Fuzz Harness”并使用远端模型，容器必须能访问外网 HTTPS。推荐在 Clash Verge 中开启 TUN，让 Docker Desktop 的 Linux VM 流量被系统级接管。

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

Codex CLI 可以装进 Linux 容器，但默认不安装，也不挂载宿主机 `~/.codex`。

原因：

- 本机 Codex App 的可执行文件是 macOS Mach-O，不能直接复制到 Linux 容器运行。
- `~/.codex` 里包含 `auth.json`、本地状态、日志和插件缓存，直接挂进 Web 服务容器会扩大凭据暴露面。
- AxF Web UI 当前调用的是 OpenAI-compatible Chat Completions API，不是 Codex App 的会话。

如果只是要让 Web 页面点生成，推荐继续使用 `.env.local` 中的 `CHAT_COMPLETIONS_URL` 和 `API_KEY`。

如果确实要在开发容器里手动使用 Codex CLI，可以用可选 build arg 构建：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml build \
  --build-arg INSTALL_CODEX_CLI=1 dev
```

之后进入容器自行登录：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev codex login
```

这条路径需要容器 HTTPS 出站正常。

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
```

运行测试：

```bash
python -m unittest discover -s tests
```

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
    --model glm-5.1 \
    --clang /usr/bin/clang-14
```

## 当前本地验证状态

截至本次本地测试：

- AxF Web UI 可在容器中启动。
- 容器不注入显式 proxy 变量。
- `.env.local` 可由 AxF 服务加载。
- kRepo/知识抽取路径可读取本地 Linux 源码。
- `python -m unittest discover -s tests` 在容器内通过。
- `http://127.0.0.1:8788/api/defaults` smoke test 通过。
- 容器 HTTPS 出站测试仍出现 `SSL: UNEXPECTED_EOF_WHILE_READING`，说明当前 Clash TUN 还没有接管 Docker Desktop VM 的 HTTPS 流量；远端 LLM API 调用仍需要继续修 Clash TUN。
