# The browser that YouTube uploads go through (backend/clipper/studio.py).
#
# Chromium runs on a virtual screen that you can see and use from any browser through noVNC (port
# 6080), to sign in to YouTube once; the sign-in is kept in the /profile volume. The worker drives it
# through Chromium's remote debugging port, which only it can reach: it shares this container's network.
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium xvfb x11vnc novnc websockify fluxbox \
      fonts-dejavu-core fonts-noto-color-emoji ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 browser && mkdir /profile && chown browser:browser /profile
COPY start.sh /usr/local/bin/start-browser
RUN chmod 755 /usr/local/bin/start-browser

USER browser
ENV DISPLAY=:99
EXPOSE 6080
CMD ["start-browser"]
