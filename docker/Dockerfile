FROM python:3.12-slim-bookworm

ARG INSTALL_CODEX_CLI=0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DEFAULT_TIMEOUT=60

RUN sed -i 's|http://deb.debian.org/debian|http://mirrors.tuna.tsinghua.edu.cn/debian|g; s|http://deb.debian.org/debian-security|http://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::Retries "5";\nAcquire::http::Timeout "60";\nAcquire::https::Timeout "60";\n' > /etc/apt/apt.conf.d/80-retries

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        clang-14 \
        curl \
        git \
        libclang-rt-14-dev \
        lld-14 \
        make \
        ripgrep \
        xz-utils \
    && if [ "$INSTALL_CODEX_CLI" = "1" ]; then \
        apt-get install -y --no-install-recommends nodejs npm \
        && npm install -g @openai/codex; \
    fi \
    && rm -rf /var/lib/apt/lists/*

ENV AXF_RUNTIME=docker

WORKDIR /workspace

COPY docker/entrypoint.sh /usr/local/bin/axf-entrypoint
RUN chmod +x /usr/local/bin/axf-entrypoint

EXPOSE 8787

ENTRYPOINT ["axf-entrypoint"]
CMD ["bash"]
