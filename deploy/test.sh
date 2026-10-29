#!/usr/bin/env bash
# Builds the app's image from this checkout and runs the backend tests inside it: the same Ubuntu, Python and ffmpeg
# the server runs them on. Building it also type-checks and builds the phone app.
# Usage: deploy/test.sh [name for the image, e.g. the commit]
set -euo pipefail
cd "$(dirname "$0")/.."
image="stream-clipper-ci:${1:-local}"
trap 'docker image rm -f "$image" >/dev/null 2>&1 || true' EXIT

docker build -f backend/Dockerfile -t "$image" .
# As root, to add pytest to the image's own Python just for this throwaway container.
docker run --rm --user root -v "$PWD/backend/tests:/app/tests:ro" --entrypoint sh "$image" \
  -c 'pip install -q "pytest>=8" && cd /app && python -m pytest -q -p no:cacheprovider tests'
