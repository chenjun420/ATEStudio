#!/usr/bin/env bash
#
# smoke_live.sh — agent-runnable LIVE smoke test for an ATE Studio cloud deploy.
#
# Verifies a deployed cloud on a bare-metal host (default 192.168.5.24):
#   * TCP reachability of nginx:80, FalkorDB:6379, Qdrant:6333, NATS monitor:8222
#   * HTTP readiness THROUGH nginx:  GET /api/v1/health/ready  (F3 key check —
#     the OLD deploy returns 404 for this path; a 404 is a clear FAIL meaning
#     "old deploy still serving")
#   * FalkorDB graph alive: redis-cli PING + GRAPH.QUERY fmea "RETURN 1"
#     (skipped if redis-cli is not installed locally)
#   * Qdrant: GET /collections returns 200 (expect ate_failures /
#     ate_fault_symptoms)
#   * NATS monitor: GET /varz returns 200
#   * optional POST /api/v1/diagnose reachability — only when SMOKE_AUTH_TOKEN
#     is set (skipped otherwise; no credentials are ever hardcoded here)
#
# This script NEVER contains passwords or keys. Any auth token/password is read
# ONLY from the environment (SMOKE_AUTH_TOKEN / FALKORDB_PASSWORD); checks that
# need auth but have no token are SKIPped with a clear message, never failed.
#
# Exit status:
#   0  all REQUIRED checks passed (readiness via nginx). SKIPs never fail.
#   1  a required check FAILED — including when the target host is unreachable
#      (the script always returns promptly: every curl uses short timeouts, so
#      a CI/agent run reports failure rather than hanging).
#
# Usage:
#   ./scripts/deploy/smoke_live.sh
#   HOST=192.168.5.24 HTTP_PORT=80 ./scripts/deploy/smoke_live.sh
#   SMOKE_AUTH_TOKEN=<jwt> ./scripts/deploy/smoke_live.sh   # also tests /diagnose
#
# Config (env-overridable, all with sensible defaults):
#   HOST             target host            (default 192.168.5.24)
#   HTTP_PORT        nginx HTTP port        (default 80)
#   CLOUD_PORT       direct cloud port      (default 8000; optional direct check)
#   FALKORDB_PORT    FalkorDB Redis port    (default 6379; graph key "fmea")
#   QDRANT_PORT      Qdrant HTTP port       (default 6333)
#   NATS_MON_PORT    NATS monitor port      (default 8222)
#   SMOKE_AUTH_TOKEN JWT for authed probes  (default unset -> those SKIP)
#   FALKORDB_PASSWORD password for redis-cli(default unset -> no AUTH)
#   CHECK_CLOUD_DIRECT=1 also probe :8000 directly (default 0)
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST="${HOST:-192.168.5.24}"
HTTP_PORT="${HTTP_PORT:-80}"
CLOUD_PORT="${CLOUD_PORT:-8000}"
FALKORDB_PORT="${FALKORDB_PORT:-6379}"
QDRANT_PORT="${QDRANT_PORT:-6333}"
NATS_MON_PORT="${NATS_MON_PORT:-8222}"
SMOKE_AUTH_TOKEN="${SMOKE_AUTH_TOKEN:-}"
FALKORDB_PASSWORD="${FALKORDB_PASSWORD:-}"
CHECK_CLOUD_DIRECT="${CHECK_CLOUD_DIRECT:-0}"

FALKORDB_GRAPH="fmea"
QDRANT_COLLECTIONS="ate_failures ate_fault_symptoms"
HEALTH_PATH="/api/v1/health/ready"

CURL_CONNECT_TIMEOUT=5
CURL_MAX_TIME=15

# ---------------------------------------------------------------------------
# Result bookkeeping — each recorded row is "<status>|<check>|<detail>".
# status is PASS / FAIL / SKIP.
# ---------------------------------------------------------------------------
ROWS=()
FAIL_REQUIRED=0

