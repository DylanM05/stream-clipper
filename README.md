# Stream Clipper

Turns long stream VODs (Twitch, YouTube, Kick, X) into chapter videos and highlight clips for a creator's clips channel, with a phone app to review them.

**Status:** see the [plan](docs/plan.md).
- **Phase 1:** the processing pipeline.
- **Phase 2:** the review app.
- **Phase 3, first step:** the upload kit for posting by hand.
- **Phase 4, part:** shows, plus watching Kick, Twitch and YouTube channels.
- **Next:** uploading through the YouTube API once Google approves the audit, then X monitoring and notifications.

How it works:

1. List the **YouTube channels** you upload to, such as your clips channels.
2. Set up each **show** once:
   - what to make from each video: clips, the full episode, or both
   - title patterns
   - context and guidance for Claude
   - which channel the clips and the full episodes go to
   - the Kick, Twitch or YouTube channels to watch
3. New streams are picked up from watched channels once they've ended. You can also paste any link in the app.
4. The worker downloads the video and makes a small preview copy. Then it transcribes the video, has Claude (through Claude Code on your subscription) pick chapters and highlights the way the show's guidance asks, and snaps the cuts to sentence boundaries. Titles follow the show's patterns, e.g. `KINO CASINO Oct 23, 2026 - <title Claude wrote>`. A full episode gets Claude's episode title and summary, plus YouTube chapter timestamps in its description.
5. Everything lands in your **Inbox**. Swipe right to approve and left to reject, or tap one to trim it on the preview, fix the title, pick its channel, and choose whether it also becomes a vertical Short.
6. Approved clips are rendered on the GPU and land in **Upload** with everything needed to post them.

A pre-show from X is usually posted whole, and Claude writes its short title from the post's text. Links that don't match any show are posted whole if they're from X and clipped otherwise.

## Server setup (Ubuntu, NVIDIA GPU)

1. **NVIDIA driver.** If `nvidia-smi` doesn't list your GPU yet:
   ```bash
   sudo ubuntu-drivers install && sudo reboot
   ```
