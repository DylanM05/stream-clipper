#!/usr/bin/env bash
# Security checks, each run with a scanner's image pinned to an exact version, so there's nothing to install.
# Usage: deploy/scan.sh <check>
#   secrets          secrets committed in the code or anywhere in its git history (gitleaks)
#   code             risky Python (bandit) and mistakes in the shell scripts (shellcheck)
#   vulnerabilities  known vulnerabilities that have a fix: in the app's Python packages as its image installs them,
#                    the phone app's packages and the upload browser's image; and risky Dockerfile settings (trivy)
# Each fails on what it finds. README.md ("Security checks") says how to accept a finding that doesn't apply.
set -euo pipefail
cd "$(dirname "$0")/.."

GITLEAKS=zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
SHELLCHECK=koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d
PYTHON=python:3.12-alpine@sha256:5789aa194f757888f41fa3d48e14b6feaa1b57c0af0b56fcb1372d6474d3bff0
BANDIT=bandit==1.9.4
# Not 0.69.4 to 0.69.6: an attacker published those in March 2026.
TRIVY=aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa

# Runs an image with this checkout as its working directory, /src. From a CI job in a container that uses the server's
# Docker, the checkout isn't on the server to mount, so it's copied in instead.
on_checkout() {
  if ! docker container inspect "$HOSTNAME" >/dev/null 2>&1; then
    docker run --rm -v "$PWD:/src:ro" -w /src "$@"
    return
  fi
  local box
  box=$(docker create --rm -w /src "$@")
  docker cp ./. "$box:/src" >/dev/null || { docker rm -f "$box" >/dev/null; return 1; }
  docker start -a "$box"
}

# trivy <command> <arguments>: fails on what's high or critical. Its vulnerability database is kept in a volume between
# runs and fetched again once a day. It's given Docker to read an image from.
trivy() {
  local command=$1
  shift
  on_checkout -v stream-clipper-trivy-cache:/cache -v /var/run/docker.sock:/var/run/docker.sock "$TRIVY" "$command" \
    --cache-dir /cache --quiet --disable-telemetry --severity HIGH,CRITICAL --exit-code 1 "$@"
}

check_secrets() {
  # The whole history, since a secret that was committed and later deleted is still in it. Redacted, as anyone who can
  # see the repository can read the CI log.
  on_checkout "$GITLEAKS" git --redact --no-banner --verbose .
}

check_code() {
  local failed=0
  echo "Python, at medium severity and confidence and up (bandit):"
  on_checkout -e PIP_ROOT_USER_ACTION=ignore -e PIP_DISABLE_PIP_VERSION_CHECK=1 "$PYTHON" \
    sh -c "pip install -q '$BANDIT' && bandit -q -r backend/clipper -ll -ii" || failed=1
  echo "Shell scripts, warnings and up (shellcheck):"
  on_checkout "$SHELLCHECK" --severity=warning deploy/*.sh browser/start.sh || failed=1
  return "$failed"
}

check_vulnerabilities() {
  local failed=0
  app=stream-clipper-scan:$$
  browser=stream-clipper-browser-scan:$$
  trap 'rm -rf .scan; docker image rm -f "$app" "$browser" >/dev/null 2>&1 || true' EXIT

  echo "The app's Python packages, as its image installs them:"
  docker build -q -f backend/Dockerfile -t "$app" . >/dev/null
  mkdir -p .scan/python
  docker run --rm --entrypoint /opt/venv/bin/pip "$app" freeze --all >.scan/python/requirements.txt
  trivy fs --scanners vuln --ignore-unfixed .scan/python || failed=1

  echo "The phone app's packages:"
  trivy fs --scanners vuln --ignore-unfixed frontend/package-lock.json || failed=1

  echo "The upload browser's image (Chromium and the system's packages):"
  # As a deploy builds it today: with the day's packages (browser/Dockerfile).
  docker build -q --build-arg "PACKAGES_DATE=$(date -u +%F)" -t "$browser" browser >/dev/null
  trivy image --scanners vuln --ignore-unfixed "$browser" || failed=1

  echo "The Dockerfiles' settings:"
  trivy config --skip-dirs '**/node_modules' --skip-dirs data . || failed=1
  return "$failed"
}

case ${1:-} in
  secrets) check_secrets ;;
  code) check_code ;;
  vulnerabilities) check_vulnerabilities ;;
  *) echo "Usage: deploy/scan.sh secrets|code|vulnerabilities" >&2; exit 2 ;;
esac
