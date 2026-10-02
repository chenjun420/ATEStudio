#!/usr/bin/env bash
#
# smoke_live.sh — agent-runnable LIVE smoke test for an ATE Studio cloud deploy.
#
# Verifies a deployed cloud on a bare-metal host (default 192.168.5.25):
#   * TCP reachability of the service and of Qdrant
#   * GET /api/v1/health/ready — HTTP 200 with database AND nats both "ok"
#   * the SPA is actually being served: GET / returns an index.html, and
#     GET /.build-info.json returns a bundle that names the commit and the
#     frontend tree it was built from
#   * Qdrant: GET /collections returns 200 (the dashboard reads fault vectors
#     from it, so this is a real dependency, not an optional extra)
#   * with SMOKE_AUTH_TOKEN set: the authenticated product surface —
#     /api/v1/auth/me, /api/v1/apps with its nested menus, and the plants /
#     stations / fault-cases collections
#
# This script NEVER contains passwords or keys. Any auth token is read ONLY from
# the environment (SMOKE_AUTH_TOKEN); checks that need auth but have no token are
# SKIPped with a clear message, never failed.
#
# Exit status:
#   0  all REQUIRED checks passed. SKIPs never fail.
#   1  a required check FAILED — including when the target host is unreachable
#      (every curl uses short timeouts, so a run reports failure rather than
#      hanging).
#
# Usage:
#   ./scripts/deploy/smoke_live.sh
#   HOST=192.168.5.25 ./scripts/deploy/smoke_live.sh
#   SMOKE_AUTH_TOKEN=<jwt> ./scripts/deploy/smoke_live.sh   # also checks the API surface
#
# Getting a token:
#   token=$(curl -sS -X POST http://192.168.5.25:8000/api/v1/auth/login \
#            -H 'Content-Type: application/json' \
#            -d '{"username":"<user>","password":"<password>"}' | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
#
# Config (env-overridable, all with sensible defaults):
#   HOST             target host               (default 192.168.5.25)
#   HTTP_PORT        the service port         (default 8000)
#   QDRANT_PORT      Qdrant HTTP port         (default 6333)
#   SMOKE_AUTH_TOKEN JWT for authed probes    (default unset -> those SKIP)
#   EXPECT_FRONTHEND_TREE  assert the served bundle was built from this frontend
#                          tree hash (default unset -> reported, not asserted)
#
# There is no reverse proxy in this deployment. An earlier version of this script
# probed nginx on :80 and the NATS monitor on :8222 and reported four FAILs
# against a perfectly healthy board: the SPA is served by FastAPI straight out of
# frontend/dist, and the NATS monitor listens on loopback by design while
# /api/v1/health/ready already reports whether NATS is usable. A smoke test that
# fails on a healthy deployment is worse than no smoke test, because it teaches
# everyone to read past the red.
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST="${HOST:-192.168.5.25}"
HTTP_PORT="${HTTP_PORT:-8000}"
QDRANT_PORT="${QDRANT_PORT:-6333}"
SMOKE_AUTH_TOKEN="${SMOKE_AUTH_TOKEN:-}"
EXPECT_FRONTHEND_TREE="${EXPECT_FRONTHEND_TREE:-}"

QDRANT_COLLECTIONS="ate_failures"
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
fail() { record "FAIL" "$1" "$2"; FAIL_REQUIRED=1; }
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

# http_code <url> [curl args...] — print the HTTP status code (or "" on failure).
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

# count_match <text> <extended-regex> — how many times the pattern occurs.
#
# Not `printf ... | grep -q`. `grep -q` exits on the first match and closes the
# pipe; the writer dies of SIGPIPE and, under `set -o pipefail`, the pipeline
# reports 141 — so a question that was answered "yes" is reported as a failure.
#
# And not `grep -c`, which counts matching LINES. Every response here is a single
# line of JSON, so `grep -c` reports 1 for eleven occurrences; counting
# occurrences is what makes "3 groups, 20 pages" come out as numbers a person
# can check against the UI.
count_match() {
    local text="$1" pattern="$2"
    printf '%s' "${text}" | grep -oE "${pattern}" 2>/dev/null | wc -l | tr -d ' ' 2>/dev/null || true
}

log "target: http://${HOST}:${HTTP_PORT}  (service :${HTTP_PORT}, Qdrant :${QDRANT_PORT})"
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
check_tcp "${HTTP_PORT}"   "service"
check_tcp "${QDRANT_PORT}" "qdrant"
echo

