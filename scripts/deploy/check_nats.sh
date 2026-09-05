#!/usr/bin/env bash
#
# check_nats.sh — DEPLOY-2: report-only NATS health check for 192.168.5.24.
#
# Reports the nats-server version, systemd unit status, and verifies the
# JetStream streams / KV buckets the cloud app requires:
#   streams: ATE_TASKS, ATE_STATUS, ATE_EXECUTION_EVENTS, ATE_DEAD_LETTERS
#   KV     : ate-workers, ate-handoffs
#
# It NEVER restarts or reconfigures NATS. It prints a recommendation:
# restart/reinstall ONLY when a stream/KV is missing or the unit is down;
# otherwise leave the running 2.10.20 service alone (recon verified healthy).
#
# Run on the host (or any machine with nats-server/systemd access):
#   ./scripts/deploy/check_nats.sh
# Overrides:
#   NATS_MONITOR_URL  default http://127.0.0.1:8222  (monitoring endpoint)
#   NATS_SERVER_URL   default nats://127.0.0.1:4222   (for the nats CLI)
#
set -euo pipefail

NATS_MONITOR_URL="${NATS_MONITOR_URL:-http://127.0.0.1:8222}"
NATS_SERVER_URL="${NATS_SERVER_URL:-nats://127.0.0.1:4222}"

log()  { printf '[check-nats] %s\n' "$*"; }
warn() { printf '[check-nats] WARNING: %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

problems=0

log "=== NATS health report (DEPLOY-2, check-only; no restarts) ==="

# --- Version -----------------------------------------------------------------
log "--- version ---"
if have nats-server; then
    nats-server -v || true
else
    warn "nats-server not on PATH (it lives in /usr/local/bin on .24)"
    [ -x /usr/local/bin/nats-server ] && /usr/local/bin/nats-server -v || true
fi

# --- systemd unit ------------------------------------------------------------
log "--- systemd ---"
if have systemctl; then
    if systemctl is-active --quiet nats 2>/dev/null; then
        log "unit 'nats' is ACTIVE"
        systemctl status nats --no-pager -n 4 2>/dev/null | sed -n '1,5p' || true
    else
        warn "unit 'nats' is NOT active"
        problems=1
    fi
else
    warn "systemctl not available — skipping unit check"
fi

# --- Monitoring endpoint /varz ----------------------------------------------
log "--- monitor (${NATS_MONITOR_URL}/varz) ---"
if have curl; then
    varz="$(curl -fsS --max-time 5 "${NATS_MONITOR_URL}/varz" 2>/dev/null || true)"
    if [ -n "${varz}" ]; then
        server_id="$(printf '%s' "${varz}" | sed -n 's/.*"server_id":"\([^"]*\)".*/\1/p')"
        version="$(printf '%s' "${varz}" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
        jetstream="$(printf '%s' "${varz}" | sed -n 's/.*"jetstream":\(true\|false\).*/\1/p')"
        log "server_id=${server_id:-?} version=${version:-?} jetstream=${jetstream:-?}"
        [ "${jetstream}" = "true" ] || { warn "JetStream NOT enabled in /varz"; problems=1; }
    else
        warn "monitor endpoint ${NATS_MONITOR_URL}/varz unreachable"
        problems=1
    fi
else
    warn "curl not available — skipping /varz check"
fi

# --- JetStream streams + KV via the nats CLI --------------------------------
log "--- JetStream streams & KV (nats CLI) ---"
if have nats; then
    log "server: ${NATS_SERVER_URL}"
    log "streams present:"
    nats --server "${NATS_SERVER_URL}" stream ls 2>/dev/null || warn "nats stream ls failed"
    log "KV buckets present:"
    nats --server "${NATS_SERVER_URL}" kv ls 2>/dev/null || warn "nats kv ls failed"

    for s in ATE_TASKS ATE_STATUS ATE_EXECUTION_EVENTS ATE_DEAD_LETTERS; do
        if nats --server "${NATS_SERVER_URL}" stream info "${s}" >/dev/null 2>&1; then
            log "  PASS stream: ${s}"
        else
            warn "  MISSING stream: ${s}"
            problems=1
        fi
    done
    for k in ate-workers ate-handoffs; do
        if nats --server "${NATS_SERVER_URL}" kv info "${k}" >/dev/null 2>&1; then
            log "  PASS KV: ${k}"
        else
            warn "  MISSING KV bucket: ${k}"
            problems=1
        fi
    done
else
    warn "nats CLI not installed — cannot enumerate streams/KV"
    warn "install it (single static binary): https://github.com/nats-io/natscli/releases"
    warn "then re-run, or check via the monitor ${NATS_MONITOR_URL}/jsz"
fi

# --- Recommendation ----------------------------------------------------------
log "=== recommendation ==="
if [ "${problems}" -eq 0 ]; then
    log "NATS healthy with all expected streams + KV — NO restart needed."
    log "(leave the running service untouched; restart only on config/version change.)"
    exit 0
else
    warn "NATS reported problems above. Actions, in order:"
    warn "  1. missing streams/KV  -> restart ate-cloud (it bootstraps JetStream) or re-run its stream setup"
    warn "  2. unit down / bad ver -> sudo systemctl restart nats, then RE-RUN this script to re-verify streams + KV + edge connectivity"
    exit 1
fi
