#!/usr/bin/env bash
set -euo pipefail

# Get default branch using git symbolic-ref (more reliable than git remote show)
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')

if [[ -z "$DEFAULT_BRANCH" ]]; then
  echo "ERROR: Unable to determine default branch" >&2
  exit 1
fi

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

if [[ "$DEFAULT_BRANCH" == "$CURRENT_BRANCH" ]]; then
  echo "Error: Cannot commit to default branch: $DEFAULT_BRANCH" >&2
  exit 1
fi
