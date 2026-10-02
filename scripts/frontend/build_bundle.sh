#!/usr/bin/env bash
#
# build_bundle.sh — build the SPA on this machine and package it for transfer
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
# So the build moved off the production host. Where it should live took three
# attempts, and the reasoning is worth keeping because each wrong turn looked
# reasonable:
#
#   1. a separate `refs/heads/spa-dist` branch. Build output in a source
#      repository is wrong, but so is a branch that is never reviewed or merged
#      and looks like a development branch;
#   2. a ref outside refs/heads/, so it stops appearing in the branch list. That
#      fixes the appearance and none of the substance: a force-pushed ref keeps
#      its old objects, so the repository still grows by the size of every build
#      ever published;
#   3. committing the bundle onto dev. Same growth, plus 198 files of hashed JS
#      in every source commit.
#
# All three are the same mistake in different costumes: treating the git object
# store as an artifact store. A repository that grows by 16.6 MB every time
# someone touches a stylesheet is a repository that eventually hits GitHub's
# 1 GB warning and 5 GB hard block, and there is no way back from that short of
# rewriting history.
#
# What this script does instead is the obvious thing: build the bundle here,
# where node already is, and let deploy_cloud.sh copy the tarball over the SSH
# connection it already opens. Nothing is published. The artifact never enters
# the repository, the production host never runs node, and neither side needs a
# GitHub credential to move a bundle around.
#
# What is preserved
# -----------------
# Provenance and staleness detection, which were the reason for moving the build
# at all. The bundle carries a `.build-info.json` recording the source commit, a
# hash of the frontend *sources*, who built it and when. deploy_cloud.sh refuses
# to install a bundle whose frontend sources differ from the code it is
# deploying, so the interface and the API can never silently disagree.
#
# Usage
# -----
#   scripts/frontend/build_bundle.sh                    # build and package
#   SKIP_BUILD=1 scripts/frontend/build_bundle.sh       # repackage existing dist/
#   OUTPUT_DIR=/tmp/x scripts/frontend/build_bundle.sh  # package elsewhere
#
# Run from anywhere; paths resolve from the repository top level, located from
# this script's own path.
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

SKIP_BUILD="${SKIP_BUILD:-0}"
FRONTEND_DIR="${REPO_ROOT}/frontend"
DIST_DIR="${FRONTEND_DIR}/dist"
BUILD_INFO="${DIST_DIR}/.build-info.json"
# The tarball lands outside the repository's tracked paths and inside a
# directory .gitignore covers, so packaging can never leave a dirty tree.
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/.spa-bundle}"
TARBALL="${OUTPUT_DIR}/spa-bundle.tar.gz"

log()  { printf '[build-bundle] %s\n' "$*"; }
die()  { printf '[build-bundle] ERROR: %s\n' "$*" >&2; exit 1; }

cd "${REPO_ROOT}"
command -v git >/dev/null 2>&1 || die "git not found"
git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository (${REPO_ROOT})"

# The source commit is recorded in the bundle. This is what makes the
# deploy-time assertion meaningful.
SOURCE_SHA="$(git rev-parse HEAD)"
SOURCE_REF="$(git rev-parse --abbrev-ref HEAD)"
[ "${SOURCE_REF}" = "HEAD" ] && SOURCE_REF="(detached)"

# ---------------------------------------------------------------------------
# Hash the frontend sources
#
# Not the commit. The deploy originally asserted source_sha == the deployed
# commit. That is safe but wrong in practice: any backend-only commit changes
# the SHA while leaving the bundle byte-identical, so every backend deploy would
# have to wait for a fresh build. Hashing the inputs the build actually reads
# answers the real question — "would this bundle differ from the current
# sources?" — so a stale bundle is rejected exactly when a frontend file
# changed.
#
# Must produce the same value as frontend_tree_filter in
# scripts/deploy/deploy_cloud.sh over the same paths. That script's copy carries
# a comment saying why: if the two sides hash different file sets the comparison
# can never succeed, and it would be reported as a stale bundle rather than as
# the mismatch it is.
# ---------------------------------------------------------------------------
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
# building with uncommitted frontend edits would attach one commit's provenance
# to another commit's bytes. Only frontend inputs are checked: an unrelated dirty
# file does not affect the bundle and must not block a build.
dirty_frontend="$(git status --porcelain -- "${FRONTEND_INPUTS[@]}" ':!frontend/dist' ':!frontend/node_modules' || true)"
if [ -n "${dirty_frontend}" ]; then
    printf '%s\n' "${dirty_frontend}" >&2
    die "frontend sources have uncommitted changes, so the bundle built from the working tree would not match the provenance recorded in it.
Commit them, or build from a clean checkout."
fi

log "repository : ${REPO_ROOT}"
log "source     : ${SOURCE_SHA} (${SOURCE_REF})"
log "output     : ${TARBALL}"

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
# The deploy refuses to install a bundle whose frontend_tree differs from the
# sources it is deploying. Without this file a stale bundle installs cleanly and
# the interface quietly stops matching the API.
#
# No secrets: a commit SHA, a tree hash, a timestamp, and tool versions. This
# file is served publicly by the SPA catch-all route, so it must stay boring.
NODE_VERSION="unknown"
command -v node >/dev/null 2>&1 && NODE_VERSION="$(node --version)"

# `built_by` is provenance that decides whether a bundle is trustworthy, so it
# must not be assembled by string interpolation. "${GITHUB_ACTIONS:+...}${GITHUB_ACTIONS:-local}"
# yields "github-actionstrue" when the variable is set to "true", because the
# :+ form substitutes its own operand and ignores the value. That shipped in the
# first smoke run and recorded a false claim about provenance. Assigned in
# branches instead.
#
# A bundle built here is built here. There is no CI path for this any more, and
# `built_by` says so rather than implying a provenance that does not exist.
BUILT_BY="local"

