#!/usr/bin/env bash
# npm-release.sh — publish @yya007/skill-finder to npmjs.com
#
# Usage:
#   ./scripts/npm-release.sh patch    # 1.0.0 → 1.0.1
#   ./scripts/npm-release.sh minor    # 1.0.0 → 1.1.0
#   ./scripts/npm-release.sh major    # 1.0.0 → 2.0.0
#   ./scripts/npm-release.sh         # no version bump, just dry-run
#
# Prerequisites:
#   npm login   (one-time: authenticates with npmjs.com as yya007)

set -euo pipefail

BUMP="${1:-}"
PKG="$(node -p "require('./package.json').name")"
CURRENT="$(node -p "require('./package.json').version")"

echo "Package : $PKG"
echo "Current : v$CURRENT"

# ── 1. Verify logged in ───────────────────────────────────────────────────────
if ! npm whoami &>/dev/null; then
  echo ""
  echo "Not logged in to npm. Run:  npm login"
  exit 1
fi
NPM_USER="$(npm whoami)"
echo "npm user: $NPM_USER"

# ── 2. Dry run — show what would be published ────────────────────────────────
echo ""
echo "=== Dry run (files that would be published) ==="
npm publish --dry-run 2>&1 | grep -E '^\bnpm\b|Tarball|Files|Total' || true
echo ""

if [[ -z "$BUMP" ]]; then
  echo "Dry-run only (no version bump argument given). Exiting."
  exit 0
fi

# ── 3. Bump version ───────────────────────────────────────────────────────────
if [[ "$BUMP" =~ ^(patch|minor|major)$ ]]; then
  NEW="$(npm version "$BUMP" --no-git-tag-version)"
  echo "Bumped  : $CURRENT → ${NEW#v}"
else
  echo "Unknown bump type '$BUMP'. Use: patch | minor | major"
  exit 1
fi

# ── 4. Publish ────────────────────────────────────────────────────────────────
echo ""
echo "Publishing $PKG@${NEW#v} ..."
npm publish --access public

# ── 5. Git tag ────────────────────────────────────────────────────────────────
echo ""
read -r -p "Create git tag $NEW and push? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
  git add package.json
  git commit -m "chore: bump npm version to ${NEW#v}"
  git tag "$NEW"
  git push && git push --tags
  echo "Tag $NEW pushed."
fi

echo ""
echo "Done. View on npm: https://www.npmjs.com/package/$PKG"
