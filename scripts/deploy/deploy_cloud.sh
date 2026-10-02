#!/usr/bin/env bash
#
# deploy_cloud.sh — deploy the ATE Studio cloud app to 192.168.5.24
# (Debian 12, bare metal, systemd)
#
# Scope, stated up front
# ----------------------
# This is the first of two steps. It does the minimum needed to get a commit
# and its matching SPA onto the host and running:
#
#     facts -> git -> bundle -> deps -> migrate -> install dist -> start -> health
#
# It deliberately does NOT yet do the menu seed, the NATS status report, or the
# .deploy/current.json record. Those come in step two, once this path is proven
# on the host. Splitting it means a failure here points at one of eight things
# rather than at forty.
#
# What was wrong with the version it replaces
# -------------------------------------------
# The previous script rsynced the tree to ~/ATEStudio and declared that target
# non-git, then asserted a hardcoded alembic head:
#
#     EXPECTED_HEAD="d1e2f3a4b5c6"
#
# Neither matched the host. The live deployment is a git checkout at
# /opt/atestudio owned by the service user atestudio, and the head has long
# moved past d1e2f3a4b5c6 — so the script aborted before migrating, with an
# error that reads like a corrupted repository rather than an out-of-date
# script. Pinning a revision in the deploy script means every migration added
# afterwards breaks the deploy, which is a property nobody wants in a tool whose
# job is to deploy.
#
# So this version takes its facts from the host instead of carrying its own:
# the app directory, the service user and the env file are read out of the
# installed systemd unit, and the alembic head is whatever the tree declares.
# The script asserts invariants — exactly one head, the service becomes active,
# health responds, the SPA matches the commit — rather than values that would
# have to be kept up to date by hand.
#
# Three host traps this encodes, each of which cost a deploy:
#
#   1. Ownership drift. A previous deploy left .git/HEAD, .git/index,
#      .git/objects/*, pyproject.toml and uv.lock owned by root inside an
#      atestudio-owned tree, so `git fetch` failed with "permission denied …
#      failed to write object". Ownership is repaired before anything else.
#   2. atestudio's home is /var/lib/atestudio, not /home/atestudio (which does
#      not exist). npm fails confusingly on the latter.
#   3. UV_CACHE_DIR must be writable by the service user. A cache left owned by
#      the deploying user makes uv refuse to start at all.
#
# The SPA comes from CI (scripts/frontend/publish_dist.sh), not from a build on
# the host: 413 MB of node_modules on a box that serves 18 MB of static files
# and whose .venv is 399 MB. The bundle records the hash of the frontend
# sources it was built from, and this script refuses to install a bundle whose
# hash does not match the deployed tree — see the FRONTEND section for why that
# is checked against the frontend tree and not against the commit.
#
# CREDENTIAL-FREE: no password or key appears in this file. SSH auth must work
# via ssh-agent / keys for the remote mode; in LOCAL_ONLY mode the operator
# exports SSH_PASSWORD, which is streamed to `sudo -S` over stdin and never
# written to disk or placed in a remote argv. Application secrets come from an
# env file the operator supplies (ENV_FILE=...).
#
# Run from a CONTROL machine (needs key-based SSH):
#   ENV_FILE=~/cloud.env ./scripts/deploy/deploy_cloud.sh
# Or ON the host:
#   sudo -E SSH_PASSWORD=... ENV_FILE=/home/rpdzkj/cloud.env LOCAL_ONLY=1 \
#     ./scripts/deploy/deploy_cloud.sh
#
# Flags (env vars):
#   REMOTE_HOST      default 192.168.5.24
#   REMOTE_USER      default rpdzkj
#   ENV_FILE         local path to the filled env file. Unset => the env file
#                    already on the host is left untouched (with a warning).
#   REF              git ref to deploy, default origin/dev
#   DIST_REF         SPA bundle ref, default spa-dist
#   FRONTEND_SOURCE  bundle (default) installs the CI-built SPA;
#                    local builds it on the host (needs node; escape hatch)
#   WITH_DEV=1       `uv sync --extra dev` (default: runtime-only)
#   SKIP_PULL=1      do not fetch/merge (re-run migrations only)
#   SKIP_FRONTEND=1  leave the SPA as-is — it will NOT match the commit
#   SKIP_HEALTH=1    skip the final HTTP check
#   LOCAL_ONLY=1     run on the host instead of over SSH
#   SSH_OPTS         extra options for ssh
set -euo pipefail
# `git fetch | tail` exits with tail's status, so without pipefail a failed step
# is swallowed and the script marches on. That happened.
set -o pipefail

