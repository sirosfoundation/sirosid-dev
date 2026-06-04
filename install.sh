#!/bin/bash
# install.sh — Bootstrap SIROS ID dev environment
# Usage: curl -fsSL <URL>/install.sh | sh
set -euo pipefail

REPOS=(
  sirosid-dev
  wallet-frontend
  wallet-backend
  go-wallet-backend
  go-trust
  wallet-common
  vc
)

# Default branches/tags
branch_for() {
  case "$1" in
    wallet-common|wallet-frontend) echo "release/sirosid" ;;
    *) echo "main" ;;
  esac
}

GITHUB_ORG="sirosfoundation"

# Clone or update a repo
clone_or_update() {
  local repo="$1"
  local branch
  branch=$(branch_for "$repo")
  if [ -d "$repo/.git" ]; then
    echo "Updating $repo..."
    git -C "$repo" fetch origin
    git -C "$repo" checkout "$branch"
    git -C "$repo" reset --hard "origin/$branch"
  else
    echo "Cloning $repo..."
    git clone "https://github.com/$GITHUB_ORG/$repo.git" --branch "$branch" --single-branch
  fi
}

# Main
for repo in "${REPOS[@]}"; do
  clone_or_update "$repo"
done

echo "\nAll repositories are ready."
echo "Next: cd sirosid-dev && make up"