record() {
    # $1=status $2=check $3=detail
    ROWS+=("$1|$2|$3")
    printf '  [%s] %-28s %s\n' "$1" "$2" "$3"
}
pass() { record "PASS" "$1" "$2"; }
fail() { record "FAIL" "$1" "$2"; }
skip() { record "SKIP" "$1" "$2"; }

log()  { printf '[smoke-live] %s\n' "$*"; }
warn() { printf '[smoke-live] WARNING: %s\n' "$*" >&2; }

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------

# tcp_open <port> — is a TCP port accepting connections on $HOST?
# Uses curl: connection failure (rc 7 = refused/host down, 28 = timeout) means
# CLOSED; any other outcome (HTTP reply, empty reply rc 52, reset rc 56) means
# the port is OPEN. Falls back to bash /dev/tcp if curl is absent.
tcp_open() {
    local port="$1"
    if have curl; then
        curl -sS --connect-timeout "${CURL_CONNECT_TIMEOUT}" \
                --max-time "${CURL_MAX_TIME}" -o /dev/null \
                "http://${HOST}:${port}/" >/dev/null 2>&1
        local rc=$?
        # rc 7  = couldn't connect (refused / host unreachable)
        # rc 28 = connect/operation timeout
        [ "${rc}" -ne 7 ] && [ "${rc}" -ne 28 ]
        return $?
    fi
    if have timeout; then
        timeout "${CURL_CONNECT_TIMEOUT}" bash -c "echo > /dev/tcp/${HOST}/${port}" 2>/dev/null
    else
        bash -c "echo > /dev/tcp/${HOST}/${port}" 2>/dev/null
    fi
}

# http_code <url> — print the HTTP status code (or "" on connection failure).
http_code() {
    local url="$1"; shift
    curl -sS --connect-timeout "${CURL_CONNECT_TIMEOUT}" \
         --max-time "${CURL_MAX_TIME}" -o /dev/null \
         -w '%{http_code}' "$@" "${url}" 2>/dev/null || true
}

# http_body <url> [curl args...] — print body on stdout (empty on failure).
http_body() {
    local url="$1"; shift
    curl -sS --connect-timeout "${CURL_CONNECT_TIMEOUT}" \
         --max-time "${CURL_MAX_TIME}" "$@" "${url}" 2>/dev/null || true
}

log "target: http://${HOST}  (nginx :${HTTP_PORT}, cloud :${CLOUD_PORT}, FalkorDB :${FALKORDB_PORT}, Qdrant :${QDRANT_PORT}, NATS mon :${NATS_MON_PORT})"
log "note: short curl timeouts (connect ${CURL_CONNECT_TIMEOUT}s / max ${CURL_MAX_TIME}s) — an unreachable host fails fast."
echo

# ---------------------------------------------------------------------------
# (a) TCP reachability
# ---------------------------------------------------------------------------
log "== TCP reachability =="
check_tcp() {
    local port="$1" name="$2"
    if tcp_open "${port}"; then
        pass "tcp/${name}" "${HOST}:${port} accepts connections"
    else
        fail "tcp/${name}" "${HOST}:${port} unreachable (connection refused/timeout)"
    fi
}
check_tcp "${HTTP_PORT}"      "nginx"
check_tcp "${FALKORDB_PORT}"  "falkordb"
check_tcp "${QDRANT_PORT}"    "qdrant"
check_tcp "${NATS_MON_PORT}"  "nats-mon"
if [ "${CHECK_CLOUD_DIRECT}" = "1" ]; then
    check_tcp "${CLOUD_PORT}" "cloud-direct"
fi
echo

