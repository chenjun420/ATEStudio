#!/usr/bin/env bash
#
# deploy_cloud.sh — deploy/refresh the ATE Studio CLOUD app on 192.168.5.24
# (Debian 12, bare metal, systemd). Orchestrates the user-mandated
# DEPLOYMENT WAVE CHECKLIST (.omo/notepads/.../problems.md DEPLOY-0..4):
#
#   DEPLOY-0  STOP & DISABLE the old ate-cloud; verify nothing holds :8000.
#   DEPLOY-1  (optional, off by default) purge Neo4j via purge_neo4j.sh.
#   DEPLOY-2  (check only) NATS version/status + JetStream streams/KV;
#             advise restart ONLY when something is missing (never forced).
#   DEPLOY-3  FalkorDB is provisioned separately by provision_falkordb.sh
#             (MODE=baremetal); this script verifies PING before deploying.
#   DEPLOY-4  rsync tree -> ~/ATEStudio (non-git target); uv sync;
#             alembic upgrade head (assert single head d1e2f3a4b5c6);
#             install ate-cloud.service + operator env file; restart;
#             health checks on :8000 and via nginx (:80).
#
# CREDENTIAL-FREE: this script never contains passwords or keys. SSH auth
# must already work via ssh-agent / keys; for password sudo on the host,
# the operator exports SSH_PASSWORD (used only to pipe `sudo -S`, never
# written to disk). Secrets for the app come from a LOCAL env file the
# operator supplies (ENV_FILE=...) — it is copied to the host with mode 600
# and referenced by systemd EnvironmentFile=.
#
# Run from a CONTROL machine (deploys over SSH/rsync):
#   ENV_FILE=~/cloud.env ./scripts/deploy/deploy_cloud.sh
# Or directly ON the host (LOCAL_ONLY=1):
#   sudo ENV_FILE=/home/rpdzkj/cloud.env LOCAL_ONLY=1 ./scripts/deploy/deploy_cloud.sh
#
# Flags / overrides (env vars):
#   REMOTE_HOST      default 192.168.5.24
#   REMOTE_USER      default rpdzkj
#   REMOTE_DIR       default ~/ATEStudio on the host (absolute /home/rpdzkj/ATEStudio)
#   ENV_FILE         LOCAL path to the filled cloud env file (REQUIRED to
#                    (re)install .env.deploy; skipped with a warning if unset)
#   WITH_DEV=1       run `uv sync --extra dev` on the host (default: runtime-only
#                    `uv sync`; dev tools live in the `dev` EXTRA, not a group)
#   RUN_PURGE=1      also run DEPLOY-1 Neo4j purge on the host (default off)
#   SKIP_RSYNC=1     do not sync code (re-run config/migrations only)
#   SKIP_HEALTH=1    skip the final HTTP health checks
#   LOCAL_ONLY=1     execute steps locally instead of over SSH (run ON the host)
#   SSH_OPTS         extra options passed to ssh/rsync (default: BatchMode fallback)
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REMOTE_HOST="${REMOTE_HOST:-192.168.5.24}"
REMOTE_USER="${REMOTE_USER:-rpdzkj}"
REMOTE_DIR="${REMOTE_DIR:-/home/${REMOTE_USER}/ATEStudio}"
ENV_FILE="${ENV_FILE:-}"
WITH_DEV="${WITH_DEV:-0}"
RUN_PURGE="${RUN_PURGE:-0}"
SKIP_RSYNC="${SKIP_RSYNC:-0}"
SKIP_HEALTH="${SKIP_HEALTH:-0}"
LOCAL_ONLY="${LOCAL_ONLY:-0}"
SSH_OPTS="${SSH_OPTS:-}"
UV_BIN="${UV_BIN:-/home/${REMOTE_USER}/.local/bin/uv}"
EXPECTED_HEAD="d1e2f3a4b5c6"
SERVICE_NAME="ate-cloud"
HEALTH_PATH="/api/v1/health/ready"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
UNIT_TEMPLATE="${SCRIPT_DIR}/ate-cloud.service.example"

