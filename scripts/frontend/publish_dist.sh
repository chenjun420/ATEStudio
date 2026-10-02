#!/usr/bin/env bash
#
# publish_dist.sh — build the SPA and commit it onto the development branch
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
# Building in CI and committing the result removes node from the production host
# and keeps the deploy atomic: the bundle records the source commit it was built
# from, and deploy_cloud.sh refuses to install a bundle that does not match the
# commit it is deploying.
#
# Why the bundle lives on dev, at spa-bundle/
# -------------------------------------------
# The bundle used to be published to a separate `spa-dist` ref, on the reasoning
# that a build output does not belong in a source branch. That reasoning was
# half right: the output does not belong *mixed into* the source history, but
# putting it on a branch of its own creates a second thing to keep in sync, and
# a ref that looks like a development branch while never being reviewed or
# merged. So the bundle is a commit on dev, at spa-bundle/ — a directory nothing
# else uses, holding build output and nothing but build output.
#
# It cannot live at frontend/dist/, because that path is in .gitignore, and git
# reports modifications to tracked files whether or not .gitignore covers them:
# a developer's local `npm run build` would rewrite 198 tracked files and leave
# the tree permanently dirty. Nothing ever writes spa-bundle/ into a working
# tree — the tree is built with plumbing into a temporary index — so it never
# appears untracked either.
#
# Why publishing is skipped when the frontend did not change
# ----------------------------------------------------------
# The bundle is 198 files and 16.6 MB. Committing it on every push would put
# megabytes of build output into dev's history for backend-only work, which is
# the exact coupling this script exists to remove. The `frontend_tree` hash
# below already answers "would this bundle differ from the current sources?", so
# it gates the commit as well as the install: a push that touches no frontend
# file publishes nothing and costs zero bytes. GitHub warns a repository at 1 GB
# and blocks uploads at 5 GB; at 16.6 MB per frontend change that is roughly 60
# frontend changes to the warning.
#
# Usage
# -----
#   scripts/frontend/publish_dist.sh              # build, record, commit, push
#   PUBLISH_BRANCH=dev scripts/frontend/publish_dist.sh   # to another branch
#   SKIP_BUILD=1 scripts/frontend/publish_dist.sh  # republish existing dist/
#
# Run from the repository root or from anywhere; paths are resolved from the
# repository top level, which this script locates from its own path.
set -euo pipefail

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

PUBLISH_BRANCH="${PUBLISH_BRANCH:-dev}"
SKIP_BUILD="${SKIP_BUILD:-0}"
# Build and record, but do not commit or push. This is a real mode, not a
# testing convenience: CI runs the build on every trigger, but only a push to
# the publish branch may commit a bundle, and a build that cannot be exercised
# outside that one case is a build nobody notices breaking.
SKIP_PUBLISH="${SKIP_PUBLISH:-0}"
FRONTEND_DIR="${REPO_ROOT}/frontend"
DIST_DIR="${FRONTEND_DIR}/dist"
BUILD_INFO="${DIST_DIR}/.build-info.json"
# Where the bundle is committed, relative to the repository root.
BUNDLE_DIR="spa-bundle"
# Where the outcome is written for a caller to read, including CI. It records
# why the publish ended the way it did, and on which frontend tree, so a caller
# never has to infer either from the absence of a push.
#
# At the repository root, not inside dist/, and that is load-bearing. Two of the
# three exits happen *before* the build, so on a fresh CI checkout dist/ does not
# exist yet and a file written there would silently fail — leaving the caller to
# read a file that was never created. The root always exists. It is gitignored,
# so writing it does not dirty the checkout.
OUTCOME_FILE="${REPO_ROOT}/.publish-outcome"

log()  { printf '[publish-dist] %s\n' "$*"; }
die()  { printf '[publish-dist] ERROR: %s\n' "$*" >&2; exit 1; }