# ---------------------------------------------------------------------------
# (b) Readiness THROUGH nginx — THE key F3 check (required).
# ---------------------------------------------------------------------------
log "== Readiness via nginx (required) =="
ready_url="http://${HOST}:${HTTP_PORT}${HEALTH_PATH}"
code="$(http_code "${ready_url}")"
case "${code}" in
    200)
        body="$(http_body "${ready_url}")"
        # The readiness JSON reports per-component "ok"/"down", e.g.
        # {"database":"ok","nats":"ok","graph":"ok"}. database is the core
        # dependency; require it to be "ok".
        if printf '%s' "${body}" | grep -Eq '"database"[[:space:]]*:[[:space:]]*"ok"'; then
            pass "health/ready (nginx)" "HTTP 200, database ok via nginx (new deploy): ${body}"
        else
            fail "health/ready (nginx)" "HTTP 200 but a component is not ok: ${body:-<empty body>}"
            FAIL_REQUIRED=1
        fi
        ;;
    404)
        # The OLD cloud deploy does not have /api/v1/health/ready -> 404.
        fail "health/ready (nginx)" "HTTP 404 from ${ready_url} — OLD deploy still serving (new deploy returns 200)"
        FAIL_REQUIRED=1
        ;;
    ""|000)
        fail "health/ready (nginx)" "no HTTP response from ${ready_url} — host/nginx unreachable"
        FAIL_REQUIRED=1
        ;;
    *)
        fail "health/ready (nginx)" "unexpected HTTP ${code} from ${ready_url}"
        FAIL_REQUIRED=1
        ;;
esac
echo

# Optional direct-to-cloud readiness (bypasses nginx; not required).
if [ "${CHECK_CLOUD_DIRECT}" = "1" ]; then
    log "== Readiness direct on :${CLOUD_PORT} (optional) =="
    dcode="$(http_code "http://${HOST}:${CLOUD_PORT}${HEALTH_PATH}")"
    if [ "${dcode}" = "200" ]; then
        pass "health/ready (cloud:8000)" "HTTP 200 direct to cloud"
    else
        skip "health/ready (cloud:8000)" "direct check returned '${dcode:-no response}' (CHECK_CLOUD_DIRECT)"
    fi
    echo
fi

# ---------------------------------------------------------------------------
# (c) FalkorDB graph alive — redis-cli PING + GRAPH.QUERY fmea "RETURN 1".
# ---------------------------------------------------------------------------
log "== FalkorDB graph =="
if ! have redis-cli; then
    skip "falkordb/graph" "redis-cli not installed locally — run on the host or install redis-tools to check graph '${FALKORDB_GRAPH}'"
else
    auth_args=()
    if [ -n "${FALKORDB_PASSWORD}" ]; then
        auth_args=(-a "${FALKORDB_PASSWORD}" --no-auth-warning)
    fi
    if ping_out="$(redis-cli -h "${HOST}" -p "${FALKORDB_PORT}" "${auth_args[@]}" PING 2>/dev/null)" \
       && printf '%s' "${ping_out}" | grep -q PONG; then
        pass "falkordb/ping" "PING -> PONG"
    else
        fail "falkordb/ping" "redis-cli PING to ${HOST}:${FALKORDB_PORT} did not return PONG"
    fi
    # GRAPH.QUERY fmea "RETURN 1" — redis-cli prints the integer as (integer) 1.
    if gq="$(redis-cli -h "${HOST}" -p "${FALKORDB_PORT}" "${auth_args[@]}" \
                 GRAPH.QUERY "${FALKORDB_GRAPH}" "RETURN 1" 2>/dev/null)" \
       && printf '%s' "${gq}" | grep -q '(integer) 1'; then
        pass "falkordb/graph" "GRAPH.QUERY ${FALKORDB_GRAPH} \"RETURN 1\" -> (integer) 1"
    else
        fail "falkordb/graph" "GRAPH.QUERY ${FALKORDB_GRAPH} did not return (integer) 1 (graph missing/provisioning?)"
    fi
fi
echo

# ---------------------------------------------------------------------------
# (d) Qdrant — GET /collections expects 200; note expected collections.
# ---------------------------------------------------------------------------
log "== Qdrant vector DB =="
qcode="$(http_code "http://${HOST}:${QDRANT_PORT}/collections")"
if [ "${qcode}" = "200" ]; then
    qbody="$(http_body "http://${HOST}:${QDRANT_PORT}/collections")"
    found=""
    for c in ${QDRANT_COLLECTIONS}; do
        if printf '%s' "${qbody}" | grep -q "\"${c}\""; then
            found="${found} ${c}"
        fi
    done
    if [ -n "${found}" ]; then
        pass "qdrant/collections" "HTTP 200; found collections:${found}"
    else
        pass "qdrant/collections" "HTTP 200 (expected collections [${QDRANT_COLLECTIONS}] not yet listed — may be pre-indexing)"
    fi
