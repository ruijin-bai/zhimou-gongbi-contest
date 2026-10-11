#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -r requirements.txt; }
[ -f .env ] || { cp .env.example .env; echo "已生成 .env，请填写交融 API Key 后重新运行"; exit 0; }
[ -f data/zhimou.db ] || { read -rp "项目名称：" P; .venv/bin/python -m app init --project "$P"; }
exec .venv/bin/python -m app serve