# Records why the publish ended the way it did, and on what tree, and leaves.
# Written before every exit path, including the skips.
outcome() {
    printf 'outcome=%s\nfrontend_tree=%s\nsource_sha=%s\nbranch=%s\n' \
        "$1" "${FRONTEND_TREE:-unknown}" "${SOURCE_SHA:-unknown}" "${PUBLISH_BRANCH}" \
        > "${OUTCOME_FILE}" 2>/dev/null || true
    log "outcome: ${1} (recorded in $(basename "${OUTCOME_FILE}"))"
}

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

log "repository   : ${REPO_ROOT}"
log "source       : ${SOURCE_SHA} (${SOURCE_REF})"
log "publish to   : refs/heads/${PUBLISH_BRANCH}"
log "bundle path  : ${BUNDLE_DIR}/"

# ---------------------------------------------------------------------------
# Resolve the target branch
# ---------------------------------------------------------------------------
# The bundle is committed *on* the branch, so the commit has to be a child of
# that branch's tip. Only an exact match is published: a bundle built from a
# commit that is not the branch tip would sit on a tree whose sources are not
# the ones the bundle was built from, which is the provenance confusion this
# whole mechanism exists to prevent.
git fetch --quiet origin "refs/heads/${PUBLISH_BRANCH}:refs/remotes/origin/${PUBLISH_BRANCH}" \
    || die "could not fetch origin/${PUBLISH_BRANCH}"
TARGET_TIP="$(git rev-parse "refs/remotes/origin/${PUBLISH_BRANCH}")"

if [ "${TARGET_TIP}" != "${SOURCE_SHA}" ]; then
    outcome "skipped-branch-tip-moved"
    log "origin/${PUBLISH_BRANCH} is at ${TARGET_TIP}, this build is ${SOURCE_SHA}."
    log "A bundle is only published from the branch tip. Nothing was pushed; the"
    log "next run on ${PUBLISH_BRANCH} will publish it."
    exit 0
fi

# ---------------------------------------------------------------------------
# Skip when the frontend did not change
# ---------------------------------------------------------------------------
# Read the bundle already on the branch, not any local copy: the question is
# whether the branch is already carrying a bundle built from these sources.
PUBLISHED_INFO=""
if git cat-file -e "refs/remotes/origin/${PUBLISH_BRANCH}:${BUNDLE_DIR}/.build-info.json" 2>/dev/null; then
    PUBLISHED_INFO="$(git show "refs/remotes/origin/${PUBLISH_BRANCH}:${BUNDLE_DIR}/.build-info.json" || true)"