else
    fail "qdrant/collections" "GET http://${HOST}:${QDRANT_PORT}/collections returned '${qcode:-no response}'"
fi
echo

# ---------------------------------------------------------------------------
# NATS monitor — GET /varz expects 200.
# ---------------------------------------------------------------------------
log "== NATS monitor =="
ncode="$(http_code "http://${HOST}:${NATS_MON_PORT}/varz")"
if [ "${ncode}" = "200" ]; then
    nbody="$(http_body "http://${HOST}:${NATS_MON_PORT}/varz")"
    srv="$(printf '%s' "${nbody}" | grep -oE '"server_id"[^,]*' | head -1 || true)"
    pass "nats/varz" "HTTP 200 monitor OK ${srv}"
else
    fail "nats/varz" "GET http://${HOST}:${NATS_MON_PORT}/varz returned '${ncode:-no response}'"
fi
echo

# ---------------------------------------------------------------------------
# (e) Optional authenticated diagnose reachability — only with a token.
# ---------------------------------------------------------------------------
log "== Authenticated diagnose probe (optional) =="
if [ -z "${SMOKE_AUTH_TOKEN}" ]; then
    skip "diagnose/post" "SMOKE_AUTH_TOKEN not set — set it to a valid JWT to test POST /api/v1/diagnose"
else
    durl="http://${HOST}:${HTTP_PORT}/api/v1/diagnose"
    dcode="$(curl -sS --connect-timeout "${CURL_CONNECT_TIMEOUT}" --max-time "${CURL_MAX_TIME}" \
                  -o /dev/null -w '%{http_code}' -X POST "${durl}" \
                  -H "Authorization: Bearer ${SMOKE_AUTH_TOKEN}" \
                  -H 'Content-Type: application/json' \
                  -d '{"query":"smoke reachability ping","limit":1}' 2>/dev/null || true)"
    case "${dcode}" in
        200|201|202) pass "diagnose/post" "POST /api/v1/diagnose -> HTTP ${dcode} (reachable + authed)" ;;
        401|403)     fail "diagnose/post" "HTTP ${dcode} — token rejected/forbidden" ;;
        404)         fail "diagnose/post" "HTTP 404 — diagnose route not present (old deploy?)" ;;
        *)           fail "diagnose/post" "unexpected HTTP '${dcode:-no response}' from /api/v1/diagnose" ;;
    esac
fi
echo

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
log "=============================================================="
log "SUMMARY — target ${HOST}"
printf '  %-6s %-28s %s\n' "STATUS" "CHECK" "DETAIL"
printf '  %-6s %-28s %s\n' "------" "----------------------------" "---------------------------------"
n_pass=0; n_fail=0; n_skip=0
for row in "${ROWS[@]}"; do
    st="${row%%|*}"; rest="${row#*|}"; chk="${rest%%|*}"; det="${rest#*|}"
    printf '  %-6s %-28s %s\n' "${st}" "${chk}" "${det}"
    case "${st}" in
        PASS) n_pass=$((n_pass+1)) ;;
        FAIL) n_fail=$((n_fail+1)) ;;
        SKIP) n_skip=$((n_skip+1)) ;;
    esac
done
log "--------------------------------------------------------------"
log "totals: PASS=${n_pass}  FAIL=${n_fail}  SKIP=${n_skip}"

if [ "${FAIL_REQUIRED}" -ne 0 ]; then
    warn "a REQUIRED check failed (readiness via nginx at ${ready_url})."
    warn "if the host is unreachable or this is an OLD deploy (404), the deploy is NOT healthy."
    log "=============================================================="
    exit 1
fi

log "required readiness check passed (SKIPs are non-fatal)."
log "=============================================================="
exit 0
