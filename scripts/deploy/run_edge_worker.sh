#!/usr/bin/env bash
#
# run_edge_worker.sh — launch the ATE Studio edge/execution worker (ate-platform)
#
# Runs the JetStreamWorker on a 工位/edge node pointed at a cloud NATS server.
# Configuration comes from an env file (default config/edge.env, copied from
# config/edge-node.env.example) and/or already-exported environment variables.
#
# Usage:
#   scripts/deploy/run_edge_worker.sh [path/to/edge.env]
#
# Relevant variables (see config/edge-node.env.example for the full list):
#   ATE_PLATFORM_NATS_URL    nats://192.168.5.24:4222   NATS JetStream URL
#   ATE_SIMULATION_MODE      true                         mock instruments, no hardware
#   ATE_PLATFORM_DATA_DIR    /var/lib/ate-platform        local state + SQLite cache
#   ATE_PLATFORM_SNAPSHOT_DIR (optional) crash-recovery snapshots
#
# The worker retries NATS connections forever (it never crashes when NATS is
# unreachable), so it is safe to start before the server is up. Stop with Ctrl-C.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${1:-${REPO_ROOT}/config/edge.env}"

log() { printf '[run-edge-worker] %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1) Load env file (placeholders only in the .example; edge.env is git-ignored)
# ---------------------------------------------------------------------------
if [[ -f "${ENV_FILE}" ]]; then
  log "Loading environment from ${ENV_FILE}"
  # shellcheck disable=SC1090
  set -a; source "${ENV_FILE}"; set +a
else
  log "Env file ${ENV_FILE} not found — using already-exported environment."
  log "Copy config/edge-node.env.example to config/edge.env and fill it in,"
  log "or export ATE_PLATFORM_NATS_URL / ATE_SIMULATION_MODE / ATE_PLATFORM_DATA_DIR."
fi

# ---------------------------------------------------------------------------
# 2) Resolve configuration (defaults match the worker module; warn on .24 miss)
# ---------------------------------------------------------------------------
: "${ATE_PLATFORM_NATS_URL:=nats://localhost:4222}"
: "${ATE_SIMULATION_MODE:=true}"
: "${ATE_PLATFORM_DATA_DIR:=${HOME}/.ate_platform}"

unresolved=0
for var in ATE_PLATFORM_NATS_URL ATE_PLATFORM_DATA_DIR ATE_PLATFORM_STATION_ID ATE_PLATFORM_SNAPSHOT_DIR; do
  val="${!var:-}"
  if [[ "${val}" == *"<"* ]]; then
    log "ERROR: ${var} still contains a placeholder (${val})."
    unresolved=1
  fi
done
if [[ "${unresolved}" -ne 0 ]]; then
  log "Edit ${ENV_FILE} and replace every <...> placeholder before starting."
  exit 2
fi

mkdir -p "${ATE_PLATFORM_DATA_DIR}"

log "NATS URL       : ${ATE_PLATFORM_NATS_URL}"
log "Simulation mode: ${ATE_SIMULATION_MODE} (true = mock instruments, no hardware)"
log "Edge data dir  : ${ATE_PLATFORM_DATA_DIR} (worker_id + SQLite offline cache)"
log "Monitor NATS   : http://$(echo "${ATE_PLATFORM_NATS_URL}" | sed -E 's#nats://([^:/]+).*#\1#'):8222"

# ---------------------------------------------------------------------------
# 3) Run the module entry point (Ctrl-C stops; reconnects are automatic)
# ---------------------------------------------------------------------------
cd "${REPO_ROOT}"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec python -m ate_platform.scheduler.edge_worker
