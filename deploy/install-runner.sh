#!/usr/bin/env bash
# Sets up the Forgejo runner that tests and deploys this repository on this server (README: Automatic deploys).
# Run it from the folder the stack runs from, with the runner's UUID and token from Forgejo (the repository's
# Settings → Actions → Runners → Create new runner):
#   sudo deploy/install-runner.sh <uuid> <token>
# It runs jobs straight on this server as the folder's owner, who must be in the docker group.
set -euo pipefail
version=13.2.0
uuid=${1:?Give the runner UUID from Forgejo}
token=${2:?Give the runner token from Forgejo}
[ "$(id -u)" = 0 ] || { echo "Run it with sudo." >&2; exit 1; }

dir=$(cd "$(dirname "$0")/.." && pwd)
user=${RUNNER_USER:-$(stat -c %U "$dir")}
if ! id -nG "$user" | tr ' ' '\n' | grep -qx docker; then
  echo "$user isn't in the docker group, so the runner couldn't build or restart the stack. Add them: sudo usermod -aG docker $user" >&2
  exit 1
fi
remote=$(sudo -u "$user" git -C "$dir" remote get-url origin)
url=${FORGEJO_URL:-$(printf '%s' "$remote" | sed -E 's#^(https?://[^/]+).*#\1/#')}
case "$url" in http*) ;; *) echo "Couldn't tell Forgejo's address from the remote ($remote); give it as FORGEJO_URL=..." >&2; exit 1 ;; esac

arch=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
download="https://code.forgejo.org/forgejo/runner/releases/download/v$version/forgejo-runner-$version-linux-$arch"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL -o "$tmp/forgejo-runner" "$download"
expected=$(curl -fsSL "$download.sha256" | cut -d' ' -f1)
echo "$expected  $tmp/forgejo-runner" | sha256sum -c --quiet -
install -m 755 "$tmp/forgejo-runner" /usr/local/bin/forgejo-runner

home=/var/lib/clipper-runner
install -d -o "$user" -m 700 "$home"
umask 077
cat > "$home/config.yml" <<CONFIG
log:
  level: info
runner:
  file: $home/.runner
  capacity: 1  # one job at a time
  timeout: 3h
  envs:
    CLIPPER_DEPLOY_DIR: $dir
  # Jobs that ask for "clipper-server" run straight on this server, as $user: no container.
  labels: ["clipper-server:host"]
server:
  connections:
    forgejo:
      url: $url
      uuid: $uuid
      token: $token
CONFIG
chown "$user" "$home/config.yml"

cat > /etc/systemd/system/clipper-runner.service <<UNIT
[Unit]
Description=Forgejo runner that tests and deploys Stream Clipper
After=docker.service network-online.target
Wants=network-online.target

[Service]
User=$user
WorkingDirectory=$home
ExecStart=/usr/local/bin/forgejo-runner daemon -c $home/config.yml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now clipper-runner.service
echo "The runner is running as $user, for $url, deploying $dir. It should show as online in Forgejo now."
