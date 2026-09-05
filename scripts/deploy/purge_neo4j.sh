#!/usr/bin/env bash
#
# purge_neo4j.sh — DEPLOY-1: fully remove the RETIRED Neo4j from a host.
#
# The graph backend migrated to FalkorDB (Redis RESP / 6379); Neo4j data
# migration is OUT of scope and the wipe is AUTHORIZED by the user. This
# script:
#   1. DETECTS Neo4j (dpkg, binaries, data dirs, containers, ports 7474/7687)
#      and prints a before/after report.
#   2. Stops/disables any neo4j systemd unit, purges neo4j*/cypher-shell
#      packages, removes leftover dirs (/var/lib/neo4j, /var/log/neo4j,
#      /etc/neo4j, /usr/share/neo4j), and removes any neo4j containers/images.
#   3. VERIFIES ports 7474 and 7687 are free.
#
# Idempotent: safe to re-run; "not installed" is a success. Requires root
# (run via sudo). It NEVER touches FalkorDB/Redis (6379) or any app data.
#
# Usage (on the target host):
#   sudo ./scripts/deploy/purge_neo4j.sh            # detect + purge
#   sudo PURGE_DRY_RUN=1 ./scripts/deploy/purge_neo4j.sh   # report only, no changes
#
set -euo pipefail

PURGE_DRY_RUN="${PURGE_DRY_RUN:-0}"

log()  { printf '[purge-neo4j] %s\n' "$*"; }
warn() { printf '[purge-neo4j] WARNING: %s\n' "$*" >&2; }
die()  { printf '[purge-neo4j] ERROR: %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (sudo)"

run() {
    if [ "${PURGE_DRY_RUN}" = "1" ]; then
        log "DRY-RUN would run: $*"
    else
        "$@"
    fi
}
run_sh() {
    if [ "${PURGE_DRY_RUN}" = "1" ]; then
        log "DRY-RUN would run: $*"
    else
        bash -c "$*"
    fi
}

# ---------------------------------------------------------------------------
# Detection (before)
# ---------------------------------------------------------------------------
log "=== DEPLOY-1 Neo4j purge (dry_run=${PURGE_DRY_RUN}) ==="
log "--- BEFORE ---"
found=0

log "[detect] dpkg packages:"
if have dpkg && dpkg -l 2>/dev/null | grep -iE 'neo4j|cypher-shell' ; then
    found=1
else
    log "  (no neo4j/cypher-shell packages)"
fi

log "[detect] binaries:"
for b in neo4j cypher-shell neo4j-admin; do
    if p="$(command -v "${b}" 2>/dev/null)"; then log "  ${b} -> ${p}"; found=1; fi
done
[ "${found}" -eq 0 ] && log "  (no neo4j binaries on PATH)"

log "[detect] directories:"
for d in /var/lib/neo4j /var/log/neo4j /etc/neo4j /usr/share/neo4j /opt/neo4j; do
    if [ -e "${d}" ]; then log "  EXISTS: ${d}"; found=1; fi
done

log "[detect] systemd unit:"
if have systemctl && systemctl list-unit-files 2>/dev/null | grep -qi neo4j; then
    systemctl list-unit-files 2>/dev/null | grep -i neo4j || true
    found=1
else
    log "  (no neo4j systemd unit)"
fi

log "[detect] containers:"
for rt in podman docker; do
    if have "${rt}"; then
        if "${rt}" ps -a 2>/dev/null | grep -i neo4j; then found=1; fi
    fi
done

log "[detect] listeners on 7474/7687 (Neo4j HTTP/Bolt):"
if have ss && ss -tlnp 2>/dev/null | grep -E ':7474|:7687'; then
    found=1
else
    log "  (7474/7687 not listening)"
fi

if [ "${found}" -eq 0 ]; then
    log "Neo4j is NOT present on this host — nothing to purge (idempotent no-op)."
    exit 0
fi

# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------
if [ "${PURGE_DRY_RUN}" = "1" ]; then
    warn "PURGE_DRY_RUN=1 — reporting only; no changes made."
    exit 0
fi

log "stopping/disabling any neo4j service..."
run_sh 'systemctl stop neo4j 2>/dev/null || true; systemctl disable neo4j 2>/dev/null || true'

log "purging neo4j packages (neo4j*, cypher-shell)..."
if have apt-get; then
    run_sh 'DEBIAN_FRONTEND=noninteractive apt-get purge -y "neo4j*" cypher-shell 2>/dev/null || true'
    run_sh 'DEBIAN_FRONTEND=noninteractive apt-get autoremove -y 2>/dev/null || true'
elif have dnf; then
    run_sh 'dnf remove -y neo4j cypher-shell 2>/dev/null || true'
fi

log "removing leftover Neo4j directories (data wipe is authorized)..."
for d in /var/lib/neo4j /var/log/neo4j /etc/neo4j /usr/share/neo4j /opt/neo4j; do
    if [ -e "${d}" ]; then
        log "  rm -rf ${d}"
        run rm -rf "${d}"
    fi
done

log "removing any Neo4j containers/images (no runtime expected on .24)..."
for rt in podman docker; do
    if have "${rt}"; then
        run_sh "${rt} ps -a --format '{{.Names}} {{.Image}}' 2>/dev/null | grep -i neo4j | awk '{print \$1}' | xargs -r ${rt} rm -f 2>/dev/null || true"
        run_sh "${rt} images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -i neo4j | xargs -r ${rt} rmi -f 2>/dev/null || true"
    fi
done

# Kill anything still bound to Neo4j ports (belt-and-braces).
log "ensuring ports 7474/7687 are free..."
run_sh 'pkill -f "neo4j" 2>/dev/null || true'

# ---------------------------------------------------------------------------
# Verification (after)
# ---------------------------------------------------------------------------
log "--- AFTER ---"
leftover=0
if have dpkg && dpkg -l 2>/dev/null | grep -iqE 'neo4j|cypher-shell'; then
    warn "neo4j packages still listed by dpkg"; leftover=1
fi
for d in /var/lib/neo4j /var/log/neo4j /etc/neo4j /usr/share/neo4j; do
    if [ -e "${d}" ]; then warn "directory still present: ${d}"; leftover=1; fi
done
if have ss && ss -tlnp 2>/dev/null | grep -E ':7474|:7687'; then
    warn "a process is STILL listening on 7474/7687"; leftover=1
else
    log "PASS: ports 7474/7687 are free"
fi

if [ "${leftover}" -ne 0 ]; then
    die "Neo4j purge incomplete — inspect the warnings above"
fi
log "PASS: Neo4j fully removed. Graph backend is FalkorDB (redis://127.0.0.1:6379)."
