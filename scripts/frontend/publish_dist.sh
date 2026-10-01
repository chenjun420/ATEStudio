#!/usr/bin/env bash
#
# publish_dist.sh — build the SPA and publish it as a standalone git ref
#
# Why this exists
# ---------------
# FastAPI mounts the SPA from <app>/frontend/dist, so the bundle is part of the
# deploy. Until now the deploy script built it *on the production host*, which
# meant:
#
#   * 413 MB of node_modules on a box that serves 18 MB of static files and
#     whose .venv is 399 MB — build tooling larger than the application;
#   * every backend-only deploy paid `npm ci` + `npm run build` even when no
#     frontend file had changed;
#   * two sources of truth for the bundle, because whoever built it last decided
#     what the interface looked like.
#
# Building in CI and shipping the result as a git ref removes node from the
# production host and keeps the deploy atomic: the bundle records the source
# commit it was built from, and deploy_cloud.sh refuses to install a bundle
# that does not match the commit it is deploying.
#
# Why a git ref rather than a release asset or a workflow artifact
# -----------------------------------------------------------------
# The production host fetches this repository anonymously over HTTPS, as a
# public repo, with no token and no `gh` CLI and no API access to speak of. A
# plain `git fetch origin spa-dist` is the only artifact transport that works
# there without provisioning credentials on a host that should not have them.
# A release asset would need a token; a workflow artifact always needs one.
#
# The ref is an orphan: it shares no history with the source branch, holds only
# `dist/`, and is force-pushed on every publish. That is deliberate — it is a
# build output, not a branch anyone reviews or merges.
#
# Usage
# -----
#   scripts/frontend/publish_dist.sh              # build, record, push
#   DIST_REF=local/dist scripts/frontend/publish_dist.sh   # to another ref
#   SKIP_BUILD=1 scripts/frontend/publish_dist.sh  # republish existing dist/
#
# Run from the repository root or from anywhere; paths are resolved from the
# repository top level, which this script locates from its own path.
set -euo pipefail
set -o pipefail  # redundant with -e above, but a pipe must never hide a failure

# Quoting rule, learned the hard way in deploy_cloud.sh: a redirection operator
# must not sit inside a double-quoted word that is inside a command substitution.
#   x="$(f "cmd 2>&1")"   -> bash -n: unexpected EOF looking for matching `)'
#   x="$(f "cmd" 2>&1)"   -> fine
# Kept here because this file has the same shape of construct.

# ---------------------------------------------------------------------------
# Locate the repository from this script's own path
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DIST_REF="${DIST_REF:-spa-dist}"
SKIP_BUILD="${SKIP_BUILD:-0}"
FRONTEND_DIR="${REPO_ROOT}/frontend"
DIST_DIR="${FRONTEND_DIR}/dist"
BUILD_INFO="${DIST_DIR}/.build-info.json"

log()  { printf '[publish-dist] %s\n' "$*"; }
die()  { printf '[publish-dist] ERROR: %s\n' "$*" >&2; exit 1; }

cd "${REPO_ROOT}"
command -v git >/dev/null 2>&1 || die "git not found"
git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository (${REPO_ROOT})"

# The source commit is recorded in the bundle. In CI this is the checked-out
# SHA, which is what makes the deploy-time assertion meaningful.
SOURCE_SHA="$(git rev-parse HEAD)"
SOURCE_REF="$(git rev-parse --abbrev-ref HEAD)"
[ "${SOURCE_REF}" = "HEAD" ] && SOURCE_REF="(detached)"