2. **Docker and the NVIDIA Container Toolkit.** Install the toolkit with [NVIDIA's install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html):
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER          # then log out and back in
   # ...install the NVIDIA Container Toolkit, then:
   sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
   docker run --rm --gpus all nvidia/cuda:12.9.1-base-ubuntu24.04 nvidia-smi   # should list your GPU
   ```
3. **Code and settings:**
   ```bash
   git clone <your repo URL> stream-clipper && cd stream-clipper
   cp .env.example .env                    # set CLIPPER_APP_PASSWORD and CLIPPER_HOST_DATA_DIR
   sudo mkdir -p /srv/stream-clipper/data && sudo chown 1000:1000 /srv/stream-clipper/data
   docker compose build
   ```
4. **Sign in to Claude Code, once.** The login is kept in a Docker volume:
   ```bash
   docker compose run --rm --entrypoint claude worker
   # open the link it shows, sign in with your Claude account, paste the code back, then type /exit
   ```
5. **Check everything:**
   ```bash
   docker compose run --rm worker doctor      # NVENC, GPU for Whisper, Claude login, data folder
   docker compose run --rm worker llm-check   # one tiny Claude request with structured output
   ```
6. **Start the app and the worker.** Both restart on their own after a reboot:
   ```bash
   docker compose up -d
   docker compose logs -f worker              # watch what the worker is doing
   ```
7. **Reach it from your phone with Tailscale.** It's free, private and needs no port forwarding.
   - Install Tailscale on the server and your phone, and sign both in to the same account.
   - In the Tailscale admin console, under **DNS**, turn on **MagicDNS** and **HTTPS Certificates**.
   - On the server:
     ```bash
     sudo tailscale serve --bg 8080
     ```
   - On your phone, open `https://<server-name>.<your-tailnet>.ts.net` and sign in with `CLIPPER_APP_PASSWORD`.
   - Add it to your home screen: in Safari use **Share → Add to Home Screen**; in Chrome use **⋮ → Add to Home screen**. It then opens full-screen like an app.

8. **Sign in the upload browser (for automatic uploads).**
   ```bash
   sudo tailscale serve --bg --https=8443 6080
   ```
   Open `https://<server-name>.<your-tailnet>.ts.net:8443/vnc.html` and enter `CLIPPER_BROWSER_PASSWORD`. You'll see a Chromium window: sign in to YouTube with the Google account that manages your clips channels. It stays signed in. Set `CLIPPER_BROWSER_VIEW_URL` in `.env` to that address so the app links to it.

To update after pulling new code: `docker compose build && docker compose up -d`.

## Automatic uploads through YouTube Studio

The worker uploads by driving YouTube Studio in that signed-in browser, the same clicks you'd make:

- It picks the right channel, attaches the file and fills in the title and description.
- It sets "not made for kids" and the visibility you chose (Public, Unlisted or Private).
- It waits for the upload to finish and saves the video's link.

Start one from a clip's **Upload** page. Uploads run one at a time and show their progress there.

**Know the risks:**
- **YouTube's terms:** they only allow automated access through the official API, so this puts the channels at some risk of strikes or suspension. It was chosen over waiting for API approval.
- **Studio changes:** Studio is a web page. When YouTube changes it, uploads fail until the selectors in `backend/clipper/studio.py` are updated. A failed upload saves a screenshot (**What Studio showed**) of where it got stuck.
- **Language:** Studio must be set to English.
- **Interrupted uploads:** if the worker restarts mid-upload, the job is marked failed rather than retried, so it can't post twice. Check Studio before retrying.

The manual steps (share to the YouTube app, or download and drag into Studio) stay on the same page as a fallback.

## The phone app

| Screen | What you do there |
|---|---|
| **Inbox** | Review proposed clips. Swipe right to approve, left to reject, or use the buttons. After each decision you get an **Undo**. |
| **Clip editor** | Tap a clip to open it. Drag the handles on the strip under the video, or fine-tune with **−1s / −0.2 / Here / +0.2 / +1s**; every change plays the few seconds around the new cut. **Last 5s** checks that nothing is cut off. Tap a transcript line to hear it, or use its **Start** and **End** buttons to cut there. Edit the title and description, and turn the vertical Short on or off. The volume slider only changes the preview, not the clip; on iPhones it's a mute button, because iOS only allows the hardware buttons. **Approve** takes you straight to the next clip. |
| **Videos** | Paste a link and it's matched to its show, which sets the mode, notes and title pattern. You can override the mode with **Find clips** or **Whole video**, and add notes for Claude. Shows each video's progress. Failed videos can be retried; videos that hit your Claude usage limit retry on their own once it resets. |
| **Upload** | Clips being rendered, clips ready to upload, and uploaded ones. Tap **Upload** on a ready clip: pick its channel, then either **upload automatically** (Public, Unlisted or Private; the worker does it through YouTube Studio, see below) or do it by hand. By hand means sharing the file straight to the YouTube app from a phone, or downloading it on a computer (it's named after the title, so Studio fills that in). Copy the title and description, then **Mark as uploaded**. New cut points render the clip again; a new title or channel doesn't. |
| **Shows** | One per show. **What to make** is Clips, Full episode or Both. **Title patterns** (one for clips, one for full episodes) use `{date}` for the stream date (e.g. Oct 23, 2026) and `{title}` for the part Claude writes; Claude gets however many characters are left of YouTube's 100. **Clips go to / Full episodes go to** pick the YouTube channels. **About the show** gives Claude context; **What the chapters and clips should be** tells Claude what to look for, and it follows that over its defaults. **Links that belong to this show** match pasted links. **Channels to watch** takes `kick.com/name`, `twitch.tv/name` or `youtube.com/@name`, with optional filters: keywords, words to skip, minimum length. The worker checks each channel every 10 minutes and queues a stream once it has ended. Videos already on a channel when you add it are listed with a **Process** button but not processed automatically. Your YouTube channels are listed under **Shows → YouTube channels you upload to**. |

## Command line

Everything the app does is also available from the command line, e.g. `docker compose run --rm worker <command>`:

| Command | What it does |
|---|---|
| `add "<url>" [--whole/--segment] [--notes "..."]` | Queue a video for the worker, same as the app's **Add video**. |
| `process "<url>" [--notes "..."] [--no-render] [--redo STEP] [--only ID]` | Run the whole pipeline in the foreground without the app; handy for testing prompts. |
| `doctor`, `llm-check` | Check the setup and the Claude Code login. |
| `browser` | On a desktop: open the upload browser (Chrome or Edge with its own profile) to sign in to YouTube. |
| `serve`, `worker` | The two services `docker compose up` runs. |

Each video's files are in `<data>/vods/<platform>-<id>/`:

| File | What it is |
|---|---|
| `meta.json` | Title, channel, platform chapters, path of the downloaded video |
| `proxy.mp4` | The small 480p copy the phone app previews and trims with |
| `transcript.json` | Every line and word with timestamps |
| `signals.json` | Long silences, loud moments, scene cuts |
| `llm/segmentation.json` | Claude's chapters and highlights, as returned |
| `segments.json` | Clips with cut points snapped to sentences |
| `clips/` | Rendered `chNN-*.mp4` chapters, `hlNN-*.mp4` highlights and `hlNN-*-short.mp4` vertical Shorts |

The database of videos, clips and review decisions is `<data>/clipper.db`.

### Claude usage

Segmenting sends the whole transcript to Claude in one request: about 100–130k tokens for an 8-hour stream. That counts toward your Claude plan's usage limits. On the Pro plan, a long VOD can use a large part of a 5-hour window. If the limit is reached, the video waits ("Waiting for Claude" in the app) and continues by itself once the limit resets.

The app only ever runs the official `claude` program with your own login. Don't let other people use it through your account; if that ever changes, switch to an API key.

## Local development (Windows)

Keep the virtual environment outside Nextcloud. `node_modules` is in Nextcloud's ignore list.

```powershell
python -m venv $env:USERPROFILE\.venvs\stream-clipper
& $env:USERPROFILE\.venvs\stream-clipper\Scripts\pip install -e "backend[transcribe,dev]"
& $env:USERPROFILE\.venvs\stream-clipper\Scripts\python -m pytest backend

cd frontend; npm install; npm run build      # the app at frontend/dist, served by `clipper serve`
```

With no NVIDIA GPU, run everything on the CPU. It's slow (about 6 minutes to transcribe 20 minutes of audio) but fine for short tests:

```powershell
$env:CLIPPER_DATA_DIR = "$env:USERPROFILE\StreamClipperData"   # outside Nextcloud
$env:CLIPPER_WHISPER_DEVICE = "cpu"; $env:CLIPPER_WHISPER_COMPUTE_TYPE = "int8"
$env:CLIPPER_VIDEO_ENCODER = "libx264"; $env:CLIPPER_HWACCEL = "none"
clipper serve       # in one terminal: http://localhost:8080
clipper worker      # in another
```

Use `none` rather than an empty value: PowerShell deletes a variable that is set to `""`. For frontend work, `npm run dev` in `frontend/` serves the app with live reload and forwards API calls to `clipper serve`. Regenerate the app icons with `python frontend/scripts/make_icons.py`.
