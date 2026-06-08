# Docker

完整说明见仓库根目录：

```text
../DOCKER.md
```

常用命令：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml build
docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev
docker --context desktop-linux compose -f docker/docker-compose.yml up -d axf-web
```

AxF Docker 镜像默认包含 Codex CLI。验证：

```bash
docker --context desktop-linux compose -f docker/docker-compose.yml run --rm dev codex --version
```
