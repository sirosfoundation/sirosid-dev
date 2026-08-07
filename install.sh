#!/bin/bash
# install.sh — Bootstrap SIROS ID dev environment
# Usage: curl -fsSL <URL>/install.sh | bash
set -euo pipefail

REPOS=(
  sirosid-dev
  wallet-frontend
  go-wallet-backend
  go-trust
  wallet-common
  vc
  facetec-api
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

# helm-charts: config-rendering source for PDP=helm / make fly-up. Not
# branched for feature work, so it gets gentler handling than the repos
# above: clone if missing, fast-forward if already on main, otherwise leave
# it alone (it may be deliberately checked out to a PR branch under test).
setup_helm_charts() {
  local dir="helm-charts"
  if [ -d "$dir/.git" ]; then
    local branch
    branch=$(git -C "$dir" branch --show-current)
    if [ "$branch" = "main" ]; then
      echo "Updating $dir..."
      git -C "$dir" fetch origin
      git -C "$dir" pull --ff-only
    else
      echo "$dir exists on branch '$branch' (not main) — leaving as-is."
    fi
  else
    echo "Cloning $dir..."
    git clone "https://github.com/$GITHUB_ORG/$dir.git" --branch main --single-branch
  fi
}

# Main
for repo in "${REPOS[@]}"; do
  clone_or_update "$repo"
done
setup_helm_charts

echo "\nAll repositories are ready."
echo "Next: cd sirosid-dev && make up"