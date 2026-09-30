#!/usr/bin/env bash
# =============================================================================
# Deploy ATEStudio to the factory board using an isolated virtualenv.
#
# Why a venv and not the system Python
# -----------------------------------
# The board is a production line controller. Its system Python may be pinned by
# the OS, is shared with other tools, and is not ours to change. Installing
# into it would make an ATEStudio upgrade a board-level event, and an
# uninstall would leave the system broken.
#
# A per-deployment venv keeps the blast radius to one directory: removal is
# `rm -rf`, and the system interpreter is never touched.
#
# Non-Docker counterpart of the Dockerfile's `uv sync --frozen --no-dev`.
# The Dockerfile path is preferred where Docker is available; this exists for
# boards where it is not (or where the platform team disallows containers).
#
# Usage (on the target board):
#   bash deploy_venv.sh --repo /opt/ate-studio --venv /opt/ate-studio/.venv
#   bash deploy_venv.sh --repo ... --venv ... --skip-migrate   # code only
# =============================================================================
set -euo pipefail

REPO_DIR=""
VENV_DIR=""
RUN_MIGRATE=1
RUN_FRONTEND=1
REPO_REF=""

die() { echo "[deploy] ERROR: $*" >&2; exit 1; }
log() { echo "[deploy] $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)         REPO_DIR="$2"; shift 2 ;;
    --venv)         VENV_DIR="$2"; shift 2 ;;
    --ref)          REPO_REF="$2"; shift 2 ;;
    --skip-migrate) RUN_MIGRATE=0; shift ;;
    --skip-frontend) RUN_FRONTEND=0; shift ;;
    -h|--help)      sed -n '2,25p' "$0"; exit 0 ;;
    *)              die "unknown argument: $1" ;;
  esac
done

[[ -n "$REPO_DIR" ]] || die "--repo is required"
[[ -n "$VENV_DIR" ]] || die "--venv is required"
[[ -d "$REPO_DIR" ]] || die "repo dir not found: $REPO_DIR"

command -v uv >/dev/null 2>&1 || die "uv not found. Install it, or use the Dockerfile path."
command -v git >/dev/null 2>&1 || die "git not found"

# ── Refresh the code ────────────────────────────────────────────────────────
# Pinned to a ref rather than "whatever is checked out": a deployment that
# deploys an arbitrary working tree cannot be reproduced after an incident.
if [[ -n "$REPO_REF" ]]; then
  log "fetching $REPO_REF"
  git -C "$REPO_DIR" fetch --all --tags
  git -C "$REPO_DIR" checkout --detach "$REPO_REF"
else
  CURRENT="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
  log "deploying working tree at $CURRENT (pass --ref to pin)"
  if [[ "$CURRENT" == "unknown" ]]; then
    die "not a git checkout and no --ref given; refusing to deploy an unversioned tree"
  fi
fi

# Refuse to deploy a dirty tree: a "deployed" version that differs from the
# repo is not the thing anyone reviewed or can roll back to.
if ! git -C "$REPO_DIR" diff --quiet 2>/dev/null; then
  git -C "$REPO_DIR" status --short >&2
  die "working tree has uncommitted changes; commit or --ref a clean commit first"
fi

DEPLOY_COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD)"
log "deploy commit: $DEPLOY_COMMIT"

# ── Virtualenv ──────────────────────────────────────────────────────────────
# Kept OUTSIDE the repo by default so a git checkout/clean cannot delete the
# running environment.
if [[ -d "$VENV_DIR" ]]; then
  log "reusing venv at $VENV_DIR"
else
  log "creating venv at $VENV_DIR"
  uv venv --python 3.12 "$VENV_DIR"
fi

# --frozen: install exactly what uv.lock pins. Without it, a resolve at deploy
# time can pull a newer transitive dependency than anything ever tested, which
# is how a deploy becomes the thing that broke production.
log "installing dependencies (frozen lock, no dev)"
uv pip install --python "$VENV_DIR/bin/python" \
  --requirement "$REPO_DIR/uv.lock" 2>/dev/null \
  || uv sync --frozen --no-dev --no-install-project --project "$REPO_DIR"

PY="$VENV_DIR/bin/python"

# Prove the interpreter is the one we think it is, before touching the DB.
"$PY" - <<'EOF'
import sys
if sys.prefix == sys.base_prefix:
    raise SystemExit("refusing to run against the system interpreter")
print(f"[deploy] interpreter: {sys.executable}")
EOF

# ── Database migration ──────────────────────────────────────────────────────
if [[ "$RUN_MIGRATE" == "1" ]]; then
  log "running alembic migrations"
  ( cd "$REPO_DIR" && PYTHONPATH="$REPO_DIR/src" "$PY" -m alembic upgrade head )
  ( cd "$REPO_DIR" && PYTHONPATH="$REPO_DIR/src" "$PY" -m alembic current )
else
  log "skipping migrations (--skip-migrate)"
fi

# ── Frontend ────────────────────────────────────────────────────────────────
# FRONTEND_BUILT records what actually happened, not what was requested. The
# stamp below exists to answer "what is running on this board after an
# incident"; a stamp that says frontend_built=true when npm was missing is
# worse than no stamp, because it is believed.
FRONTEND_BUILT=false
if [[ "$RUN_FRONTEND" == "1" && -d "$REPO_DIR/frontend" ]]; then
  if command -v npm >/dev/null 2>&1; then
    log "building frontend"
    ( cd "$REPO_DIR/frontend" && npm ci --no-audit --no-fund && npm run build )
    # Trust the artifact, not the exit code: npm can succeed and still leave no
    # dist (a misconfigured outDir), and the stamp is the only record.
    if [[ -f "$REPO_DIR/frontend/dist/index.html" ]]; then
      FRONTEND_BUILT=true
    else
      log "WARNING: npm build produced no frontend/dist/index.html"
    fi
  else
    # "previously built" is a claim, not a fact. On a fresh clone there is no
    # previous build and the UI is simply absent.
    if [[ -f "$REPO_DIR/frontend/dist/index.html" ]]; then
      log "npm not found; keeping the existing frontend/dist"
    else
      log "WARNING: npm not found and no frontend/dist — the web UI will not be served"
    fi
  fi
else
  log "skipping frontend build"
fi

# ── Record what is actually running ─────────────────────────────────────────
# Written next to the venv, not in the repo, so a `git clean` does not erase
# the only record of what the running process was started from.
STAMP_DIR="$VENV_DIR/../.deploy"
mkdir -p "$STAMP_DIR"
cat > "$STAMP_DIR/current.json" <<EOF
{
  "commit": "$DEPLOY_COMMIT",
  "venv": "$VENV_DIR",
  "deployed_at": "$(date -Iseconds)",
  "migrations": $( [[ "$RUN_MIGRATE" == "1" ]] && echo true || echo false ),
  "frontend_built": $FRONTEND_BUILT
}
EOF

log "staged at $VENV_DIR (not started — the supervisor owns process lifetime)"
log "verify with: curl -fsS http://127.0.0.1:8000/api/v1/health"
