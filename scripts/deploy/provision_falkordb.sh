#!/usr/bin/env bash
#
# provision_falkordb.sh — idempotent FalkorDB provisioning for the
# ATE Studio debug server (192.168.5.24, Debian 12 bookworm arm64).
#
# FalkorDB speaks the Redis protocol (RESP) on port 6379 — it replaces the
# retired Neo4j (bolt 7687 is NOT used). The container image also serves the
# FalkorDB Browser UI on port 3000.
#
# Two install modes (MODE env var, default "auto"):
#
#   MODE=container  (MODE A) podman/docker container "falkordb" with named
#                   volume "falkordb-data" (ports 6379 + 3000). Requires a
#                   container runtime. Kept for hosts that have one.
#
#   MODE=baremetal  (MODE B) NO container runtime. Installs Redis 8 from the
#                   official redis.io apt repository (Debian/Ubuntu ship only
#                   Redis 7.x, which is too old for FalkorDB 4.x), downloads
#                   the official prebuilt falkordb.so module for the arch,
#                   and runs redis-server with the module loaded under the
#                   systemd unit "falkordb" (nohup fallback on non-systemd).
#                   This is the chosen mode for 192.168.5.24 (no podman/docker;
#                   arm64/aarch64).
#
#   MODE=auto       (default) use the container path when podman/docker exists,
#                   otherwise bare metal.
#
# ARM64 MODULE AVAILABILITY (verified 2026-09-06 against FalkorDB v4.20.4
# GitHub release assets):
#   falkordb-arm64v8.so  — glibc linux/arm64, 43,833,584 bytes,
#                          sha256 3af674201bdfce73004effab2f2ede632bd00f04539adde0c4923df33cfb53fa
#   falkordb-x64.so      — glibc linux/amd64
#   (falkordb-alpine-*.so are musl/Alpine builds — NOT for Debian.)
# If the prebuilt module for the arch cannot be downloaded, the script prints
# an explicit STOP message and exits non-zero — it NEVER installs a compiler
# toolchain or guesses a build.
#
# Idempotent: safe to re-run. If FalkorDB (graph module loaded) already
# answers on 6379 the script prints "already running" and exits 0. Fixed
# names: container/volume "falkordb"/"falkordb-data" (container path) or
# /var/lib/falkordb + unit "falkordb" (bare metal), so re-runs never create
# duplicates.
#
# Usage (run ON the target host, e.g. 192.168.5.24 — requires SSH/console
# access; see docs/部署手册-192.168.5.24调试服务器.md):
#   chmod +x scripts/deploy/provision_falkordb.sh
#   sudo MODE=baremetal ./scripts/deploy/provision_falkordb.sh   # .24 (option B)
#   sudo ./scripts/deploy/provision_falkordb.sh                  # auto
#
# Environment overrides:
#   MODE                 auto|container|baremetal   (default auto)
#   FALKORDB_IMAGE       container image            (default falkordb/falkordb:latest)
#   FALKORDB_VERSION     module release tag         (default v4.20.4, bare-metal path)
#   FALKORDB_MODULE_SHA256  optional sha256 of the downloaded .so (verified when set)
#   FALKORDB_PORT        RESP port                  (default 6379)
#   FALKORDB_BIND        redis bind address         (default 127.0.0.1; set
#                        0.0.0.0 only if LAN clients must reach 6379 directly)
#   FALKORDB_UI_PORT     browser UI port            (default 3000, container path)
#   FALKORDB_CONTAINER   container name             (default falkordb)
#   FALKORDB_VOLUME      container volume name      (default falkordb-data)
#   FALKORDB_DATA_DIR    bare-metal data directory  (default /var/lib/falkordb)
#   FALKORDB_PASSWORD    optional Redis password    (default: none — debug LAN)
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODE="${MODE:-auto}"
FALKORDB_IMAGE="${FALKORDB_IMAGE:-falkordb/falkordb:latest}"
FALKORDB_VERSION="${FALKORDB_VERSION:-v4.20.4}"
FALKORDB_MODULE_SHA256="${FALKORDB_MODULE_SHA256:-}"
FALKORDB_PORT="${FALKORDB_PORT:-6379}"
FALKORDB_BIND="${FALKORDB_BIND:-127.0.0.1}"
FALKORDB_UI_PORT="${FALKORDB_UI_PORT:-3000}"
FALKORDB_CONTAINER="${FALKORDB_CONTAINER:-falkordb}"
FALKORDB_VOLUME="${FALKORDB_VOLUME:-falkordb-data}"
FALKORDB_DATA_DIR="${FALKORDB_DATA_DIR:-/var/lib/falkordb}"
FALKORDB_PASSWORD="${FALKORDB_PASSWORD:-}"
GRAPH_NAME="fmea"