log()  { printf '[deploy-cloud] %s\n' "$*"; }
warn() { printf '[deploy-cloud] WARNING: %s\n' "$*" >&2; }
die()  { printf '[deploy-cloud] ERROR: %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Remote execution wrapper
# ---------------------------------------------------------------------------
# Remote execution wrappers. Auth: ssh-agent/keys are assumed; when a sudo
# password is needed the operator exports SSH_PASSWORD, which is streamed to
# `sudo -S` over stdin (never echoed, logged, written, or in a remote argv).
ssh_target="${REMOTE_USER}@${REMOTE_HOST}"

# remote_cmd <command-string>  — run a shell command string on the host (or
# locally under LOCAL_ONLY). The command is parsed by exactly ONE login
# shell (bash -lc): pipes/quotes survive because it is a single argument.
remote_cmd() {
    local cmd="$1"
    if [ "${LOCAL_ONLY}" = "1" ]; then
        bash -lc "${cmd}"
    else
        # shellcheck disable=SC2086
        ssh ${SSH_OPTS} "${ssh_target}" bash -lc "$(printf '%q' "${cmd}")"
    fi
}

# remote_stdin — like remote_cmd but reads a script on stdin (used to pipe a
# heredoc to `bash -s`); the heredoc text is parsed by the remote shell.
remote_stdin() {
    if [ "${LOCAL_ONLY}" = "1" ]; then
        bash -ls
    else
        # shellcheck disable=SC2086
        ssh ${SSH_OPTS} "${ssh_target}" bash -ls
    fi
}

# remote_sudo <command string...> — run as root on the host. Arguments are
# joined into one command string and executed under `sudo bash -c` so pipes,
# quotes and `|| true` work identically locally and over SSH.
remote_sudo() {
    local cmd="$*"
    if [ "$(id -u)" = "0" ]; then
        bash -c "${cmd}"
    elif [ "${LOCAL_ONLY}" = "1" ]; then
        if [ -n "${SSH_PASSWORD:-}" ]; then
            printf '%s\n' "${SSH_PASSWORD}" | sudo -S -p '' bash -c "${cmd}"
        else
            sudo bash -c "${cmd}"
        fi
    else
        # printf %q quotes the command for the remote shell; the password is
        # streamed over stdin (never appears in the remote command line).
        local quoted
        quoted="$(printf '%q' "${cmd}")"
        if [ -n "${SSH_PASSWORD:-}" ]; then
            printf '%s\n' "${SSH_PASSWORD}" | \
                # shellcheck disable=SC2086
                ssh ${SSH_OPTS} "${ssh_target}" "sudo -S -p '' bash -c ${quoted}"
        else
            # shellcheck disable=SC2086
            ssh ${SSH_OPTS} "${ssh_target}" "sudo bash -c ${quoted}"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
[ -f "${UNIT_TEMPLATE}" ] || die "missing systemd template: ${UNIT_TEMPLATE}"
if [ "${LOCAL_ONLY}" != "1" ]; then
    have ssh    || die "ssh not found on this control machine"
    have rsync  || die "rsync not found on this control machine (install rsync or run with LOCAL_ONLY=1 on the host)"
fi
if [ -n "${ENV_FILE}" ]; then
    [ -f "${ENV_FILE}" ] || die "ENV_FILE does not exist: ${ENV_FILE}"
    if grep -Eq '(password|key|secret)\s*=\s*<' "${ENV_FILE}" 2>/dev/null; then
        warn "ENV_FILE ${ENV_FILE} still contains <...> placeholders — the app may fail to boot"
    fi
else
    warn "ENV_FILE not set — the .env.deploy will NOT be (re)installed; the existing remote env file is left untouched"
fi

log "target: ${ssh_target}:${REMOTE_DIR}  (LOCAL_ONLY=${LOCAL_ONLY}, WITH_DEV=${WITH_DEV}, RUN_PURGE=${RUN_PURGE})"

# ---------------------------------------------------------------------------
# DEPLOY-3 prerequisite: FalkorDB must answer on 6379 (provision separately).
# ---------------------------------------------------------------------------
log "[prereq] checking FalkorDB on 127.0.0.1:6379 (host-local)..."
if ! remote_cmd 'redis-cli -h 127.0.0.1 -p 6379 PING 2>/dev/null | grep -q PONG'; then
    warn "FalkorDB/Redis is not answering PING on the host"
    warn "provision it FIRST (on the host):  sudo MODE=baremetal ./scripts/deploy/provision_falkordb.sh"
    die "FalkorDB not reachable — run provision_falkordb.sh (DEPLOY-3) before deploying"
fi
if ! remote_cmd 'redis-cli -h 127.0.0.1 -p 6379 MODULE LIST 2>/dev/null | grep -q "\"graph\""'; then
    warn "Redis answers PING but the FalkorDB 'graph' module is NOT loaded"
    die "FalkorDB graph module missing — re-run provision_falkordb.sh and check journalctl -u falkordb"
fi
log "PASS: FalkorDB reachable with graph module loaded"

# ---------------------------------------------------------------------------
# DEPLOY-2 (check only): NATS version + systemd status + JetStream streams/KV.
# Advises restart ONLY when something is missing — never forces one.
# ---------------------------------------------------------------------------
log "[DEPLOY-2] NATS health check (report-only; no restart)..."
remote_stdin <<'NATS_EOF' || warn "NATS check reported issues (see above)"
set -uo pipefail
echo "--- nats-server version ---"
( command -v nats-server >/dev/null && nats-server -v ) || echo "nats-server binary not found in PATH"
echo "--- nats CLI version (if present) ---"
( command -v nats >/dev/null && nats --version ) || echo "nats CLI not installed (stream/KV checks skipped)"
echo "--- systemd unit ---"
systemctl is-active nats 2>/dev/null && systemctl status nats --no-pager -n 3 2>/dev/null | sed -n '1,6p' || echo "unit 'nats' not active/found"
if command -v nats >/dev/null; then
  echo "--- streams (expect ATE_TASKS, ATE_STATUS, ATE_EXECUTION_EVENTS, ATE_DEAD_LETTERS) ---"
  nats stream ls 2>/dev/null || true
  echo "--- KV buckets (expect ate-workers, ate-handoffs) ---"
  nats kv ls 2>/dev/null || true
  missing=0
  for s in ATE_TASKS ATE_STATUS ATE_EXECUTION_EVENTS ATE_DEAD_LETTERS; do
    nats stream info "$s" >/dev/null 2>&1 || { echo "MISSING STREAM: $s"; missing=1; }
  done
  for k in ate-workers ate-handoffs; do
    nats kv info "$k" >/dev/null 2>&1 || { echo "MISSING KV: $k"; missing=1; }
  done
  if [ "$missing" -eq 1 ]; then
    echo ">>> NATS streams/KV MISSING — restart/reconfigure NATS (or re-run the app's JetStream bootstrap) before deploy."
  else
    echo ">>> all expected streams + KV buckets present — no NATS restart needed."
  fi
fi
NATS_EOF

# ---------------------------------------------------------------------------
# DEPLOY-1 (optional): Neo4j purge (authorized wipe; off unless RUN_PURGE=1).
# ---------------------------------------------------------------------------
if [ "${RUN_PURGE}" = "1" ]; then
    log "[DEPLOY-1] purging Neo4j from the host (authorized wipe)..."
    purge_script="${SCRIPT_DIR}/purge_neo4j.sh"
    [ -f "${purge_script}" ] || die "missing ${purge_script}"
    if [ "${LOCAL_ONLY}" = "1" ]; then
        bash "${purge_script}"
    else
        # shellcheck disable=SC2086
        ssh ${SSH_OPTS} "${ssh_target}" 'cat > /tmp/purge_neo4j.sh' < "${purge_script}"
        remote_sudo 'bash /tmp/purge_neo4j.sh'
        remote_cmd "rm -f /tmp/purge_neo4j.sh"
    fi
else
    log "[DEPLOY-1] Neo4j purge skipped (set RUN_PURGE=1 to enable; graph backend is FalkorDB)"
fi

# ---------------------------------------------------------------------------
# DEPLOY-0 (user mandate): FULLY STOP & DISABLE the old ate-cloud; make sure
# nothing is listening on :8000 before we deploy.
# ---------------------------------------------------------------------------
log "[DEPLOY-0] stopping & disabling old ${SERVICE_NAME}..."
remote_sudo systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
remote_sudo systemctl disable "${SERVICE_NAME}" 2>/dev/null || true

log "[DEPLOY-0] verifying port 8000 is free..."
if remote_cmd 'ss -tlnp 2>/dev/null | grep -q ":8000 "'; then
    warn "something still listens on :8000 after stopping the service"
    remote_cmd 'ss -tlnp | grep ":8000 " || true; pgrep -af "uvicorn|ate_cloud.main" || true'
    log "attempting to kill stragglers..."
    # Kill only processes that look like our app (uvicorn serving ate_cloud).
    remote_sudo 'pkill -f "uvicorn ate_cloud.main:app" 2>/dev/null || true; sleep 2'
    if remote_cmd 'ss -tlnp 2>/dev/null | grep -q ":8000 "'; then
        die "port 8000 is STILL held by another process — inspect manually (ss -tlnp | grep :8000)"
    fi
fi
log "PASS: port 8000 is free"

# ---------------------------------------------------------------------------
# DEPLOY-4a: rsync the current tree to ~/ATEStudio (target is non-git).
# Exclusions: VCS, venvs, node deps, local state/plans/caches.
# ---------------------------------------------------------------------------
if [ "${SKIP_RSYNC}" != "1" ]; then
    log "[DEPLOY-4] rsync ${REPO_ROOT}/ -> ${ssh_target}:${REMOTE_DIR}/"
    if [ "${LOCAL_ONLY}" = "1" ]; then
        have rsync || die "rsync required for the sync step"
        mkdir -p "${REMOTE_DIR}"
        rsync -a --delete \
            --exclude '.git/' \
            --exclude '.venv/' \
            --exclude 'node_modules/' \
            --exclude '.omo/' \
            --exclude '__pycache__/' \
            --exclude 'frontend/node_modules/' \
            --exclude '.pytest_cache/' \
            --exclude '.mypy_cache/' \
            --exclude '.ruff_cache/' \
            --exclude '*.pyc' \
            --exclude 'data/' \
            "${REPO_ROOT}/" "${REMOTE_DIR}/"
    else
        rsync_args=(-az --delete)
        [ -n "${SSH_OPTS}" ] && rsync_args+=(-e "ssh ${SSH_OPTS}")
        # shellcheck disable=SC2086
        rsync "${rsync_args[@]}" \
            --exclude '.git/' \
            --exclude '.venv/' \
            --exclude 'node_modules/' \
            --exclude '.omo/' \
            --exclude '__pycache__/' \
            --exclude 'frontend/node_modules/' \
            --exclude '.pytest_cache/' \
            --exclude '.mypy_cache/' \
            --exclude '.ruff_cache/' \
            --exclude '*.pyc' \
            --exclude 'data/' \
            "${REPO_ROOT}/" "${ssh_target}:${REMOTE_DIR}/"
    fi
    log "rsync complete"
else
    log "[DEPLOY-4] SKIP_RSYNC=1 — leaving remote tree untouched"
fi

# uv must exist on the host (recon: 0.12.1 at ~/.local/bin/uv).
remote_cmd "[ -x '${UV_BIN}' ] && '${UV_BIN}' --version" \
    || die "uv not found at ${UV_BIN} on the host — install uv for ${REMOTE_USER} first"

# ---------------------------------------------------------------------------
# DEPLOY-4b: uv sync (runtime-only by default; --extra dev only if WITH_DEV=1).
# Dev tooling (pytest/ruff/mypy) lives in the `dev` OPTIONAL EXTRA, not a
# dependency group — `uv sync` alone is the correct runtime deploy.
# ---------------------------------------------------------------------------
if [ "${WITH_DEV}" = "1" ]; then
    log "[DEPLOY-4] uv sync --extra dev (WITH_DEV=1)"
    remote_cmd "cd '${REMOTE_DIR}' && '${UV_BIN}' sync --extra dev"
else
    log "[DEPLOY-4] uv sync (runtime deps only; set WITH_DEV=1 for the dev extra)"
    remote_cmd "cd '${REMOTE_DIR}' && '${UV_BIN}' sync"
fi

# ---------------------------------------------------------------------------
# DEPLOY-4c: migrations — assert single head d1e2f3a4b5c6, then upgrade.
# PYTHONPATH=src (the app uses the src/ layout).
# ---------------------------------------------------------------------------
log "[DEPLOY-4] checking alembic heads (expect single ${EXPECTED_HEAD})..."
heads_out="$(remote_cmd "cd '${REMOTE_DIR}' && PYTHONPATH=src '${UV_BIN}' run alembic heads 2>&1")"
log "alembic heads: $(printf '%s' "${heads_out}" | tr '\n' ' ')"
head_count="$(printf '%s' "${heads_out}" | grep -cE '^[0-9a-f]{12}(\s|$|\()' || true)"
if ! printf '%s' "${heads_out}" | grep -q "${EXPECTED_HEAD}"; then
    die "expected alembic head ${EXPECTED_HEAD} not found — aborting before upgrade"
fi
if [ "${head_count}" -ne 1 ]; then
    die "expected exactly ONE alembic head, found ${head_count} — resolve migrations before deploy"
fi
log "PASS: single head ${EXPECTED_HEAD}"

log "[DEPLOY-4] running alembic upgrade head..."
remote_cmd "cd '${REMOTE_DIR}' && PYTHONPATH=src '${UV_BIN}' run alembic upgrade head"

# ---------------------------------------------------------------------------
# DEPLOY-4d: install the systemd unit + operator env file.
# ---------------------------------------------------------------------------
log "[DEPLOY-4] installing systemd unit ${SERVICE_NAME}.service..."
unit_remote="/tmp/${SERVICE_NAME}.service.new"
if [ "${LOCAL_ONLY}" = "1" ]; then
    sed "s/__CHANGEME_USER__/${REMOTE_USER}/g" "${UNIT_TEMPLATE}" > "${unit_remote}"
    remote_sudo cp "${unit_remote}" "/etc/systemd/system/${SERVICE_NAME}.service"
    rm -f "${unit_remote}"
else
    # Render the placeholder locally, ship via stdin -> /tmp, then sudo-install.
    sed "s/__CHANGEME_USER__/${REMOTE_USER}/g" "${UNIT_TEMPLATE}" | \
        # shellcheck disable=SC2086
        ssh ${SSH_OPTS} "${ssh_target}" "cat > ${unit_remote}"
    remote_sudo cp "${unit_remote}" "/etc/systemd/system/${SERVICE_NAME}.service"
    remote_cmd "rm -f '${unit_remote}'"
fi

# Env file: copy the operator-supplied LOCAL file into the remote tree with
# mode 600. NEVER embedded in the repo; never echoed.
if [ -n "${ENV_FILE}" ]; then
    log "[DEPLOY-4] installing env file -> ${REMOTE_DIR}/.env.deploy (mode 600)"
    env_remote_tmp="/tmp/.env.deploy.$$"
    if [ "${LOCAL_ONLY}" = "1" ]; then
        install -m 600 "${ENV_FILE}" "${REMOTE_DIR}/.env.deploy"
        remote_sudo chown "${REMOTE_USER}:${REMOTE_USER}" "${REMOTE_DIR}/.env.deploy" 2>/dev/null || true
    else
        # shellcheck disable=SC2086
        ssh ${SSH_OPTS} "${ssh_target}" "cat > ${env_remote_tmp} && chmod 600 ${env_remote_tmp}" < "${ENV_FILE}"
        remote_sudo "install -o '${REMOTE_USER}' -g '${REMOTE_USER}' -m 600 '${env_remote_tmp}' '${REMOTE_DIR}/.env.deploy' && rm -f '${env_remote_tmp}'"
    fi
else
    warn "no ENV_FILE — relying on any pre-existing ${REMOTE_DIR}/.env.deploy"
fi

# ---------------------------------------------------------------------------
# DEPLOY-4e: reload + restart.
# ---------------------------------------------------------------------------
log "[DEPLOY-4] daemon-reload + restart ${SERVICE_NAME}..."
remote_sudo systemctl daemon-reload
remote_sudo systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
remote_sudo systemctl restart "${SERVICE_NAME}"
sleep 3
if ! remote_cmd "systemctl is-active --quiet ${SERVICE_NAME}"; then
    warn "${SERVICE_NAME} is not active — recent logs:"
    remote_cmd "journalctl -u ${SERVICE_NAME} -n 60 --no-pager" || true
    die "${SERVICE_NAME} failed to start"
fi
log "PASS: ${SERVICE_NAME} is active"

# ---------------------------------------------------------------------------
# DEPLOY-4f: health checks — host-local :8000 then via nginx :80.
# ---------------------------------------------------------------------------
if [ "${SKIP_HEALTH}" = "1" ]; then
    log "SKIP_HEALTH=1 — skipping HTTP health checks"
else
    log "[health] waiting for ${HEALTH_PATH} on 127.0.0.1:8000 (host-local)..."
    ok=0
    for i in $(seq 1 20); do
        if remote_cmd "curl -fsS -o /dev/null 'http://127.0.0.1:8000${HEALTH_PATH}'"; then
            ok=1; break
        fi
        sleep 2
    done
    [ "${ok}" = "1" ] || { remote_cmd "journalctl -u ${SERVICE_NAME} -n 80 --no-pager" || true; die "health check failed on 127.0.0.1:8000${HEALTH_PATH}"; }
    log "PASS: ${HEALTH_PATH} via 127.0.0.1:8000"

    log "[health] checking via nginx: http://${REMOTE_HOST}${HEALTH_PATH}"
    if curl -fsS -o /dev/null "http://${REMOTE_HOST}${HEALTH_PATH}"; then
        log "PASS: ${HEALTH_PATH} via nginx (http://${REMOTE_HOST})"
    else
        # The nginx site is NOT managed by this script — it is assumed present
        # on the host. The canonical config (SPA + /api + /docs proxy + SSE
        # buffering-off) is shipped at scripts/deploy/nginx-ate-studio.conf;
        # install/enable it if the site is missing or misbehaving:
        #   sudo cp scripts/deploy/nginx-ate-studio.conf \
        #        /etc/nginx/sites-available/ate-studio
        #   sudo ln -sf /etc/nginx/sites-available/ate-studio \
        #        /etc/nginx/sites-enabled/ate-studio
        #   sudo nginx -t && sudo systemctl reload nginx
        # (Then re-run scripts/deploy/smoke_live.sh to verify end to end.)
        warn "nginx path failed (host-local OK) — the nginx site config is assumed-present on the host."
        warn "install the shipped config: scripts/deploy/nginx-ate-studio.conf (see its header); re-check with scripts/deploy/smoke_live.sh"
        warn "this is non-fatal; the app itself is healthy on :8000"
    fi
fi

log "============================================================"
log "deploy complete: ${SERVICE_NAME} on ${ssh_target}"
log "  app dir : ${REMOTE_DIR}"
log "  unit    : /etc/systemd/system/${SERVICE_NAME}.service"
log "  env file: ${REMOTE_DIR}/.env.deploy (mode 600)"
log "  logs    : ssh ${ssh_target} 'journalctl -u ${SERVICE_NAME} -f'"
log "============================================================"