# ---------------------------------------------------------------------------
# (b) Readiness (required).
#
# NATS is asserted here rather than by probing its monitor port: the monitor
# listens on 127.0.0.1 and is not reachable from another host by design, while
# this endpoint reports whether NATS is actually usable by the application.
# ---------------------------------------------------------------------------
log "== Readiness (required) =="
ready_url="http://${HOST}:${HTTP_PORT}${HEALTH_PATH}"
code="$(http_code "${ready_url}")"
case "${code}" in
    200)
        body="$(http_body "${ready_url}")"
        db_ok=0; nats_ok=0
        [ "$(count_match "${body}" '"database"[[:space:]]*:[[:space:]]*"ok"')" -ge 1 ] && db_ok=1
        [ "$(count_match "${body}" '"nats"[[:space:]]*:[[:space:]]*"ok"')" -ge 1 ] && nats_ok=1
        if [ "${db_ok}" -eq 1 ] && [ "${nats_ok}" -eq 1 ]; then
            pass "health/ready" "HTTP 200, database ok, nats ok: ${body}"
        else
            fail "health/ready" "HTTP 200 but database=${db_ok} nats=${nats_ok}: ${body:-<empty body>}"
        fi
        ;;
    ""|000)
        fail "health/ready" "no HTTP response from ${ready_url} — host unreachable or the service is down"
        ;;
    *)
        fail "health/ready" "unexpected HTTP ${code} from ${ready_url}"
        ;;
esac
echo

# ---------------------------------------------------------------------------
# (c) The SPA.
#
# This is the part that says the deploy actually installed a bundle, rather than
# leaving whatever was there before. A backend-only deploy that never touched
# frontend/dist would still pass (b) and (c)-readiness; only these two catch it.
# ---------------------------------------------------------------------------
log "== SPA =="
spa_url="http://${HOST}:${HTTP_PORT}/"
code="$(http_code "${spa_url}")"
if [ "${code}" = "200" ]; then
    index="$(http_body "${spa_url}")"
    n_idx="$(printf '%s' "${index}" | wc -c | tr -d ' ')"
    if [ "${n_idx}" -gt 1000 ] && [ "$(count_match "${index}" '<div id="app"')" -ge 1 ]; then
        pass "spa/index" "HTTP 200, ${n_idx} bytes, mounts #app"
    else
        fail "spa/index" "HTTP 200 but the body is not an application shell (${n_idx} bytes, #app mount $( [ "$(count_match "${index}" '<div id="app"')" -ge 1 ] && echo present || echo missing))"
    fi
else
    fail "spa/index" "GET ${spa_url} returned '${code:-no response}'"
fi

info_url="http://${HOST}:${HTTP_PORT}/.build-info.json"
code="$(http_code "${info_url}")"
if [ "${code}" != "200" ]; then
    fail "spa/build-info" "GET ${info_url} returned '${code:-no response}' — the installed bundle cannot be traced to a commit"