case "${MODE}" in
    auto|container|baremetal) ;;
    *) printf '[provision-falkordb] ERROR: invalid MODE=%s (want auto|container|baremetal)\n' "${MODE}" >&2; exit 1 ;;
esac

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
log()  { printf '[provision-falkordb] %s\n' "$*"; }
warn() { printf '[provision-falkordb] WARNING: %s\n' "$*" >&2; }
die()  { printf '[provision-falkordb] ERROR: %s\n' "$*" >&2; exit 1; }

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        warn "not running as root — container runtimes may fail or data dirs may not be writable"
        warn "re-run with sudo if provisioning fails: sudo $0"
    fi
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "this step requires root — re-run with sudo: sudo MODE=${MODE} $0"
}

have() { command -v "$1" >/dev/null 2>&1; }

# redis-cli argument vector for the password (empty when no password set).
redis_cli() {
    if [ -n "${FALKORDB_PASSWORD}" ]; then
        redis-cli -h 127.0.0.1 -p "${FALKORDB_PORT}" -a "${FALKORDB_PASSWORD}" --no-auth-warning "$@"
    else
        redis-cli -h 127.0.0.1 -p "${FALKORDB_PORT}" "$@"
    fi
}

# Wait until the local RESP port answers (or timeout). $1 = timeout seconds.
wait_for_port() {
    local timeout="${1:-30}" waited=0
    log "waiting for FalkorDB on 127.0.0.1:${FALKORDB_PORT} (up to ${timeout}s)..."
    while [ "${waited}" -lt "${timeout}" ]; do
        if redis_cli PING 2>/dev/null | grep -q PONG; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

# True when the thing answering on 6379 has the FalkorDB graph module loaded
# (a vanilla redis-server — e.g. the distro package that auto-started —
# answers PING but does NOT have the module; that must not be mistaken for a
# completed provision).
graph_module_loaded() {
    redis_cli MODULE LIST 2>/dev/null | grep -q '"graph"'
}

# ---------------------------------------------------------------------------
# Verification — Redis PING + openCypher RETURN 1. Works for every install
# path because FalkorDB speaks RESP regardless of how it was launched.
# ---------------------------------------------------------------------------
verify() {
    log "verifying FalkorDB..."

    if ! wait_for_port 30; then
        warn "FalkorDB did not answer PING within 30s"
        warn "container path: <runtime> logs ${FALKORDB_CONTAINER}"
        warn "bare-metal path: systemctl status falkordb; journalctl -u falkordb -n 100"
        return 1
    fi
    log "PASS: Redis PING -> PONG (127.0.0.1:${FALKORDB_PORT})"

    if ! graph_module_loaded; then
        warn "redis on port ${FALKORDB_PORT} answers PING but the FalkorDB 'graph' module is NOT loaded"
        warn "bare-metal: journalctl -u falkordb -n 100 (look for loadmodule errors)"
        return 1
    fi
    log "PASS: FalkorDB 'graph' module loaded (MODULE LIST)"

    # openCypher smoke test: create/query a throwaway graph. GRAPH.QUERY
    # auto-creates the graph on first write; RETURN 1 needs no data.
    local cypher_out
    if cypher_out="$(redis_cli GRAPH.QUERY "${GRAPH_NAME}" "RETURN 1" 2>&1)"; then
        # redis-cli prints the result set as e.g. "2) 1) 1) (integer) 1".
        if printf '%s' "${cypher_out}" | grep -q "(integer) 1"; then
            log "PASS: openCypher GRAPH.QUERY ${GRAPH_NAME} \"RETURN 1\" -> 1"
        else
            log "PASS: openCypher GRAPH.QUERY ${GRAPH_NAME} \"RETURN 1\" executed:"
            printf '%s\n' "${cypher_out}" | sed 's/^/         /'
        fi
    else
        warn "GRAPH.QUERY failed:"
        printf '%s\n' "${cypher_out}" | sed 's/^/         /' >&2
        return 1
    fi

    log "------------------------------------------------------------"
    log "FalkorDB is ready:"
    log "  RESP (Redis protocol): redis://${FALKORDB_BIND}:${FALKORDB_PORT} (graph '${GRAPH_NAME}')"
    if [ "${MODE}" = "container" ]; then
        log "  Browser UI           : http://192.168.5.24:${FALKORDB_UI_PORT}"
    fi
    log "  Python client        : falkordb.FalkorDB.from_url(\"redis://127.0.0.1:${FALKORDB_PORT}\")"
    log "------------------------------------------------------------"
}

# Detect the container runtime early (podman preferred — design doc §10.2
# verified Podman Compose on .24 — then docker). Used by both the
# already-running fast path and the install path.
RUNTIME=""
if have podman; then
    RUNTIME="podman"
elif have docker; then
    RUNTIME="docker"
fi

# PING through a running container when the host has no redis-cli.
ping_via_runtime() {
    [ -n "${RUNTIME}" ] || return 1
    "${RUNTIME}" ps --format '{{.Names}}' 2>/dev/null | grep -qx "${FALKORDB_CONTAINER}" || return 1
    if [ -n "${FALKORDB_PASSWORD}" ]; then
        "${RUNTIME}" exec "${FALKORDB_CONTAINER}" redis-cli -a "${FALKORDB_PASSWORD}" --no-auth-warning PING 2>/dev/null | grep -q PONG
    else
        "${RUNTIME}" exec "${FALKORDB_CONTAINER}" redis-cli PING 2>/dev/null | grep -q PONG
    fi
}

# ---------------------------------------------------------------------------
# Already-provisioned fast path: FalkorDB (graph module loaded) answering on
# the port => no-op success. A vanilla redis-server without the module does
# NOT count — provisioning continues so the module gets installed/loaded.
# ---------------------------------------------------------------------------
if { have redis-cli && redis_cli PING 2>/dev/null | grep -q PONG; } || ping_via_runtime; then
    if { have redis-cli && graph_module_loaded; } || ping_via_runtime; then
        log "FalkorDB already running and answering PING on port ${FALKORDB_PORT} — nothing to do."
        if [ -n "${RUNTIME}" ] && "${RUNTIME}" ps --format '{{.Names}}' 2>/dev/null | grep -qx "${FALKORDB_CONTAINER}"; then
            log "  (${RUNTIME} container '${FALKORDB_CONTAINER}' is up; UI on ${FALKORDB_UI_PORT})"
        elif have systemctl && systemctl is-active --quiet falkordb 2>/dev/null; then
            log "  (systemd service 'falkordb' is active — bare-metal install)"
        fi
        exit 0
    fi
    warn "port ${FALKORDB_PORT} answers PING but the FalkorDB graph module is NOT loaded"
    warn "(a plain redis-server is running?) — continuing provisioning to load falkordb.so"
fi

need_root

# ---------------------------------------------------------------------------
# MODE A — container path (podman preferred, then docker)
# ---------------------------------------------------------------------------
if [ "${MODE}" != "baremetal" ] && [ -n "${RUNTIME}" ]; then
    MODE="container"
    log "using container runtime: ${RUNTIME}"

    # Pull first so a re-run after an image update picks up the new image;
    # failure to pull (offline LAN with a cached image) is non-fatal.
    if ! "${RUNTIME}" pull "${FALKORDB_IMAGE}"; then
        warn "image pull failed (offline?); continuing with any locally cached image"
    fi

    # Idempotent container name: a stopped/exited container from a previous
    # run is removed (volume is kept — data persists), then re-created.
    if "${RUNTIME}" ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${FALKORDB_CONTAINER}"; then
        log "removing existing container '${FALKORDB_CONTAINER}' (data volume '${FALKORDB_VOLUME}' is preserved)..."
        "${RUNTIME}" rm -f "${FALKORDB_CONTAINER}" >/dev/null
    fi

    # Named volume is created implicitly by the runtime if missing; re-runs
    # reuse it, so graph data survives container recreation.
    run_args=(run -d --name "${FALKORDB_CONTAINER}"
              --restart unless-stopped
              -p "${FALKORDB_PORT}:6379"
              -p "${FALKORDB_UI_PORT}:3000"
              -v "${FALKORDB_VOLUME}:/data")
    if [ -n "${FALKORDB_PASSWORD}" ]; then
        # Persistence (AOF) + auth are passed straight through to redis-server.
        run_args+=(-e "REDIS_ARGS=--requirepass ${FALKORDB_PASSWORD} --appendonly yes --appendfsync everysec")
    else
        run_args+=(-e "REDIS_ARGS=--appendonly yes --appendfsync everysec")
    fi
    run_args+=("${FALKORDB_IMAGE}")

    log "starting ${FALKORDB_CONTAINER}: ${RUNTIME} ${run_args[*]}"
    "${RUNTIME}" "${run_args[@]}"

    # redis-cli is available inside the image even if the host lacks it.
    if ! have redis-cli; then
        redis_cli() {
            "${RUNTIME}" exec "${FALKORDB_CONTAINER}" redis-cli "$@"
        }
        if [ -n "${FALKORDB_PASSWORD}" ]; then
            redis_cli() {
                "${RUNTIME}" exec "${FALKORDB_CONTAINER}" redis-cli -a "${FALKORDB_PASSWORD}" --no-auth-warning "$@"
            }
        fi
    fi

    if verify; then exit 0; fi
    die "container started but verification failed — inspect: ${RUNTIME} logs ${FALKORDB_CONTAINER}"
fi

# MODE=container was requested explicitly but no runtime exists — fail loud
# instead of silently dropping to bare metal.
if [ "${MODE}" = "container" ]; then
    cat >&2 <<EOF

[provision-falkordb] MODE=container requested but neither podman nor docker
was found. Install a container runtime, or re-run with MODE=baremetal:

    Debian/Ubuntu : sudo apt-get update && sudo apt-get install -y podman
    Docker        : https://docs.docker.com/engine/install/

EOF
    die "no container runtime available"
fi

# ---------------------------------------------------------------------------
# MODE B — bare metal: no podman, no docker (chosen option on 192.168.5.24).
# FalkorDB = Redis 8.0+ server + the falkordb.so module loaded at startup.
#   1. Ensure redis-server >= 8.0 (install from the redis.io apt repo on
#      Debian/Ubuntu — the distro repo only ships Redis 7.0).
#   2. Download the official prebuilt falkordb.so for the arch into
#      /var/lib/falkordb; STOP explicitly if no prebuilt artifact exists.
#   3. Run redis-server with the module loaded under systemd unit "falkordb"
#      (nohup fallback where systemd is absent).
# ---------------------------------------------------------------------------
MODE="baremetal"
log "using bare-metal mode (Redis 8 + falkordb.so, no container runtime)"
require_root

# --- 1) Ensure Redis 8 -------------------------------------------------------
redis_major() {
    redis-server --version 2>/dev/null | sed -n 's/.*v=\([0-9]*\)\.\([0-9]*\)\.[0-9]*.*/\1/p' | head -1
}

install_redis8_debian() {
    require_root
    # Arch via dpkg (authoritative on Debian/Ubuntu); only arm64/amd64 have
    # prebuilt falkordb.so artifacts.
    local arch
    if have dpkg; then
        arch="$(dpkg --print-architecture)"
    else
        arch="$(uname -m)"
    fi
    case "${arch}" in
        arm64|aarch64)  arch="arm64" ;;
        amd64|x86_64)   arch="amd64" ;;
        *) die "unsupported architecture '${arch}' for prebuilt FalkorDB modules (use the container path)" ;;
    esac
    log "architecture: ${arch}"

    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # Prerequisites for adding the redis.io repo over HTTPS.
    apt-get install -y -qq ca-certificates curl gnupg lsb-release >/dev/null

    local keyring=/usr/share/keyrings/redis-archive-keyring.gpg
    local sources=/etc/apt/sources.list.d/redis.list
    if [ ! -f "${keyring}" ]; then
        log "adding redis.io apt repository signing key..."
        curl -fsSL https://packages.redis.io/gpg \
            | gpg --dearmor -o "${keyring}.tmp"
        chmod 644 "${keyring}.tmp"
        mv "${keyring}.tmp" "${keyring}"
    fi
    # Codename: prefer lsb_release, fall back to /etc/os-release, then bookworm
    # (the target host runs Debian 12 bookworm).
    local codename
    codename="$(lsb_release -cs 2>/dev/null || true)"
    if [ -z "${codename}" ] && [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        codename="$(. /etc/os-release; echo "${VERSION_CODENAME:-}")"
    fi
    codename="${codename:-bookworm}"
    local repo_line="deb [signed-by=${keyring}] https://packages.redis.io/deb ${codename} main"
    if [ ! -f "${sources}" ] || ! grep -Fq "${repo_line}" "${sources}" 2>/dev/null; then
        log "adding redis.io apt repository for ${codename} (Redis 8; distro ships only 7.0)..."
        echo "${repo_line}" > "${sources}"
    fi

    apt-get update -qq
    # redis.io packages carry epoch 6: (vs Debian's 5:), so apt picks Redis 8.
    log "installing Redis 8 (redis-server redis-tools) + libgomp1..."
    apt-get install -y -qq redis-server redis-tools libgomp1 >/dev/null
}

