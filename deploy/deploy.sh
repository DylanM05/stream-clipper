#!/usr/bin/env bash
# Deploys a commit to this server: brings the checkout the stack runs from up to it, rebuilds, restarts the stack
# between uploads through the upload browser (those can't carry on after a restart), checks the app answers, and goes
# back to the commit it had if it doesn't.
# Usage: deploy/deploy.sh <folder the stack runs from> [commit, or origin/main by default]
set -euo pipefail
dir=$(cd "${1:?Give the folder the stack runs from (the checkout with .env in it)}" && pwd)
target=${2:-origin/main}
wait_hours=${CLIPPER_DEPLOY_WAIT_HOURS:-2.5}  # under Forgejo's 3-hour limit for a job
cd "$dir"

exec 9>"$dir/.deploy.lock"
flock 9  # one deploy at a time

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "The checkout in $dir has changes of its own; not deploying over them. Commit or undo them first." >&2
  exit 1
fi
previous=$(git rev-parse HEAD)
git fetch -q origin
git merge -q --ff-only "$target"
echo "Deploying $(git log -1 --format='%h %s') (was $(git rev-parse --short "$previous"))"

old_images=$(docker compose images -q 2>/dev/null | sort -u || true)
docker compose build

hold_released=0
release() { [ "$hold_released" = 1 ] || docker compose run --rm --no-deps -T web deploy-done >/dev/null 2>&1 || true; hold_released=1; }
trap release EXIT
# With the new image, which has the command. It holds new uploads until the restart, and waits for one going up
# through the upload browser to finish.
if ! docker compose run --rm --no-deps -T web deploy-wait --max-hours "$wait_hours"; then
  echo "Not restarting under an upload that's still going. Run the deploy again once it's done." >&2
  git reset -q --hard "$previous"
  docker compose build -q  # so the images match what's running again
  exit 1
fi

docker compose up -d --remove-orphans

healthy() {
  local address
  address=$(docker compose port web 8080 2>/dev/null | tail -1) || return 1
  for _ in $(seq 1 "${CLIPPER_DEPLOY_HEALTH_TRIES:-45}"); do  # about 90 s
    if curl -fsS -m 5 "http://${address/0.0.0.0/127.0.0.1}/api/session" >/dev/null 2>&1 \
       && docker compose ps --status running --services | grep -qx worker; then
      return 0
    fi
    sleep 2
  done
  return 1
}

if ! healthy; then
  echo "The app didn't come up. Its last lines:" >&2
  docker compose logs --tail 60 web worker >&2 || true
  echo "Going back to $(git rev-parse --short "$previous")" >&2
  git reset -q --hard "$previous"
  docker compose build -q
  docker compose up -d --remove-orphans
  exit 1
fi
release
echo "Deployed $(git rev-parse --short HEAD)"

# The images this replaced, now that nothing uses them (an image still in use is left alone).
for image in $old_images; do
  docker image rm "$image" >/dev/null 2>&1 || true
done
