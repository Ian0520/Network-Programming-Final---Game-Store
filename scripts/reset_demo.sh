#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

FORCE="${1:-}"
if [[ "${FORCE}" != "--yes" ]]; then
  cat <<'EOF'
Reset demo state (DESTRUCTIVE).

This will remove:
  - SQLite DB file (NP_HW3_DB_PATH or hw3/server/storage/hw3.sqlite3)
  - server/uploaded_games/
  - server/storage/tmp_uploads/
  - player/downloads/
  - player/downloads/_review_drafts/

Usage:
  ./hw3/scripts/reset_demo.sh --yes
EOF
  exit 2
fi

# Stop servers first (best-effort).
"${ROOT_DIR}/scripts/stop_all.sh" || true

DB_PATH="${NP_HW3_DB_PATH:-}"
if [[ -z "${DB_PATH}" ]]; then
  CONFIG_PATH="${NP_HW3_CONFIG:-${ROOT_DIR}/config.json}"
  if [[ -f "${CONFIG_PATH}" ]]; then
    DB_PATH="$(
      ROOT_DIR="${ROOT_DIR}" CONFIG_PATH="${CONFIG_PATH}" python3 - <<'PY' || true
import json
import os
from pathlib import Path

root = Path(os.environ["ROOT_DIR"])
cfg_path = Path(os.environ["CONFIG_PATH"])
try:
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

db = cfg.get("db") if isinstance(cfg, dict) else None
sqlite_path = (db or {}).get("sqlitePath") if isinstance(db, dict) else None
if not isinstance(sqlite_path, str) or not sqlite_path.strip():
    raise SystemExit(0)

p = Path(sqlite_path.strip())
if not p.is_absolute():
    p = (root / p).resolve()
print(str(p))
PY
    )"
  fi
fi
DB_PATH="${DB_PATH:-${ROOT_DIR}/server/storage/hw3.sqlite3}"

echo "[reset] removing db: ${DB_PATH}"
rm -f "${DB_PATH}"

echo "[reset] removing uploaded_games/"
rm -rf "${ROOT_DIR}/server/uploaded_games"

echo "[reset] removing tmp_uploads/"
rm -rf "${ROOT_DIR}/server/storage/tmp_uploads"

echo "[reset] removing player downloads/"
rm -rf "${ROOT_DIR}/player/downloads"

echo "[reset] removing run state (.run/)"
rm -rf "${ROOT_DIR}/.run"

echo "[reset] done"
