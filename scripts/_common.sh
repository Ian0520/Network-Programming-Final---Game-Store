#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${ROOT_DIR}/.run"
LOG_DIR="${RUN_DIR}/logs"

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

pidfile() {
  local name="$1"
  echo "${RUN_DIR}/${name}.pid"
}

is_running() {
  local pid="$1"
  kill -0 "${pid}" >/dev/null 2>&1
}

start_process() {
  local name="$1"
  shift
  local pf
  pf="$(pidfile "${name}")"
  local lf="${LOG_DIR}/${name}.log"

  if [[ -f "${pf}" ]]; then
    local pid
    pid="$(cat "${pf}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && is_running "${pid}"; then
      echo "[${name}] already running (pid=${pid})"
      return 0
    fi
    rm -f "${pf}"
  fi

  echo "[${name}] starting..."
  nohup "$@" >"${lf}" 2>&1 &
  echo "$!" >"${pf}"
  echo "[${name}] pid=$! log=${lf}"
}

stop_process() {
  local name="$1"
  local pf
  pf="$(pidfile "${name}")"
  if [[ ! -f "${pf}" ]]; then
    echo "[${name}] not running (no pidfile)"
    return 0
  fi
  local pid
  pid="$(cat "${pf}" 2>/dev/null || true)"
  if [[ -z "${pid}" ]]; then
    rm -f "${pf}"
    echo "[${name}] stale pidfile removed"
    return 0
  fi
  if ! is_running "${pid}"; then
    rm -f "${pf}"
    echo "[${name}] not running (stale pidfile removed)"
    return 0
  fi

  echo "[${name}] stopping pid=${pid}..."
  kill "${pid}" >/dev/null 2>&1 || true
  for _ in {1..20}; do
    if ! is_running "${pid}"; then
      rm -f "${pf}"
      echo "[${name}] stopped"
      return 0
    fi
    sleep 0.1
  done
  echo "[${name}] force killing pid=${pid}..."
  kill -9 "${pid}" >/dev/null 2>&1 || true
  rm -f "${pf}"
  echo "[${name}] stopped (killed)"
}