# Quoting rule this file must obey, learned the hard way: a redirection operator
# must not sit inside a double-quoted word that is inside a command substitution.
#     x="$(f "cmd 2>&1")"   -> bash -n: unexpected EOF looking for matching `)'
#     x="$(f "cmd" 2>&1)"   -> fine
# Inside "$( )" bash re-parses, and the `&` is seen as a metacharacter, so the
# substitution is never closed.

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REMOTE_HOST="${REMOTE_HOST:-192.168.5.24}"
REMOTE_USER="${REMOTE_USER:-rpdzkj}"
ENV_FILE="${ENV_FILE:-}"
REF="${REF:-origin/dev}"
DIST_REF="${DIST_REF:-spa-dist}"
FRONTEND_SOURCE="${FRONTEND_SOURCE:-bundle}"
WITH_DEV="${WITH_DEV:-0}"
SKIP_PULL="${SKIP_PULL:-0}"
SKIP_FRONTEND="${SKIP_FRONTEND:-0}"
SKIP_HEALTH="${SKIP_HEALTH:-0}"
LOCAL_ONLY="${LOCAL_ONLY:-0}"
SSH_OPTS="${SSH_OPTS:-}"
SERVICE_NAME="${SERVICE_NAME:-ate-cloud}"
HEALTH_PATH="${HEALTH_PATH:-/api/v1/health/ready}"

# How a published bundle is checked for staleness. Must compute the same hash as
# FRONTEND_INPUTS in scripts/frontend/publish_dist.sh over the same paths: the
# deploy compares its own hash of the deployed sources against the one the
# bundle recorded, and if the two sides hash different file sets the comparison
# can never succeed — reported as a stale bundle rather than as the mismatch it
# is. Derived from git rather than hand-listed, for the same reason as on the
# publishing side: the hand-written list had five paths that do not exist in
# this repository and missed postcss/tailwind config and frontend/public.
FRONTEND_INPUTS=(frontend)
# `git ls-tree` does not support `:!exclude` pathspecs — that belongs to git log
# and git diff. It returns nothing and exits 0, so a hash over the empty result
# is stable and would satisfy every check while tracking no file. List
# unfiltered, filter afterwards.
frontend_tree_filter() {
    # git ls-tree output is "<mode> <type> <sha>\t<path>": anchor on the tab that
    # separates the header from the path, not on the start of the line.
    grep -v -e $'\tfrontend/node_modules/' \
             -e $'\tfrontend/dist/' \
             -e $'\tfrontend/\.gitignore$'
}