if have redis-server; then
    cur_major="$(redis_major || true)"
    if [ -n "${cur_major}" ] && [ "${cur_major}" -ge 8 ]; then
        log "redis-server $(redis-server --version | sed -n 's/.*v=\([0-9.]*\).*/\1/p' | head -1) already meets the >= 8.0 requirement"
    else
        warn "redis-server present but too old (major '${cur_major:-unknown}'); FalkorDB 4.x requires Redis 8.0+"
        if have apt-get; then
            install_redis8_debian
        else
            die "no apt-get on this host — install Redis 8 manually (https://redis.io/docs/latest/operate/oss_and_stack/install/install-redis/) and re-run"
        fi
    fi
else
    log "redis-server not found"
    if have apt-get; then
        install_redis8_debian
    else
        cat >&2 <<EOF

[provision-falkordb] redis-server is not installed and this host has no
apt-get. Install Redis 8.0+ manually (FalkorDB 4.x does NOT support Redis 7.x):

    RHEL/CentOS/Rocky : https://packages.redis.io/rpm  (dnf install redis)
    Debian/Ubuntu     : this script can do it automatically on apt-based hosts

EOF
        die "redis-server >= 8.0 required"
    fi
fi

# Final version gate after any install.
redis_ver="$(redis-server --version 2>/dev/null | sed -n 's/.*v=\([0-9]*\)\.\([0-9]*\)\.[0-9]*.*/\1.\2/p' | head -1)"
redis_major_now="${redis_ver%%.*}"
if [ -z "${redis_major_now}" ] || [ "${redis_major_now}" -lt 8 ]; then
    die "redis-server ${redis_ver:-unknown} installed but FalkorDB requires Redis 8.0+ (check apt policy redis-server)"