else
    info="$(http_body "${info_url}")"
    src="$(printf '%s' "${info}" | sed -n 's/.*"source_sha"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    tree="$(printf '%s' "${info}" | sed -n 's/.*"frontend_tree"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    built="$(printf '%s' "${info}" | sed -n 's/.*"built_by"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    if [ -z "${src}" ] || [ -z "${tree}" ]; then
        fail "spa/build-info" "the served .build-info.json names no source_sha/frontend_tree: ${info:-<empty>}"
    elif [ -n "${EXPECT_FRONTHEND_TREE}" ] && [ "${tree}" != "${EXPECT_FRONTHEND_TREE}" ]; then
        fail "spa/build-info" "bundle was built from frontend tree ${tree}, expected ${EXPECT_FRONTHEND_TREE}"
    else
        pass "spa/build-info" "bundle from ${src:0:12} (${built:-unknown}) frontend tree ${tree:0:12}"
    fi
fi
echo

# ---------------------------------------------------------------------------
# (d) Qdrant — required, because api/v1/dashboard.py reads fault vectors from it.
# ---------------------------------------------------------------------------
log "== Qdrant vector DB =="
qcode="$(http_code "http://${HOST}:${QDRANT_PORT}/collections")"
if [ "${qcode}" = "200" ]; then
    qbody="$(http_body "http://${HOST}:${QDRANT_PORT}/collections")"
    found=""
    for c in ${QDRANT_COLLECTIONS}; do
        # Counting, not `grep -q` — see count_match.
        [ "$(count_match "${qbody}" "${c}")" -ge 1 ] && found="${found} ${c}"
    done
    if [ -n "${found}" ]; then
        pass "qdrant/collections" "HTTP 200; found collections:${found}"
    else
        pass "qdrant/collections" "HTTP 200 (expected collections [${QDRANT_COLLECTIONS}] not listed — may be pre-indexing)"
    fi
else
    fail "qdrant/collections" "GET http://${HOST}:${QDRANT_PORT}/collections returned '${qcode:-no response}' — the dashboard's fault queries will fail"
fi
echo

# ---------------------------------------------------------------------------
# (e) Authenticated product surface — only with a token.
# ---------------------------------------------------------------------------
log "== Authenticated API surface (optional) =="
if [ -z "${SMOKE_AUTH_TOKEN}" ]; then
    skip "api/surface" "SMOKE_AUTH_TOKEN not set — set it to a JWT to check /auth/me, /apps, /plants, /stations, /fault-cases"
else
    auth=(-H "Authorization: Bearer ${SMOKE_AUTH_TOKEN}")

    me_code="$(http_code "http://${HOST}:${HTTP_PORT}/api/v1/auth/me" "${auth[@]}")"
    case "${me_code}" in
        200)
            me="$(http_body "http://${HOST}:${HTTP_PORT}/api/v1/auth/me" "${auth[@]}")"
            user="$(printf '%s' "${me}" | sed -n 's/.*"username"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
            role="$(printf '%s' "${me}" | sed -n 's/.*"role"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
            pass "api/auth-me" "HTTP 200 as ${user:-?}/${role:-?}"
            ;;
        401|403) fail "api/auth-me" "HTTP ${me_code} — the token was rejected" ;;
        *)       fail "api/auth-me" "unexpected HTTP '${me_code:-no response}'" ;;
    esac

    # The app LIST carries no menus. GET /api/v1/apps returns
    # {"items":[{id,code,name,...}],"total":N} — the two-level IA only appears on
    # GET /api/v1/apps/{id}, where each group holds a nested `children` array.
    # Asserting on the list response finds zero groups and zero pages, which
    # reads as "the seed did not run" on a perfectly healthy deployment.
    #
    # ids are pulled with grep because this script stays dependency-free. Every
    # object in `items` has exactly one "id", and nothing else in the response
    # does, so the matches are the app ids. If that ever stops being true the
    # count check below fails loudly rather than silently passing.
    apps_code="$(http_code "http://${HOST}:${HTTP_PORT}/api/v1/apps" "${auth[@]}")"
    if [ "${apps_code}" = "200" ]; then
        apps_body="$(http_body "http://${HOST}:${HTTP_PORT}/api/v1/apps" "${auth[@]}")"
        app_ids="$(printf '%s' "${apps_body}" \
                  | grep -oE '"id"[[:space:]]*:[[:space:]]*"[0-9a-f-]+"' \
                  | grep -oE '[0-9a-f-]{8,}' || true)"
        n_apps="$(printf '%s\n' "${app_ids}" | grep -c . || true)"
        if [ "${n_apps}" -lt 1 ]; then
            fail "api/apps" "HTTP 200 but no app id could be read from ${apps_body:-<empty>}"
        else
            n_groups=0; n_pages=0; bad=""
            while read -r aid; do
                [ -n "${aid}" ] || continue
                one="$(http_body "http://${HOST}:${HTTP_PORT}/api/v1/apps/${aid}" "${auth[@]}")"
                g="$(count_match "${one}" '"parent_id"[[:space:]]*:[[:space:]]*null')"
                p="$(count_match "${one}" '"parent_id"[[:space:]]*:[[:space:]]*"')"
                n_groups=$((n_groups + g)); n_pages=$((n_pages + p))
                [ "${g}" -ge 1 ] || bad="${bad} ${aid}"
            done <<< "${app_ids}"
            if [ "${n_pages}" -lt 1 ]; then
                fail "api/apps" "HTTP 200; ${n_apps} apps, ${n_groups} groups, 0 pages under them — the two-level seed did not run${bad:+ (apps with no group at all:${bad})}"
            else
                pass "api/apps" "HTTP 200; ${n_apps} apps, ${n_groups} groups, ${n_pages} pages nested under them"
            fi
        fi
    else
        fail "api/apps" "unexpected HTTP '${apps_code:-no response}' from /api/v1/apps"
    fi

    for p in plants stations fault-cases; do
        c="$(http_code "http://${HOST}:${HTTP_PORT}/api/v1/${p}" "${auth[@]}")"
        if [ "${c}" = "200" ]; then
            b="$(http_body "http://${HOST}:${HTTP_PORT}/api/v1/${p}" "${auth[@]}")"
            n="$(printf '%s' "${b}" | sed -n 's/.*"total"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')"
            pass "api/${p}" "HTTP 200, total=${n:-?}"
        else
            fail "api/${p}" "unexpected HTTP '${c:-no response}' from /api/v1/${p}"
        fi
    done
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
    warn "a REQUIRED check failed."
    log "=============================================================="
    exit 1
fi

log "all required checks passed (SKIPs are non-fatal)."
log "=============================================================="
exit 0