log()  { printf '[deploy-cloud] %s\n' "$*"; }
warn() { printf '[deploy-cloud] WARNING: %s\n' "$*" >&2; }
die()  { printf '[deploy-cloud] ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

ssh_target="${REMOTE_USER}@${REMOTE_HOST}"

# ---------------------------------------------------------------------------
# Remote execution
# ---------------------------------------------------------------------------
remote_cmd() {
    local cmd="$1"
    if [ "${LOCAL_ONLY}" = "1" ]; then
        bash -lc "${cmd}"
    else
        # shellcheck disable=SC2086
        ssh ${SSH_OPTS} "${ssh_target}" bash -lc "$(printf '%q' "${cmd}")"
    fi
}

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

# Run as the service user. Needed for everything touching the venv or the
# database: the env file is mode 600 and owned by the service user, so uv and
# alembic cannot read configuration as anyone else.
as_service_user() {
    local user="$1"; shift
    remote_sudo "set -e; cd '${APP_DIR}' && sudo -u '${user}' env \
        HOME='${SERVICE_HOME}' \
        UV_CACHE_DIR='${SERVICE_HOME}/.cache/uv' \
        npm_config_cache='${SERVICE_HOME}/.npm' \
        PYTHONPATH='${APP_DIR}/src' \
        $*"
}

# Git against the deployment checkout, as the service user.
#
# The checkout is owned by the service user, and the person running this script
# is not that user. `git fetch` has to write .git/FETCH_HEAD, so running it as
# the SSH account fails with "cannot open '.git/FETCH_HEAD': Permission denied"
# — which is exactly what happened on the first run against this host. The
# ownership repair does not help: the tree is already consistent, the caller
# simply is not its owner.
#
# safe.directory is set for the service user's git too, since git refuses to
# operate on a checkout owned by a different user.
service_git() {
    remote_sudo "cd '${APP_DIR}' && sudo -u '${SERVICE_USER}' env \\
        HOME='${SERVICE_HOME}' git config --global --add safe.directory '${APP_DIR}' \\
        && sudo -u '${SERVICE_USER}' env HOME='${SERVICE_HOME}' git $*"
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
if [ "${LOCAL_ONLY}" != "1" ]; then
    have ssh || die "ssh not found on this control machine"
fi
if [ -n "${ENV_FILE}" ]; then
    [ -f "${ENV_FILE}" ] || die "ENV_FILE does not exist: ${ENV_FILE}"
    if grep -Eq '(password|key|secret)\s*=\s*<' "${ENV_FILE}" 2>/dev/null; then
        die "ENV_FILE ${ENV_FILE} still contains <...> placeholders — the app will not boot"
    fi
else
    warn "ENV_FILE not set — the env file already on the host is left untouched"
fi

# ---------------------------------------------------------------------------
# Read the facts from the installed unit rather than assuming them
# ---------------------------------------------------------------------------
log "reading deployment facts out of ${SERVICE_NAME}.service..."
UNIT_DUMP="$(remote_sudo "cat /etc/systemd/system/${SERVICE_NAME}.service" 2>/dev/null || true)"
[ -n "${UNIT_DUMP}" ] || die "cannot read ${SERVICE_NAME}.service — is it installed?"

SERVICE_USER="$(printf '%s\n' "${UNIT_DUMP}" | sed -n 's/^User=//p' | head -1)"
APP_DIR="$(printf '%s\n' "${UNIT_DUMP}" | sed -n 's/^WorkingDirectory=//p' | head -1)"
ENV_PATH="$(printf '%s\n' "${UNIT_DUMP}" | sed -n 's/^EnvironmentFile=//p' | grep -v '^-' | head -1 || true)"

[ -n "${SERVICE_USER}" ] || die "no User= in ${SERVICE_NAME}.service"
[ -n "${APP_DIR}" ]     || die "no WorkingDirectory= in ${SERVICE_NAME}.service"
[ -d "${APP_DIR}" ]     || die "WorkingDirectory ${APP_DIR} does not exist on the host"

# Home from passwd, not from $HOME or a guess: on this host the service user's
# home is /var/lib/atestudio and /home/<user> does not exist, which is enough to
# make npm fail with a misleading error.
SERVICE_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
[ -n "${SERVICE_HOME}" ] || die "no passwd entry for ${SERVICE_USER}"

UV_BIN="$(command -v uv || echo /usr/local/bin/uv)"
[ -x "${UV_BIN}" ] || die "uv not found (looked in PATH and /usr/local/bin)"

log "target       : ${APP_DIR}"
log "service user : ${SERVICE_USER} (home ${SERVICE_HOME})"
log "env file     : ${ENV_PATH:-<none declared>}"
log "ref          : ${REF}"
log "bundle ref   : ${DIST_REF} (${FRONTEND_SOURCE})"

# ---------------------------------------------------------------------------
# Ownership repair, first: everything below writes into this tree, and one
# root-owned file makes git or uv fail in a message that does not mention
# ownership at all.
# ---------------------------------------------------------------------------
log "repairing ownership under ${APP_DIR} (expected owner ${SERVICE_USER})..."
foreign="$(remote_sudo "find '${APP_DIR}' ! -user '${SERVICE_USER}' -print -quit" || true)"
if [ -n "${foreign}" ]; then
    warn "found an entry not owned by ${SERVICE_USER}: ${foreign}"
    warn "this is what breaks 'git fetch' with 'permission denied'; repairing"
    remote_sudo "chown -R '${SERVICE_USER}:${SERVICE_USER}' '${APP_DIR}'"
fi
remote_sudo "mkdir -p '${SERVICE_HOME}/.cache/uv' '${APP_DIR}/logs'"
remote_sudo "chown -R '${SERVICE_USER}:${SERVICE_USER}' '${SERVICE_HOME}/.cache' '${APP_DIR}/logs'"
log "PASS: ownership consistent"

# ---------------------------------------------------------------------------
# Stop the service. Nothing should hold :8000 while migrations run.
# ---------------------------------------------------------------------------
log "stopping ${SERVICE_NAME}..."
remote_sudo "systemctl stop '${SERVICE_NAME}'" 2>/dev/null || true
if remote_cmd 'ss -tlnp 2>/dev/null | grep -q ":8000 "'; then
    warn ":8000 still held after stopping the service"
    remote_cmd 'ss -tlnp 2>/dev/null | grep ":8000 " || true; pgrep -af "uvicorn.*ate_cloud" || true'
    remote_sudo 'pkill -f "uvicorn ate_cloud.main:app" 2>/dev/null || true; sleep 2'
    if remote_cmd 'ss -tlnp 2>/dev/null | grep -q ":8000 "'; then
        die ":8000 is STILL held by another process"
    fi
fi
log "PASS: :8000 free"

# ---------------------------------------------------------------------------
# Code. The target is a git checkout, so deploying is merging a ref.
# ---------------------------------------------------------------------------
if [ "${SKIP_PULL}" != "1" ]; then
    log "[code] fetching and fast-forwarding to ${REF}..."
    service_git "fetch origin" 2>&1 | tail -3
    # Detached HEAD is the normal state after a scripted checkout and it hides
    # what is deployed from `git log`. Move onto the branch.
    branch_cmd="rev-parse --abbrev-ref HEAD"
    branch="$(service_git "${branch_cmd}" || echo HEAD)"
    if [ "${branch}" = "HEAD" ]; then
        warn "host is on a detached HEAD; moving onto 'dev' so the deploy is visible"
        service_git "checkout dev" 2>&1 | tail -2
    fi
    merge_cmd="merge --ff-only ${REF}"
    service_git "${merge_cmd}" 2>&1 | tail -5
else
    warn "SKIP_PULL=1 — deploying whatever is already checked out"
fi
sha_cmd="rev-parse HEAD"
DEPLOY_SHA="$(service_git "${sha_cmd}")"
short_cmd="rev-parse --short HEAD"
log "PASS: at $(service_git "${short_cmd}")"

# Refuse to continue with a dirty tree: migrations run against the checkout, so
# uncommitted edits would make the running code differ from the recorded commit.
status_cmd="status --porcelain --untracked-files=no"
dirty="$(service_git "${status_cmd}" || true)"
if [ -n "${dirty}" ]; then
    warn "tracked files are modified on the host:"
    printf '%s\n' "${dirty}" | sed 's/^/    /'
    die "commit or discard them — a deploy must be reproducible from the recorded SHA"
fi

# ---------------------------------------------------------------------------
# Dependencies, from pyproject. Runtime-only by default; dev tooling is an
# optional extra, not a group, so plain `uv sync` is the correct runtime deploy.
# ---------------------------------------------------------------------------
if [ "${WITH_DEV}" = "1" ]; then
    log "[deps] uv sync --extra dev"
    as_service_user "${SERVICE_USER}" "${UV_BIN} sync --extra dev" 2>&1 | tail -5
else
    log "[deps] uv sync (runtime only; WITH_DEV=1 for the dev extra)"
    as_service_user "${SERVICE_USER}" "${UV_BIN} sync" 2>&1 | tail -5
fi

# ---------------------------------------------------------------------------
# Migrations. The head is read from the tree, and the invariant asserted is
# "exactly one head" — a repository with two heads has no single upgrade path,
# and `alembic upgrade head` would fail with a message about itself rather than
# about the cause.
# ---------------------------------------------------------------------------
log "[migrate] reading alembic heads from the tree..."
heads_out="$(as_service_user "${SERVICE_USER}" "${UV_BIN} run alembic heads" 2>&1 || true)"
head_ids="$(printf '%s\n' "${heads_out}" | grep -oE '^[[:space:]]*[0-9a-f]{6,}' | tr -d '[:space:]' || true)"
head_count="$(printf '%s\n' "${head_ids}" | grep -c . || true)"
[ "${head_count}" -ge 1 ] || die "could not read an alembic head from the tree:
${heads_out}"
if [ "${head_count}" -ne 1 ]; then
    die "expected exactly ONE alembic head, found ${head_count}: ${head_ids}
resolve the migration branches before deploying"
fi
HEAD_ID="$(printf '%s\n' "${head_ids}" | head -1)"
log "PASS: single head ${HEAD_ID}"

current="$(as_service_user "${SERVICE_USER}" "${UV_BIN} run alembic current" 2>&1 | tail -1 || true)"
log "[migrate] current: ${current}"
log "[migrate] upgrading to head..."
as_service_user "${SERVICE_USER}" "${UV_BIN} run alembic upgrade head" 2>&1 | tail -6
after="$(as_service_user "${SERVICE_USER}" "${UV_BIN} run alembic current" 2>&1 | tail -1 || true)"
log "PASS: now at ${after}"

# ---------------------------------------------------------------------------
# The SPA
#
# Checked against the frontend tree, not the commit. A commit-SHA comparison is
# safe but wrong: any backend-only commit moves the SHA while leaving the bundle
# byte-identical, so it would reject every backend deploy and wait on a build
# that would produce the same bytes. The tree hash answers the real question —
# would this bundle differ from the deployed sources?
# ---------------------------------------------------------------------------
if [ "${SKIP_FRONTEND}" != "1" ]; then
    if [ "${FRONTEND_SOURCE}" = "bundle" ]; then
        log "[frontend] fetching bundle ref ${DIST_REF}..."
        bundle_fetch="fetch origin 'refs/heads/${DIST_REF}:refs/remotes/origin/${DIST_REF}' --force"
        service_git "${bundle_fetch}" 2>&1 | tail -2
        # Commands are assembled into a variable and then passed to service_git.
        # Written inline as "$(remote_cmd "git ... 'ref')" it is a syntax error:
        # the inner " closes the outer quote, so the substitution is never
        # closed and bash reports the error at the next `else` — hundreds of
        # lines away from the line that is actually wrong. That rule is noted at
        # the top of this file; it applies to every git call here, not just the
        # one it was discovered on.
        resolve_cmd="rev-parse 'refs/remotes/origin/${DIST_REF}'"
        BUNDLE_SHA="$(service_git "${resolve_cmd}")"
        log "  bundle commit: ${BUNDLE_SHA}"

        log "[frontend] checking the bundle against the deployed frontend sources..."
        show_cmd="show 'refs/remotes/origin/${DIST_REF}:frontend/dist/.build-info.json'"
        bundle_info="$(service_git "${show_cmd}" || true)"
        recorded_sha="$(printf '%s' "${bundle_info}" | sed -n 's/.*"source_sha": *"\([0-9a-f]*\)".*/\1/p')"
        recorded_tree="$(printf '%s' "${bundle_info}" | sed -n 's/.*"frontend_tree": *"\([^"]*\)".*/\1/p')"
        # The filter is written to a temp file on the host and sourced there, so
        # there is one copy of the rule (this file) and the pipeline stays a
        # plain pipeline. Passing it through nested quotes instead is both
        # unreadable and a quoting hazard — see the note at the top of this file.
        remote_sudo "cat > /tmp/.frontend-filter.sh <<'FILTER_EOF'
$(declare -f frontend_tree_filter)
FILTER_EOF"
        tree_script="cd '${APP_DIR}' && . /tmp/.frontend-filter.sh && git ls-tree -r HEAD -- ${FRONTEND_INPUTS[*]} | frontend_tree_filter | git hash-object --stdin"
        deployed_tree="$(remote_sudo "sudo -u '${SERVICE_USER}' env HOME='${SERVICE_HOME}' bash -c \"${tree_script}\"" 2>/dev/null || echo unavailable)"
        remote_sudo "rm -f /tmp/.frontend-filter.sh"

        if [ -z "${recorded_tree}" ] || [ "${recorded_tree}" = "unavailable" ]; then
            warn "bundle carries no usable frontend_tree (built before that field"
            warn "existed, or the sources could not be hashed) - falling back to a"
            warn "whole-commit comparison, which is stricter than necessary."
            if [ "${recorded_sha}" != "${DEPLOY_SHA}" ]; then
                printf '%s\n' "${bundle_info}" >&2
                die "bundle is from ${recorded_sha:-<none>}, this deploy is ${DEPLOY_SHA}, and neither can be compared on frontend sources.
Republish:  scripts/frontend/publish_dist.sh"
            fi
            log "PASS: bundle was built from the deployed commit"
        elif [ "${recorded_tree}" != "${deployed_tree}" ]; then
            printf '%s\n' "${bundle_info}" >&2
            die "bundle was built from frontend tree ${recorded_tree}
but the deployed sources hash to ${deployed_tree}
The interface would not match the API. Wait for the CI run on this commit, or
publish a bundle from the code you intend to deploy:
  scripts/frontend/publish_dist.sh
Or bypass knowingly:  FRONTEND_SOURCE=local"
        else
            log "PASS: frontend tree matches (${recorded_tree})"
        fi

        # Replace the whole directory rather than copying over it. Vite's output
        # is content-hashed so a leftover file is never referenced by the new
        # index.html — but it is still served, and 16 MB of orphaned JS is what
        # makes a bundle impossible to reason about later.
        log "[frontend] installing bundle into ${APP_DIR}/frontend/dist..."
        # rm -rf runs as root on purpose: the directory is owned by the service
        # user, and the archive is then written as that user.
        remote_sudo "rm -rf '${APP_DIR}/frontend/dist' && mkdir -p '${APP_DIR}/frontend/dist'"
        # Extract INTO the dist directory, with the two leading path components
        # stripped: the archive holds frontend/dist/... and the target wants the
        # contents of dist/ directly. Extracting into APP_DIR instead put
        # index.html and assets/ at the application root, which is how the first
        # run reported "installed 0 files" while quietly littering the checkout
        # with 197 untracked files.
        #
        # The pipeline lives in the string handed to remote_sudo, which runs it
        # under `sudo bash -c`, so the `|` is a real pipe there. Two earlier
        # attempts were wrong: writing it inside a quoted argument made the `|`
        # literal (tar would run on the control machine), and routing it through
        # `bash -c "$(printf '%q' ...)"` quoted the whole command into one word,
        # so bash tried to execute that word as a program name.
        # Only git needs to be the service user; tar can be root, and the chown
        # afterwards fixes ownership.
        archive_cmd="cd '${APP_DIR}' && sudo -u '${SERVICE_USER}' env HOME='${SERVICE_HOME}' git archive 'refs/remotes/origin/${DIST_REF}' frontend/dist | tar -x -C '${APP_DIR}/frontend/dist' --strip-components=2"
        remote_sudo "${archive_cmd}" 2>&1 | tail -3

        # A run that extracted to the wrong place leaves the application root
        # full of SPA files. Detect it rather than leaving it to be discovered
        # later as a mysteriously dirty checkout.
        # `|| true` is load-bearing, not defensive noise. `ls` on a missing file
        # exits 2, and under `set -e` a failing command substitution terminates
        # the script — with ls's status as the exit code and no message. The
        # first run of this check did exactly that: every preceding step passed,
        # 198 files were installed, and the deploy then died silently with exit
        # 2 pointing at nothing. Every `x="$(cmd ...)"` in this script whose cmd
        # can legitimately fail needs the same guard.
        stray="$(remote_cmd "ls '${APP_DIR}/index.html' 2>/dev/null" || true)"
        if [ -n "${stray}" ]; then
            die "SPA files are sitting in the application root (${APP_DIR}/index.html exists).
An earlier run extracted with -C APP_DIR instead of -C APP_DIR/frontend/dist.
Remove the stray files before deploying again:
  find ${APP_DIR} -maxdepth 1 \\( -name index.html -o -name assets -o -name monacoeditorwork -o -name '.build-info.json' -o -name 'favicon.svg' -o -name 'icons.svg' \\) -exec rm -rf {} +"
        fi
        remote_sudo "chown -R '${SERVICE_USER}:${SERVICE_USER}' '${APP_DIR}/frontend/dist'"
        n_files="$(remote_cmd "find '${APP_DIR}/frontend/dist' -type f | wc -l" || true)"
        log "PASS: installed ${n_files} files"
    else
        warn "FRONTEND_SOURCE=local — building the SPA on the production host."
        warn "this needs node and leaves node_modules behind, which is what this"
        warn "script exists to stop. Use it to recover, not as a routine path."
        as_service_user "${SERVICE_USER}" "bash -c \"cd '${APP_DIR}/frontend' && npm ci\"" 2>&1 | tail -3
        as_service_user "${SERVICE_USER}" "bash -c \"cd '${APP_DIR}/frontend' && npm run build\"" 2>&1 | tail -5
        log "PASS: SPA rebuilt on the host"
    fi
else
    warn "SKIP_FRONTEND=1 — the interface is whatever the previous deploy left, and will not match this commit's API"
fi

# ---------------------------------------------------------------------------
# Env file. Only (re)installed when the operator supplies one.
# ---------------------------------------------------------------------------
if [ -n "${ENV_FILE}" ] && [ -n "${ENV_PATH}" ]; then
    log "[env] installing ${ENV_PATH} (mode 600)..."
    if [ "${LOCAL_ONLY}" = "1" ]; then
        install -m 600 "${ENV_FILE}" "${ENV_PATH}"
    else
        # shellcheck disable=SC2086
        ssh ${SSH_OPTS} "${ssh_target}" "cat > /tmp/.env.deploy.\$\$ && chmod 600 /tmp/.env.deploy.\$\$" < "${ENV_FILE}"
        remote_sudo "install -o '${SERVICE_USER}' -g '${SERVICE_USER}' -m 600 /tmp/.env.deploy.\$\$ '${ENV_PATH}' && rm -f /tmp/.env.deploy.\$\$"
    fi
    log "PASS: env file installed"
fi

# ---------------------------------------------------------------------------
# Start, then health
# ---------------------------------------------------------------------------
log "[start] daemon-reload + restart ${SERVICE_NAME}..."
remote_sudo "systemctl daemon-reload"
remote_sudo "systemctl enable '${SERVICE_NAME}'" >/dev/null 2>&1 || true
remote_sudo "systemctl restart '${SERVICE_NAME}'"
for i in $(seq 1 20); do
    if remote_cmd "systemctl is-active --quiet '${SERVICE_NAME}'"; then
        break
    fi
    if [ "$i" = 20 ]; then
        remote_cmd "tail -40 '${APP_DIR}/logs/api.log' 2>/dev/null" || true
        die "${SERVICE_NAME} failed to start"
    fi
    sleep 1
done
log "PASS: ${SERVICE_NAME} is active"

if [ "${SKIP_HEALTH}" != "1" ]; then
    log "[health] ${HEALTH_PATH} on 127.0.0.1:8000..."
    ok=0
    for i in $(seq 1 20); do
        if remote_cmd "curl -fsS -o /dev/null 'http://127.0.0.1:8000${HEALTH_PATH}'"; then
            ok=1; break
        fi
        sleep 2
    done
    if [ "${ok}" != "1" ]; then
        remote_cmd "tail -60 '${APP_DIR}/logs/api.log' 2>/dev/null" || true
        die "health check failed on 127.0.0.1:8000${HEALTH_PATH}"
    fi
    log "PASS: ${HEALTH_PATH} responded"
fi

# ---------------------------------------------------------------------------
# Not in this step, deliberately: the menu seed, the NATS status report and the
# .deploy/current.json record. A deploy that lands without the seed shows menus
# that do not match the code, which step two fixes.
# ---------------------------------------------------------------------------
log "============================================================"
log "deploy complete: ${SERVICE_NAME}"
log "  commit  : ${DEPLOY_SHA}"
log "  app dir : ${APP_DIR}"
log "  logs    : tail -f ${APP_DIR}/logs/api.log   (stdout goes to a file, not journald)"
log "============================================================"