# A hash of the frontend *sources*, not of the commit.
#
# The deploy originally asserted source_sha == the deployed commit. That is safe
# but wrong in practice: any backend-only commit changes the SHA while leaving
# the bundle byte-identical, so every backend deploy would have to wait for a
# fresh build. That defeats the point of moving the build out of the deploy.
# Hashing the inputs the build actually reads answers the real question — "would
# this bundle differ from the current sources?" — so a bundle is rejected exactly
# when a frontend file changed, and reused otherwise.
FRONTEND_INPUTS=(
    frontend
)
# node_modules and dist are build products, not inputs. .gitignore does not
# change the bundle but would move the hash for no reason, and a hash that moves
# for no reason trains people to ignore it.
#
# `git ls-tree` does NOT support the `:!exclude` pathspec magic — that belongs to
# git log and git diff. Passing it there returns zero rows with exit status 0, so
# a hash over the empty result is perfectly stable and satisfies every check
# while tracking no file at all. Both sides therefore list everything and filter
# afterwards. That mistake was made here once already; the count floor below
# exists because it is what would have caught it.
frontend_tree_filter() {
    # git ls-tree output is "<mode> <type> <sha>\t<path>". The pattern is
    # anchored on the tab that separates the header from the path, not on the
    # start of the line, because the line begins with the mode.
    grep -v -e $'\tfrontend/node_modules/' \
             -e $'\tfrontend/dist/' \
             -e $'\tfrontend/\.gitignore$'
}
frontend_tree_hash() {
    git ls-tree -r HEAD -- "${FRONTEND_INPUTS[@]}" \
        | frontend_tree_filter \
        | git hash-object --stdin
}
frontend_tree_count() {
    # --name-only output has no object header and no tab.
    git ls-tree -r --name-only HEAD -- "${FRONTEND_INPUTS[@]}" \
        | grep -v -e '^frontend/node_modules/' \
                 -e '^frontend/dist/' \
                 -e '^frontend/\.gitignore$' \
        | wc -l | tr -d ' '
}

FRONTEND_TREE="$(frontend_tree_hash)"
n_inputs="$(frontend_tree_count)"
log "frontend tree: ${FRONTEND_TREE} (${n_inputs} tracked files)"

# The hash is only meaningful if it covers the source tree. A broken filter
# produces a stable hash of nothing, which passes every check while tracking no
# file at all.
if [ "${n_inputs}" -lt 50 ]; then
    die "frontend tree covers only ${n_inputs} files — the exclusion filter is broken"
fi

# The hash comes from HEAD, while `npm run build` reads the working tree, so
# publishing with uncommitted frontend edits would attach one commit's provenance
# to another commit's bytes. Only frontend inputs are checked: an unrelated dirty
# file does not affect the bundle and must not block a publish.
dirty_frontend="$(git status --porcelain -- "${FRONTEND_INPUTS[@]}" ':!frontend/dist' ':!frontend/node_modules' || true)"
if [ -n "${dirty_frontend}" ]; then
    printf '%s\n' "${dirty_frontend}" >&2
    die "frontend sources have uncommitted changes, so the bundle built from the working tree would not match the provenance recorded in it.
Commit them, or publish from a clean checkout."
fi

log "repository : ${REPO_ROOT}"
log "source     : ${SOURCE_SHA} (${SOURCE_REF})"
log "target ref : refs/heads/${DIST_REF}"

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
if [ "${SKIP_BUILD}" != "1" ]; then
    command -v npm >/dev/null 2>&1 || die "npm not found in PATH"
    cd "${FRONTEND_DIR}"
    log "npm ci..."
    npm ci
    log "npm run build (tsc && vite build)..."
    npm run build
    cd "${REPO_ROOT}"
    [ -f "${DIST_DIR}/index.html" ] || die "build produced no index.html in ${DIST_DIR}"
fi

[ -f "${DIST_DIR}/index.html" ] || die "no bundle at ${DIST_DIR} (index.html missing)"

# ---------------------------------------------------------------------------
# Record provenance inside the bundle
# ---------------------------------------------------------------------------
# The deploy asserts source_sha against the commit it is deploying. Without it
# a stale bundle installs cleanly and the interface quietly stops matching the
# API — the exact failure deploy_cloud.sh used to call out in a comment and
# could not detect.
#
# No secrets: a commit SHA, a timestamp, and tool versions. This file is served
# publicly by the SPA catch-all route, so it must stay boring.
NODE_VERSION="unknown"
command -v node >/dev/null 2>&1 && NODE_VERSION="$(node --version)"

# `built_by` is provenance that decides whether a bundle is trustworthy, so it
# must not be assembled by string interpolation. "${GITHUB_ACTIONS:+...}${GITHUB_ACTIONS:-local}"
# yields "github-actionstrue" when the variable is set to "true", because the
# :+ form substitutes its own operand and ignores the value. That shipped in the
# first smoke run and recorded a false claim about provenance. Assigned in
# branches instead.
if [ -n "${GITHUB_ACTIONS:-}" ]; then
    BUILT_BY="github-actions"
else
    BUILT_BY="local"
fi