fi
log "redis-server ${redis_ver} meets the >= 8.0 requirement"

# The distro/redis.io package ships its own redis-server.service which may
# have auto-started a plain redis on 6379 (no module). Our falkordb.service
# owns the port — stop and disable the stock unit (idempotent; never fail the
# run if it is already gone).
if have systemctl && [ -d /run/systemd/system ]; then
    if systemctl is-active --quiet redis-server 2>/dev/null; then
        log "stopping stock redis-server.service (falkordb.service will own port ${FALKORDB_PORT})..."
        systemctl stop redis-server 2>/dev/null || true
    fi
    systemctl disable redis-server 2>/dev/null || true
fi

# OpenMP runtime is required by the module (libgomp.so.1).
if ! ldconfig -p 2>/dev/null | grep -q libgomp; then
    warn "libgomp (OpenMP) not detected — the module may fail to load."
    warn "install it: apt-get install -y libgomp1  |  dnf install -y libgomp"
fi

# --- 2) Obtain the prebuilt falkordb.so for the arch ------------------------
arch="$(uname -m)"
case "${arch}" in
    x86_64|amd64)  module_asset="falkordb-x64.so" ;;
    aarch64|arm64) module_asset="falkordb-arm64v8.so" ;;
    *) die "unsupported architecture for prebuilt module: ${arch} (use the container path instead)" ;;