log "writing $(basename "${BUILD_INFO}")..."
cat > "${BUILD_INFO}" <<JSON
{
  "source_sha": "${SOURCE_SHA}",
  "source_ref": "${SOURCE_REF}",
  "frontend_tree": "${FRONTEND_TREE}",
  "built_at": "$(date -Iseconds)",
  "built_by": "${BUILT_BY}",
  "node_version": "${NODE_VERSION}"
}
JSON

# The recorded provenance has to describe what was actually built, or the
# deploy-time assertion is checking a self-consistent lie. A build that read a
# different tree — a stale checkout, or a dirty worktree in a way that mattered
# — would otherwise ship a bundle whose provenance is wrong.
RECORDED="$(sed -n 's/.*"source_sha": *"\([0-9a-f]\{7,40\}\)".*/\1/p' "${BUILD_INFO}")"
[ "${RECORDED}" = "${SOURCE_SHA}" ] \
  || die "build-info records ${RECORDED} but the source is ${SOURCE_SHA} — refusing to package a bundle that misreports its origin"

RECORDED_TREE="$(sed -n 's/.*"frontend_tree": *"\([^"]*\)".*/\1/p' "${BUILD_INFO}")"
[ "${RECORDED_TREE}" = "${FRONTEND_TREE}" ] \
  || die "build-info records frontend_tree ${RECORDED_TREE} but the sources hash to ${FRONTEND_TREE} — refusing to package a bundle that misreports what it was built from"

RECORDED_BY="$(sed -n 's/.*"built_by": *"\([^"]*\)".*/\1/p' "${BUILD_INFO}")"
[ "${RECORDED_BY}" = "${BUILT_BY}" ] \
  || die "build-info records built_by=${RECORDED_BY} but this run is ${BUILT_BY} — refusing to package a bundle that misreports who built it"

# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------
# The archive holds the contents of dist/ at its root, so the deploy extracts it
# straight into frontend/dist with no path rewriting. A mismatch between what
# this packs and what the deploy expects is the failure mode that once reported
# "installed 0 files" while quietly littering the checkout.
mkdir -p "${OUTPUT_DIR}"
log "packaging ${DIST_DIR} -> ${TARBALL}..."
tar -czf "${TARBALL}" -C "${DIST_DIR}" .

n_files="$(find "${DIST_DIR}" -type f | wc -l | tr -d ' ')"
[ "${n_files}" -ge 100 ] \
  || die "only ${n_files} files in ${DIST_DIR}; a real bundle has ~198. A truncated or partial build is not worth shipping."

# A checksum travels with the archive so the receiving side can verify it with
# the same tool it already trusts, rather than comparing against a value this
# script printed and the deploy recomputed differently. sha256sum -c is the
# form; the tools below all produce it.
#
# The lookup is a chain rather than a hard dependency because this runs on
# whatever machine is deploying: Linux and Git Bash have sha256sum, macOS has
# shasum, and a bare Windows box has neither but always has certutil.
if command -v sha256sum >/dev/null 2>&1; then
    ( cd "${OUTPUT_DIR}" && sha256sum spa-bundle.tar.gz > spa-bundle.tar.gz.sha256 )
elif command -v shasum >/dev/null 2>&1; then
    ( cd "${OUTPUT_DIR}" && shasum -a 256 spa-bundle.tar.gz > spa-bundle.tar.gz.sha256 )
elif command -v openssl >/dev/null 2>&1; then
    sum="$(openssl dgst -sha256 "${TARBALL}" | sed 's/.*= *//')"
    printf '%s  %s\n' "${sum}" "spa-bundle.tar.gz" > "${TARBALL}.sha256"
elif command -v certutil >/dev/null 2>&1; then
    sum="$(certutil -hashfile "${TARBALL}" SHA256 | tr -d '\r' | grep -iE '^[0-9a-f]{64}$')"
    [ -n "${sum}" ] || die "certutil did not return a sha256"
    printf '%s  %s\n' "${sum}" "spa-bundle.tar.gz" > "${TARBALL}.sha256"
else
    die "no sha256 tool found (looked for sha256sum, shasum, openssl, certutil)
The deploy verifies the archive before unpacking it; it will not do so blind."
fi

CHECKSUM="$(awk '{print $1}' "${TARBALL}.sha256")"
[ -n "${CHECKSUM}" ] || die "checksum file is empty"

# The archive must contain the provenance file, or the deploy has nothing to
# check staleness against and would have to install it unverified.
# `grep -q` cannot be used to test the listing. It exits on the first match and
# closes the pipe, tar dies of SIGPIPE, and under `set -o pipefail` the pipeline
# reports 141 — a failure for a check that succeeded. Counting consumes all the
# input and returns a value rather than a signal.
n_info="$(tar -tzf "${TARBALL}" | grep -c 'build-info\.json' || true)"
case "${n_info}" in
    ''|*[!0-9]*) die "could not read the archive listing" ;;
esac
[ "${n_info}" -ge 1 ] \
  || die "the archive has no .build-info.json — the deploy could not verify it"

log "============================================================"
log "built   ${TARBALL}"
log "  source    : ${SOURCE_SHA} (${SOURCE_REF})"
log "  tree      : ${FRONTEND_TREE}"
log "  built_by  : ${BUILT_BY}"
log "  files     : ${n_files}"
log "  sha256    : ${CHECKSUM}"
log "============================================================"