#!/usr/bin/env bash
# Safely commit & push generated data files from CI.
#
# Replaces the fragile `git pull --rebase --autostash || true` pattern that
# could silently commit conflict markers when an auto-merge failed (it once
# corrupted 5 data files this way). Instead we:
#   1. commit our generated files first,
#   2. merge the latest origin preferring OUR generated files on conflict,
#   3. HARD-ABORT if any conflict marker is present (never push corruption),
#   4. push (with a small retry loop for transient races).
#
# Usage:
#   batch/safe_commit_push.sh "<commit message>" <file> [<file> ...]
set -euo pipefail

MSG="${1:?commit message required}"; shift
if [ "$#" -eq 0 ]; then
  echo "[safe_commit] no files specified"; exit 1
fi

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add -- "$@"
if git diff --cached --quiet; then
  echo "[safe_commit] nothing to commit"; exit 0
fi
git commit -m "$MSG"

# Merge latest origin, preferring our freshly generated files on any conflict.
# (Concurrency group serializes CI runs; -X ours only matters for human-vs-CI
#  races, where the generated data should win.)
pushed=false
for attempt in 1 2 3 4 5; do
  git fetch origin main || true
  if git merge --no-edit -X ours origin/main; then
    # Belt-and-suspenders: refuse to push if any marker slipped through.
    if grep -rIl --include='*.json' -e '^<<<<<<< ' -e '^>>>>>>> ' -e '^=======$' web/data data 2>/dev/null; then
      echo "[safe_commit] ERROR: conflict markers detected — aborting push"; exit 1
    fi
    if git push origin HEAD:main; then
      pushed=true; break
    fi
  fi
  echo "[safe_commit] attempt $attempt failed (likely a concurrent push) — retrying in 5s"
  sleep 5
done

if [ "$pushed" != true ]; then
  echo "[safe_commit] ERROR: could not push after retries"; exit 1
fi
echo "[safe_commit] pushed successfully"