esac
module_url="https://github.com/FalkorDB/FalkorDB/releases/download/${FALKORDB_VERSION}/${module_asset}"

mkdir -p "${FALKORDB_DATA_DIR}"
module_path="${FALKORDB_DATA_DIR}/falkordb.so"
conf_path="${FALKORDB_DATA_DIR}/falkordb.conf"
pidfile="${FALKORDB_DATA_DIR}/falkordb.pid"
logfile="${FALKORDB_DATA_DIR}/falkordb.log"

if [ -s "${module_path}" ]; then
    log "module already present: ${module_path} (re-running reuses it; delete to re-download)"
else
    log "downloading FalkorDB module ${FALKORDB_VERSION}/${module_asset}..."
    log "  URL: ${module_url}"
    dl_ok=0
    if have curl; then
        curl -fSL --retry 3 -o "${module_path}.tmp" "${module_url}" && dl_ok=1 || dl_ok=0
    elif have wget; then
        wget -q -O "${module_path}.tmp" "${module_url}" && dl_ok=1 || dl_ok=0
    else
        die "need curl or wget to download the module (or install podman/docker)"
    fi
    if [ "${dl_ok}" -ne 1 ]; then
        rm -f "${module_path}.tmp"
        cat >&2 <<EOF

[provision-falkordb] =================================================================
[provision-falkordb]  no prebuilt ${arch} falkordb.so — manual compilation required;
[provision-falkordb]  STOPPING
[provision-falkordb] =================================================================
[provision-falkordb] Checked (HTTP error / asset missing):
[provision-falkordb]   ${module_url}
[provision-falkordb] FalkorDB release assets: https://github.com/FalkorDB/FalkorDB/releases/tag/${FALKORDB_VERSION}
[provision-falkordb] This script will NOT install a compiler/toolchain or guess a build.
[provision-falkordb] Options:
[provision-falkordb]   1. Build falkordb.so from source for ${arch} out-of-band, then place it
[provision-falkordb]      at ${module_path} and re-run this script.
[provision-falkordb]   2. Switch this host to the container path (install podman/docker).

