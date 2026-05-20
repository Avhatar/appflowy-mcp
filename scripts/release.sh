#!/usr/bin/env bash
# Build and push appflowy-mcp to GHCR using the version from pyproject.toml.
#
# Prereqs:
#   - Bumped `version` in pyproject.toml (and updated CHANGELOG.md)
#   - `docker login ghcr.io` already done (PAT with write:packages scope)
#
# Usage: scripts/release.sh
#
# Override the registry by setting REGISTRY=ghcr.io/your-org (defaults to
# ghcr.io/avhatar).

set -euo pipefail

cd "$(dirname "$0")/.."

REGISTRY="${REGISTRY:-ghcr.io/avhatar}"
IMAGE_NAME="appflowy-mcp"

VERSION=$(sed -nE 's/^version = "([^"]+)"/\1/p' pyproject.toml | head -n1)
if [[ -z "$VERSION" ]]; then
  echo "ERROR: could not read version from pyproject.toml" >&2
  exit 1
fi

FULL_TAG="$REGISTRY/$IMAGE_NAME:$VERSION"
echo "Building $FULL_TAG ..."
docker build -t "$FULL_TAG" .

echo "Pushing $FULL_TAG ..."
docker push "$FULL_TAG"

DIGEST_LINE=$(docker inspect "$FULL_TAG" --format '{{index .RepoDigests 0}}')
echo
echo "Pushed. Paste this into appflowy-deploy/docker-compose.yml under appflowy_mcp:"
echo
echo "    image: $DIGEST_LINE"
