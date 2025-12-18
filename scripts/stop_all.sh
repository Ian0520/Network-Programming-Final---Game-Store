#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# Stop in reverse order.
stop_process "lobby"
stop_process "developer"
stop_process "db"

echo "[all] stopped"