EOF
        die "no prebuilt ${arch} falkordb.so — manual compilation required; STOPPING"
    fi

    # Verify the download is a real ELF shared object (guards against an HTML
    # error page saved by a proxy) and, when FALKORDB_MODULE_SHA256 is set,
    # verify its checksum.
    if ! head -c 4 "${module_path}.tmp" | od -An -tx1 | grep -q '7f 45 4c 46'; then
        rm -f "${module_path}.tmp"
        die "downloaded module is not an ELF binary (expected falkordb.so) — aborting"
    fi
    if [ -n "${FALKORDB_MODULE_SHA256}" ]; then
        if have sha256sum; then
            got="$(sha256sum "${module_path}.tmp" | awk '{print $1}')"
            if [ "${got}" != "${FALKORDB_MODULE_SHA256}" ]; then
                rm -f "${module_path}.tmp"
                die "module sha256 mismatch: got ${got}, expected ${FALKORDB_MODULE_SHA256}"
            fi
            log "PASS: module sha256 matches FALKORDB_MODULE_SHA256"
        else
            warn "sha256sum not available — skipping FALKORDB_MODULE_SHA256 verification"
        fi
    fi
    mv "${module_path}.tmp" "${module_path}"
    chmod 0755 "${module_path}"
    log "module installed: ${module_path}"
