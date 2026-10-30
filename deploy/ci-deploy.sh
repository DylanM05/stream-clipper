#!/usr/bin/env bash
# Runs deploy.sh from a CI job that runs in a container using the server's Docker, as a Forgejo runner's ubuntu-latest
# jobs do. The folder the stack runs from isn't in the job's container, so deploy.sh runs in another container that has
# it, at the same path, so Docker Compose finds the stack's files and data where they are on the server. That container
# runs as the folder's owner, so what git writes there stays theirs, and on the server's network, so the deploy can
# check that the app answers.
# Usage: deploy/ci-deploy.sh <folder the stack runs from, on the server> <commit>
set -euo pipefail
dir=${1:-}
target=${2:?Give the commit to deploy}
if [[ $dir != /* ]]; then
  echo "Set the repository's CLIPPER_DEPLOY_DIR variable (Settings → Actions → Variables) to the full path of the" \
    "folder the stack runs from on the server." >&2
  exit 1
fi

# The job's own container. Its image is on the server already and has git, curl and Docker's command-line tools.
job=$HOSTNAME
if ! image=$(docker container inspect --format '{{.Image}}' "$job" 2>/dev/null); then
  echo "Run this from a CI job in a container that uses the server's Docker (it looks for its own container, $job)." >&2
  exit 1
fi
# --mount rather than -v, so a folder that isn't there is an error instead of being created empty.
folder=(--mount "type=bind,src=$dir,dst=$dir")
owner=$(docker run --rm "${folder[@]}" --entrypoint stat "$image" -c %u:%g "$dir")

# This deploy.sh, the one from the commit being deployed, runs in the job's process namespace, so cancelling the job
# stops the deploy too.
docker run --rm --pid "container:$job" --network host --user "$owner" \
  --group-add "$(stat -c %g /var/run/docker.sock)" -e HOME=/tmp \
  --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock "${folder[@]}" -w "$dir" \
  --entrypoint bash "$image" -c "$(cat "$(dirname "$0")/deploy.sh")" deploy.sh . "$target"
