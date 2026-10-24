#!/bin/sh
# Virtual screen + window manager + VNC (local only) + noVNC on port 6080 + Chromium, restarted if it closes.
set -e

# A restarted container keeps its /tmp, and Xvfb refuses to start while the last run's lock is there.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
xvfb=$!
sleep 1
fluxbox >/dev/null 2>&1 &

if [ -n "$VNC_PASSWORD" ]; then
  x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
  auth="-rfbauth /tmp/vncpass"
else
  auth="-nopw"
fi
x11vnc -display :99 -forever -shared -localhost -rfbport 5900 -quiet $auth &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

while kill -0 "$xvfb" 2>/dev/null; do
  rm -f /profile/Singleton*  # left behind when the container stopped with Chromium running
  # --no-sandbox: Docker's default seccomp profile blocks Chromium's sandbox. It only visits YouTube.
  chromium --no-first-run --no-default-browser-check --no-sandbox --disable-dev-shm-usage \
    --user-data-dir=/profile --remote-debugging-port=9222 --remote-allow-origins='*' \
    --window-position=0,0 --window-size=1440,900 https://studio.youtube.com || true
  sleep 3
done

# Without the screen Chromium can't start; exit so Docker restarts the container.
echo "Xvfb stopped" >&2
exit 1