fi

# --- 3) redis.conf + service -------------------------------------------------
# Default bind is loopback (the ate-cloud app is co-located on .24). Set
# FALKORDB_BIND=0.0.0.0 only if LAN clients must reach 6379 directly; in that
# case protected-mode is turned off and a password is strongly recommended.
auth_line=""
protected_mode="yes"
if [ -n "${FALKORDB_PASSWORD}" ]; then
    auth_line="requirepass ${FALKORDB_PASSWORD}"
fi
case "${FALKORDB_BIND}" in
    0.0.0.0|'*')
        if [ -z "${FALKORDB_PASSWORD}" ]; then
            warn "FALKORDB_BIND=${FALKORDB_BIND} with no password — protected-mode disabled; set FALKORDB_PASSWORD"
        fi
        protected_mode="no"
        ;;
esac
cat > "${conf_path}" <<EOF
# Generated by provision_falkordb.sh — FalkorDB (Redis 8 + falkordb.so module)
bind ${FALKORDB_BIND}
protected-mode ${protected_mode}
port ${FALKORDB_PORT}
dir ${FALKORDB_DATA_DIR}
pidfile ${pidfile}
logfile ${logfile}
daemonize no
supervised no
appendonly yes
appendfsync everysec
save 900 1
save 300 10
loadmodule ${module_path}
${auth_line}
EOF
log "wrote ${conf_path} (bind ${FALKORDB_BIND}, port ${FALKORDB_PORT})"

start_bare() {
    log "starting redis-server with FalkorDB module..."
    if have systemctl && [ -d /run/systemd/system ]; then
        require_root
        unit="/etc/systemd/system/falkordb.service"
        cat > "${unit}" <<EOF
[Unit]
Description=FalkorDB (Redis 8 + falkordb.so graph module)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$(command -v redis-server) ${conf_path}
Restart=always
RestartSec=3
User=root
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable falkordb >/dev/null 2>&1 || true
        systemctl restart falkordb
        log "systemd unit 'falkordb' installed and started (journalctl -u falkordb for logs)"
    else
        # No systemd (containers/minimal hosts): run supervised by nohup.
        if [ -f "${pidfile}" ] && kill -0 "$(cat "${pidfile}")" 2>/dev/null; then
            log "falkordb process already running (pid $(cat "${pidfile}"))"
        else
            nohup redis-server "${conf_path}" >> "${logfile}" 2>&1 &
            log "started via nohup (pid $!); logs: ${logfile}"
        fi
    fi
}

# If a systemd unit already exists from a previous run, just (re)start it —
# regenerating the unit is harmless and keeps config changes idempotent.
start_bare

if verify; then exit 0; fi
die "bare-metal FalkorDB failed verification — check ${logfile} (or: journalctl -u falkordb -n 100)"