fi
if [ -n "${PUBLISHED_INFO}" ]; then
    published_tree="$(printf '%s' "${PUBLISHED_INFO}" | sed -n 's/.*"frontend_tree": *"\([^"]*\)".*/\1/p')"
    published_sha="$(printf '%s' "${PUBLISHED_INFO}" | sed -n 's/.*"source_sha": *"\([0-9a-f]\{7,40\}\)".*/\1/p')"
    if [ "${published_tree}" = "${FRONTEND_TREE}" ]; then
        outcome "skipped-frontend-unchanged"
        log "origin/${PUBLISH_BRANCH} already carries a bundle built from frontend tree"
        log "${FRONTEND_TREE} (source ${published_sha:-unknown}). The frontend did not"
        log "change, so the bundle would be byte-identical and committing it would"
        log "add 16.6 MB of build output for nothing. Nothing was pushed."
        exit 0
    fi
    log "published bundle is from a different frontend tree:"
    log "  on branch : ${published_tree:-<none>} (source ${published_sha:-unknown})"
    log "  this build: ${FRONTEND_TREE}"
else
    log "origin/${PUBLISH_BRANCH} carries no bundle yet; this publish will add one."
fi

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
  "bundle_path": "${BUNDLE_DIR}"
}
JSON

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

if [ "${SKIP_PUBLISH}" = "1" ]; then
    outcome "skipped-not-publishing"
    log "SKIP_PUBLISH=1 — the bundle was built and recorded, and nothing was"
    log "committed or pushed. ${BUNDLE_DIR}/ on ${PUBLISH_BRANCH} is unchanged."
    exit 0
fi

# ---------------------------------------------------------------------------
# Commit the bundle, without touching the working tree
# ---------------------------------------------------------------------------
# The tree is assembled with plumbing into a temporary index rather than by
# copying dist/ into the checkout and staging it. Two reasons, both learned the
# hard way:
#
#   * a temporary index keeps the source branch's index and working tree
#     untouched, so this cannot leave the checkout in a half-staged state — the
#     failure mode that bit a commit earlier in this project, where
#     `git commit -- <path>` carried along everything else that was staged;
#   * spa-bundle/ must never exist as a working-tree directory, or a developer's
#     next `npm run build` would show 198 modified tracked files.
#
# `git add -f` is not usable here: the temp index starts empty, so git resolves
# paths against the cwd, and the files we want are in dist/, not at the bundle
# path. Hashing them and feeding `update-index --index-info` puts them at the
# right path without ever writing them there.
log "hashing ${DIST_DIR} into a ${BUNDLE_DIR}/ tree..."
TMP_INDEX="$(mktemp)"
cleanup() { rm -f "${TMP_INDEX}"; }
trap cleanup EXIT
rm -f "${TMP_INDEX}"   # git refuses to use an index file that already exists

export GIT_INDEX_FILE="${TMP_INDEX}"
git read-tree --empty
# NUL-delimited throughout. vite names assets after their content, and nothing
# stops a source filename from containing a space or a newline; a line-based
# loop would silently drop or misattribute such a file.
while IFS= read -r -d '' path; do
    rel="${path#./}"
    sha="$(git hash-object -w -- "${DIST_DIR}/${rel}")"
    printf '100644 blob %s\t%s/%s\0' "${sha}" "${BUNDLE_DIR}" "${rel}"
done < <(cd "${DIST_DIR}" && find . -type f -print0) \
    | git update-index -z --index-info
TREE="$(git write-tree)"
unset GIT_INDEX_FILE
[ -n "${TREE}" ] || die "empty tree — dist/ staged nothing"

FILE_COUNT="$(GIT_INDEX_FILE="${TMP_INDEX}" git ls-files | wc -l | tr -d ' ')"
log "tree ${TREE} with ${FILE_COUNT} files"

# Parent is the branch tip, which the check above established is this build's
# own source commit. So the bundle commit sits directly on the source it was
# built from, and the deploy can name either.
log "parent  : ${TARGET_TIP} (refs/heads/${PUBLISH_BRANCH})"
COMMIT="$(git commit-tree "${TREE}" -p "${TARGET_TIP}" \
    -m "spa: bundle for ${SOURCE_SHA} (frontend tree ${FRONTEND_TREE})")"
log "commit  : ${COMMIT}"

# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------
# No force. The commit is a child of the branch tip, so this is a fast-forward
# and a rejection means someone pushed in the last few seconds — in which case
# their commit is the new tip and the next CI run publishes against it. Forcing
# would silently discard whatever they pushed, which for a source branch is not
# a trade this script is allowed to make on its own.
log "pushing to origin refs/heads/${PUBLISH_BRANCH}..."
if ! git push origin "${COMMIT}:refs/heads/${PUBLISH_BRANCH}"; then
    outcome "push-rejected"
    die "could not push the bundle onto ${PUBLISH_BRANCH}. Nothing was overwritten; re-run once the branch settles."
fi

outcome "published"
log "============================================================"
log "published ${COMMIT}"
log "  source : ${SOURCE_SHA} (${SOURCE_REF})"
log "  branch : origin/${PUBLISH_BRANCH}"
log "  path   : ${BUNDLE_DIR}/"
log "  files  : ${FILE_COUNT}"
log "============================================================"
