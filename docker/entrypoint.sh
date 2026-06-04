#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="/workspace:/workspace/knowledge_base:${PYTHONPATH:-}"

exec "$@"
