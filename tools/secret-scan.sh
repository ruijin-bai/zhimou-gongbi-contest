#!/usr/bin/env bash
# 密钥扫描（CI / 本地通用）（阶段 6，2026-09-23）
#
# 优先用 gitleaks（有则跑全历史+工作区）；未安装时退化为内置关键模式扫描，
# 覆盖四大类：环境文件被追踪、PostgreSQL 口令、JWT secret、AI key。
# 无论哪条路径，**发现即非 0 退出**，供 CI 阻断合并。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
FAIL=0

log() { echo "[secret-scan] $*"; }

# ── 1) 敏感文件不得被 git 追踪 ──
log "检查敏感文件是否被追踪 …"
TRACKED=$(git ls-files | grep -E '(^|/)\.env$|(^|/)\.env\.prod$|(^|/)\.env\.production$|\.dump$|\.log$|(^|/)backups/' || true)
if [ -n "$TRACKED" ]; then
  echo "❌ 以下敏感/产物文件被 git 追踪，必须移除：" >&2
  echo "$TRACKED" >&2
  FAIL=1
else
  log "✅ 无敏感文件被追踪（.env / .env.prod / dump / log / backups）"
fi

# ── 2) gitleaks（若已安装）──
if command -v gitleaks >/dev/null 2>&1; then
  log "运行 gitleaks（工作区 + 历史）…"
  if ! gitleaks detect --config .gitleaks.toml --redact --no-banner; then
    echo "❌ gitleaks 发现疑似密钥" >&2
    FAIL=1
  else
    log "✅ gitleaks 通过"
  fi
else
  log "⚠️ 未安装 gitleaks，退化为内置模式扫描（建议 CI 中安装 gitleaks）"
  # 仅扫源码与配置文件（排除 tests/docs/example 等已知安全位置）
  HITS=$(grep -rnE \
    '(ZHIMOU_JWT_SECRET|POSTGRES_PASSWORD|AI_API_KEY)[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9_\-]{16,}' \
    --include='*.py' --include='*.yml' --include='*.yaml' --include='*.sh' --include='*.ts' --include='*.tsx' \
    . 2>/dev/null \
    | grep -vE '\.example|/tests/|/docs/|\.gitleaks\.toml|env\.prod\.example|process\.env|os\.environ|getenv|ci-only-|ci-database-password|\.github/workflows/|\$\{' \
    || true)
  if [ -n "$HITS" ]; then
    echo "❌ 内置扫描发现疑似硬编码密钥：" >&2
    echo "$HITS" >&2
    FAIL=1
  else
    log "✅ 内置扫描通过（未发现硬编码密钥）"
  fi
fi

if [ "$FAIL" -ne 0 ]; then
  echo "[secret-scan] ✗ 未通过" >&2
  exit 1
fi
echo "[secret-scan] ✓ 全部通过"
