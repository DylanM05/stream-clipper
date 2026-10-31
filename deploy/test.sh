#!/usr/bin/env bash
# Builds the app's image from this checkout and runs the backend tests inside it: the same Ubuntu, Python and ffmpeg
# the server runs them on. Building it also type-checks and builds the phone app.
# Usage: deploy/test.sh [name for the image, e.g. the commit]
set -euo pipefail
cd "$(dirname "$0")/.."
image="stream-clipper-ci:${1:-local}"
box=""
trap 'docker rm -f "$box" >/dev/null 2>&1; docker image rm -f "$image" >/dev/null 2>&1 || true' EXIT

docker build -f backend/Dockerfile -t "$image" .
# As root, to add pytest to the image's own Python just for this throwaway container. The tests are copied in rather
# than mounted: from a CI job in a container that uses the server's Docker, a mount would look for them on the server.
box=$(docker create --user root --entrypoint sh "$image" \
  -c 'pip install -q "pytest>=8" && cd /app && python -m pytest -q -p no:cacheprovider tests')
docker cp backend/tests "$box:/app/tests"
docker start -a "$box"