log "writing $(basename "${BUILD_INFO}")..."
cat > "${BUILD_INFO}" <<JSON
{
  "source_sha": "${SOURCE_SHA}",
  "source_ref": "${SOURCE_REF}",
  "frontend_tree": "${FRONTEND_TREE}",
  "built_at": "$(date -Iseconds)",
  "built_by": "${BUILT_BY}",
  "ci_run": "${GITHUB_RUN_ID:-}",
  "node_version": "${NODE_VERSION}",
  "bundle_ref": "refs/heads/${DIST_REF}"
}
JSON

# ---------------------------------------------------------------------------
# Publish as an orphan ref, without touching the working tree
# ---------------------------------------------------------------------------
# A temporary index keeps the source branch's index and working tree untouched,
# so this cannot leave the checkout in a half-staged state — the failure mode
# that bit a commit earlier in this project, where `git commit -- <path>`
# carried along everything else that happened to be staged.
#
# `git add -f` is required: dist/ is in .gitignore, which is correct for the
# source branch and wrong for a branch whose entire content is dist/.
log "staging dist/ into a temporary index..."
TMP_INDEX="$(mktemp)"
cleanup() { rm -f "${TMP_INDEX}"; }
trap cleanup EXIT
rm -f "${TMP_INDEX}"   # git refuses to use an index file that already exists

# The path must be repo-relative: the temp index starts empty, so git resolves
# it against the cwd, which is REPO_ROOT.
GIT_INDEX_FILE="${TMP_INDEX}" git add -f -- frontend/dist \
  || die "could not stage frontend/dist — is ${DIST_DIR} inside the repository?"
TREE="$(GIT_INDEX_FILE="${TMP_INDEX}" git write-tree)"
[ -n "${TREE}" ] || die "empty tree — dist/ staged nothing"

FILE_COUNT="$(GIT_INDEX_FILE="${TMP_INDEX}" git ls-files | wc -l | tr -d ' ')"
log "tree ${TREE} with ${FILE_COUNT} files"

# Parent: the previous bundle, so the ref has history you can diff. Falls back
# to no parent on the first publish.
PREV="$(git ls-remote --heads origin "refs/heads/${DIST_REF}" 2>/dev/null | awk '{print $1}' || true)"
if [ -n "${PREV}" ] && git cat-file -e "${PREV}^{commit}" 2>/dev/null; then
    PARENT="${PREV}"
    log "parent  : ${PREV} (previous bundle)"
else
    PARENT=""
    log "parent  : none (first publish, orphan)"
fi

if [ -n "${PARENT}" ]; then
    COMMIT="$(git commit-tree "${TREE}" -p "${PARENT}" -m "spa: ${SOURCE_SHA} (${SOURCE_REF})")"
else
    COMMIT="$(git commit-tree "${TREE}" -m "spa: ${SOURCE_SHA} (${SOURCE_REF})")"
fi
log "commit  : ${COMMIT}"

# Two things must never be silently wrong, because both would make the bundle
# misrepresent itself and the deploy-time assertion would then be checking a
# self-consistent lie:
#
#   1. the recorded SHA has to be the SHA we just built — a build that read a
#      different tree (stale checkout, a dirty worktree in a way that mattered)
#      would otherwise ship a bundle whose provenance is wrong;
#   2. the recorded ref has to be what we think it is. A build run from a
#      detached HEAD must not claim a branch name.
RECORDED="$(sed -n 's/.*"source_sha": *"\([0-9a-f]\{7,40\}\)".*/\1/p' "${BUILD_INFO}")"
[ "${RECORDED}" = "${SOURCE_SHA}" ] \
  || die "build-info records ${RECORDED} but the source is ${SOURCE_SHA} — refusing to publish a bundle that misreports its origin"

RECORDED_BY="$(sed -n 's/.*"built_by": *"\([^"]*\)".*/\1/p' "${BUILD_INFO}")"
[ "${RECORDED_BY}" = "${BUILT_BY}" ] \
  || die "build-info records built_by=${RECORDED_BY} but this run is ${BUILT_BY} — refusing to publish a bundle that misreports who built it"

log "pushing to origin refs/heads/${DIST_REF}..."
# Force: this is a build output on an orphan ref, so fast-forward is not a
# meaningful expectation and a rejected push would leave the board on an old
# bundle with no signal as to why.
git push --force origin "${COMMIT}:refs/heads/${DIST_REF}"

log "============================================================"
log "published ${COMMIT}"
log "  source : ${SOURCE_SHA} (${SOURCE_REF})"
log "  ref    : origin/${DIST_REF}"
log "  files  : ${FILE_COUNT}"
log "============================================================"
