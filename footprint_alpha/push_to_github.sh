#!/bin/bash
# One-command push of this clean repo to GitHub. Token is NEVER stored on disk.
#   GITHUB_TOKEN=ghp_xxxxx bash push_to_github.sh [repo_url] [branch]
REPO_URL="${1:-https://github.com/karan1399/Trading-Strategie.git}"
BRANCH="${2:-main}"
: "${GITHUB_TOKEN:?Set GITHUB_TOKEN env var (PAT with Contents: Read&Write on the repo)}"
AUTH_URL=$(python3 -c "import os,sys;u=sys.argv[1];t=os.environ['GITHUB_TOKEN'];print(u.replace('https://','https://x-access-token:'+t+'@'))" "$REPO_URL")
git push "$AUTH_URL" "HEAD:refs/heads/$BRANCH" --force-with-lease 2>&1 | sed -E "s/x-access-token:[^@]+@/***@/g"
rc=$?
git remote remove autotmp 2>/dev/null
exit $rc
