#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

PYTHON="${PYTHON:-python3}"
# Allow running from within hw3/ while keeping imports as `hw3.*`.
export PYTHONPATH="${PYTHONPATH:-${ROOT_DIR}/..}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

start_process "db" "${PYTHON}" -u -m hw3.server.db_server
sleep 0.2
start_process "developer" "${PYTHON}" -u -m hw3.server.developer_server
sleep 0.2
start_process "lobby" "${PYTHON}" -u -m hw3.server.lobby_server

echo "[all] started"
