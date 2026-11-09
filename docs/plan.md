# Stream Clipper: Plan

## Context

You run clip channels for creators who have given you permission. Right now the work is manual: watching for new streams and posts, downloading 4–8 hour VODs, finding where each segment starts, cutting the clips and uploading them. This app automates that whole loop:

**monitor → download → transcribe → find segments → cut → review on phone → upload to YouTube**

The project folder is empty, so this is a new build.

**Decisions from your answers:**
- **Two kinds of segments:** long-form chapters (topic, game or guest, 5–30 min) and short highlights (15 s to 3 min, also rendered vertically as Shorts).
- **Runs on your server** with the 8 GB NVIDIA GPU, which handles both transcription and video encoding. Your main PC and its AMD GPU aren't needed.
- **Publishing:** a mobile-first review app is the default. Each source also gets a "fully automatic" switch.
- **X/Twitter:** one account and its preshow video three times a week. That video is re-uploaded whole, with no segmenting.
- **AI uses your Claude subscription, not a paid API key.** The app runs Claude Code itself in headless mode (`claude -p`), signed in with your own subscription.
- **X is watched through a real logged-in browser on the server, at no cost.** The app shows that browser live, along with a list of the posts it has captured.

---

## Architecture

```
 Monitors (scheduled)        Pipeline workers (server)                    Review & publish
 ───────────────────        ──────────────────────────────────           ─────────────────
 YouTube RSS      ─┐        download (yt-dlp) ─► 480p proxy (NVENC)      Phone app (PWA via Tailscale)
 Twitch Helix API ─┤ rules  ─► audio extract ─► transcribe (faster-whisper) ─► Inbox: approve / trim / edit
 Kick API         ─┼─engine─► signals (scene cuts, silences, chat spikes)  │        │
 X (logged-in     ─┤        ─► segment (Claude Code `claude -p`:           │   auto mode skips review
   browser)        │             chapters + highlights)                 │
 Phone share/URL  ─┘        ─► snap cut points to sentence/silence        ▼        ▼
                             ─► render final (ffmpeg NVENC, 16:9 + 9:16) ─► YouTube upload queue
 SQLite job state machine + ntfy push notifications at every hand-off
```

---

## Components

### 1. Sources and monitoring

| Platform | How new content is detected | Downloaded with |
|---|---|---|
| YouTube | Poll the channel RSS feed every 5 minutes. This is free and uses no API quota. yt-dlp metadata then tells live, upcoming and finished-VOD apart. | yt-dlp |
| Twitch | Helix API with a free app token: poll `streams` to see a stream go live and then offline, then `videos?type=archive` for the VOD. Title and category are the indicators. Twitch's own VOD chapters (game changes) are free segment hints. | yt-dlp. If VODs come back muted or get deleted, streamlink can record the live stream instead. |
| Kick | Kick's official public API (docs.kick.com) for live status or webhooks, with polling as the fallback. This is the least stable integration and gets verified in Phase 4. | yt-dlp |
| X | A logged-in browser on the server checks the account's profile and captures new posts. See the X section below. | yt-dlp, using that browser's login cookies |
| Manual | Share a URL from your phone, paste one into the app, or drop a file into a watched folder. | n/a |

When a stream goes offline, the app waits until the VOD has finished processing (its duration stops changing) before downloading.

### 2. Rules engine (`config/sources.yaml`, also editable from the app)

```yaml
sources:
  - name: CreatorA Twitch
    platform: twitch
    handle: creatora
    match: { any_keywords: ["podcast", "ep."], exclude: ["rerun"], min_duration: 2h }
    pipeline: [chapters, highlights]
    destination: creatora-clips        # which YouTube channel login to upload with
    publish: { mode: review, auto_threshold: 0.85, spacing: 3h }
  - name: CreatorA preshow
    platform: x
    handle: creatora
    match: { has_video: true, days: [mon, wed, fri] }
    pipeline: [passthrough]            # download and re-upload whole
    destination: creatora-clips
    publish: { mode: auto }
```

Rules can match on: keywords (any or all), regex, exclude words, category or game, duration range, content type (VOD, upload or post-with-video) and days of the week.

### 3. Transcription
- **Model:** faster-whisper `large-v3-turbo` in float16 with word-level timestamps and built-in voice detection. It fits in 8 GB of VRAM.
- **Speed:** an 8-hour VOD should take about 10–25 minutes.
- **Input:** audio is extracted first with ffmpeg as 16 kHz mono.
- **Output:** saved as JSON (words with timestamps) and kept permanently. It's small, and kept transcripts let segments be re-cut later without transcribing again.

### 4. Segmentation (the core problem: no timestamps are provided)

**Hints gathered by code first:**
- Twitch chapters
- scene cuts and BRB or starting-soon screens (PySceneDetect)
- long silences
- loudness and laughter spikes
- chat-rate spikes from the Twitch or YouTube chat replay

**Two Claude passes, run through Claude Code on your subscription:**
- **How each pass runs:** the worker calls the Claude Code command line in headless mode, with no paid API key:
  ```
  claude -p "<task>" --model opus --output-format json --json-schema '<schema>' \
         --append-system-prompt-file prompts/chapters.md --permission-mode dontAsk  < transcript.txt
  ```
  - The JSON comes back in the `structured_output` field. The app checks that field exists, then validates it with pydantic. A failed check is retried once.
  - Each pass runs in an empty working folder with permission mode `dontAsk`, so Claude can't run commands or edit files. That folder has no CLAUDE.md, hooks or MCP servers for Claude Code to pick up.
  - **Don't use `--bare`.** It skips the subscription login and requires an API key.
- **Input:** the transcript, piped in as timestamped lines (`[02:14:07] …`, roughly one per sentence), plus the hints.
  - An 8-hour transcript is about 100–130k tokens and about 0.5 MB, well under Claude Code's 10 MB limit for piped input.
- **One wrapper for all AI calls:** chapters, highlights, titles and descriptions all go through `llm.py`.
  - Default backend: `claude_code`. A second backend that uses a paid API key can be switched on in config if you ever need it.
  - If you hit your plan's usage limit, Claude Code returns an error. The job waits, retries after the limit resets and sends you an ntfy notice.
- **Pass A, chapters:** returns `{start, end, title, summary, confidence}` for each chapter.
- **Pass B, highlights:** returns `{start, end, hook_title, why, score, vertical_ok}` for each clip, 15 s to 3 min long.
- **Per-creator style:** each source can have notes plus the titles of the creator's existing clip-channel videos, used as examples so titles match their tone.

**Boundary refinement, done in code:**
- Snap each start to a sentence start with a ~0.5 s lead-in, and each end to a sentence end or silence.
- Enforce minimum and maximum lengths.
- Merge overlapping highlights.

### 5. Rendering
- **Encoding:** ffmpeg with `h264_nvenc` re-encodes every cut, so cuts are frame-accurate rather than snapping to keyframes.
- **Chapters:** rendered at source resolution, with an optional intro and outro.
- **Shorts:** 1080×1920 with a choice of layout: center crop or blurred-background fill to start, face-tracked crop later. Word-by-word captions are burned in from the word timestamps (ASS subtitles).
- **Phone previews:** a 480p proxy of the whole VOD is made at download time. Previews and trimming use the proxy, and the final render runs only after you approve.
- **Thumbnails:** candidate frames are extracted, and you pick one in the app.

### 6. Review app (mobile-first PWA)
- **Stack:** FastAPI backend with live updates over Server-Sent Events, and a React + Vite + Tailwind frontend. It's installable to your home screen, uses large touch targets and has a dark mode.
- **Inbox:** swipeable cards for proposed clips: approve, reject or edit.
- **Clip editor:** a proxy player with draggable in and out handles, ±0.5 s nudge buttons and loop preview. You can also edit the title, description and tags, switch between long-form and Short, pick a thumbnail and set the publish time.
- **Pipeline:** live progress for each job (downloading, transcribing at 63%, …) and a retry button on failures.
- **Sources & Rules:** add and edit sources, keywords and destinations. Each source has its own auto-publish switch and confidence threshold.
- **Published:** history with YouTube links, plus an "unpublish" button that also works on clips auto mode published.
- **Settings:** YouTube quota used today, free disk space and the status of each API key.
- **Remote access:** Tailscale on the server and your phone. It's free and private, needs no port forwarding, and the app keeps its own login as well.
- **Notifications:** ntfy push messages, for example "7 clips ready from Tuesday's stream" or "Upload failed".
- **Ingest from phone:**
  - Android: the app appears in the share sheet (PWA share target).
  - iOS: a Shortcut sends the URL to `/api/ingest`.

### 7. Publishing to YouTube
- **Sign-in:** YouTube Data API v3 with an OAuth login for each destination channel, done once from the app. Refresh tokens are stored encrypted.
- **Order of operations:**
  1. render after approval
  2. resumable `videos.insert`
  3. set privacy or `publishAt`, spaced out per source
  4. add to playlist
  5. set thumbnail
- **Description template:** credits the creator and links to the original VOD at the right timestamp.
- **Quota:** default 10,000 units/day. Google's docs list 100 units per upload, older references say 1,600, so the app reads the real figure from Cloud Console. The upload queue tracks units and waits for the daily reset at midnight Pacific when the quota runs out.
- **⚠ Blocker to start on day one:** API projects that haven't passed Google's audit can only upload **locked-private** videos. Submit the *YouTube API Services Audit & Quota Extension* form in Phase 0, because approval can take weeks. Until then:
  - test against a throwaway channel
  - the app can hand you the rendered file to upload by hand
- **Update, Oct 2026:** the `videos.insert` docs now say uploads from unverified projects *aren't* restricted to private, and uploads have their own bucket of 100 per day. The change wasn't in the revision history, so the app flags any upload YouTube made private when you asked for something else. Confirmed Oct 24, 2026: an Unlisted upload from the unaudited project stayed Unlisted.

### 8. Orchestration
- **Language and storage:** Python 3.12, SQLite in WAL mode, and SQLModel.
- **Job states:** `detected → downloading → transcribing → segmenting → awaiting_review → rendering → uploading → published`. Any state can go to `failed`, which retries with backoff, and every step can safely re-run.
- **Two worker lanes:**
  - **GPU lane:** one job at a time (transcription and NVENC rendering).
  - **I/O lane:** downloads, uploads and API calls.
- **Scheduling:** APScheduler runs the monitor polls.
- **Deployment:** Docker Compose. GPU access in containers uses the NVIDIA Container Toolkit. This works on Linux, or on Windows through Docker Desktop with WSL2. The services:
  - `api`, which also serves the phone app
  - `worker`, which has the Claude Code CLI installed; its `~/.claude` login folder is kept in a volume
  - `xbrowser`: Chromium with a saved profile, driven by Playwright and viewable over noVNC
  - optionally a self-hosted `ntfy`
- **Claude Code login on the server:** do this once.
  - Option 1: run `claude` in the worker container over SSH and complete the sign-in through Anthropic's own login flow (it gives you a link to open on your phone or PC).
  - Option 2: run `claude setup-token` on your PC and put the result in `CLAUDE_CODE_OAUTH_TOKEN` on the server. Some users report 401 errors with these tokens, so option 1 is the main path.
  - The Settings screen shows the login status, using `claude auth status`.
- **Storage:** media lives on the server's data disk, never in Nextcloud. Plan for 15–25 GB per 8-hour 1080p60 VOD. Source VODs and proxies are deleted N days after all their clips are published or rejected. Transcripts and segment data are kept.

---

## X/Twitter: a logged-in browser on the server (free)

**Why not an iframe:** x.com refuses to be embedded in other sites, so an iframe of it would show up blank. The app gets the same result another way.

**As built (Oct 2026):** there's no separate `xbrowser`. The upload browser (the `browser` service) also reads X, signed in to a spare account through its noVNC view (`xposts.py`). An X account is a channel to watch like Kick or Twitch: every 10 minutes the worker opens the profile, reads the posts from X's own timeline data, and queues the account's new video posts straight away. Filters (keywords in the post's text, minimum length) take the place of a preshow rule, and the account's card shows whether the browser is signed in to X. Videos download with yt-dlp without cookies, which works for public posts. Not built: the feed list with thumbnails, faster checks around the preshow's time slots, and ntfy alerts.

- **Real browser:** a Chromium browser runs on the server with a saved profile (the `xbrowser` service).
- **Logging in:** the app's **X** tab embeds a live view of that browser over noVNC. noVNC is self-hosted, so it *can* go in an iframe.
  - You log in once, including 2FA, from your phone or PC.
  - You can scroll the real feed there whenever you like.
  - The same view is how you log back in if the session ever expires. The app sends an ntfy alert when it detects you've been logged out.
- **Monitoring:**
  - Every 10–15 minutes, and every 2–3 minutes during the preshow's usual time slots, Playwright opens the account's profile.
  - It reads the post data X's own page loads (text, time and video URLs). Reading that data is more reliable than scraping the page's HTML.
  - It records any posts it hasn't seen before.
- **Feed list:** the X tab also lists captured posts with text, video thumbnail, time and status (matched, ignored, downloaded or uploaded).
  - Posts matching the preshow rule (has video, plus keyword or day) are processed automatically.
  - Any other post has a **Process** button.
- **Download:** yt-dlp downloads the video using the cookies from that same browser profile. Its fallback is the best-quality MP4 link in the captured data.
- **⚠ Risk:** X's terms of service forbid automated access, even through your own login. One profile checked a few times an hour is low risk but not zero: X could challenge or lock the account. **Use a secondary X account, not your main one.**
- **Backups that need no browser:**
  - Share the post from your phone to the app (Android share sheet or iOS Shortcut).
  - Have the creator's team drop the file into a shared Nextcloud folder the app watches.
  - X's official API is pay-per-use, about $1–3/month for one account. It's an option to switch to later if the browser approach becomes unreliable.

---

## Project layout

```
stream-clipper/
  docker-compose.yml   .env.example   config/sources.yaml
  backend/app/
    main.py  db.py  models.py  rules.py  notify.py
    api/            clips.py jobs.py sources.py ingest.py auth.py xfeed.py
    jobs/runner.py  # job queue stored in the database (GPU lane + I/O lane)
    llm/            base.py claude_code.py api.py   # claude_code (default) runs `claude -p`
    prompts/        chapters.md highlights.md metadata.md
    monitors/       base.py youtube.py twitch.py kick.py x_browser.py folder.py
    pipeline/       download.py proxy.py audio.py transcribe.py signals.py
                    segment.py refine.py render.py captions.py
    publish/        youtube.py quota.py
  backend/tests/
  frontend/         # React + Vite + Tailwind PWA
  xbrowser/         # Dockerfile: Chromium + Playwright + noVNC, saved profile volume
```

Keep the code in a git repo, either outside this Nextcloud folder or excluded from sync. Syncing `.venv` and `node_modules` folders through Nextcloud is painful.

---

## Build phases

**Phase 0: accounts and approvals.** Start these now, because some take weeks.
- Google Cloud project with the YouTube Data API and an OAuth client, then **submit the audit form**
- Twitch developer app
- Kick developer app
- Install Claude Code on the server, log in with your subscription, and check that `claude -p --output-format json --json-schema …` returns a `structured_output` field
- A secondary X account for the browser monitor
- Tailscale on the server and your phone
- Confirm the server's OS and NVIDIA driver, and that Docker can see the GPU (`nvidia-smi` inside a container)

**Phase 1: core pipeline as a command-line tool.** This is the riskiest part, so it goes first.
- `clipper process <url>` runs download → transcribe → segment → render into a local folder.
- Tune the prompts and refinement on 3 real VODs from your creators.

**Phase 2: review app.**
- Proxy generation, Inbox, the clip editor with trimming, metadata editing, and approve and reject.

**Phase 3: YouTube publishing.** First step done (decided after the first test): an upload kit for posting by hand, with an Upload tab, the destination channel per clip, share-to-YouTube-app on phones, downloads named after the title, and "mark as uploaded". It works without the API, so publishing isn't blocked on Google's audit. Embedding YouTube Studio in an iframe isn't possible (YouTube blocks it). Then, at the user's request and knowing it breaks YouTube's terms, automatic uploads by driving YouTube Studio in a signed-in browser on the server (the `browser` service, signed in through noVNC). Then, once Google's docs said unaudited projects can post public videos, uploads through the API (`youtube.py`): Google sign-in per channel from the app, and resumable uploads that carry on after a dropped connection or restart. Channels not connected still go through Studio.
- As built (Oct 2026): playlists per show or clip (never for Shorts); spacing a show's clip uploads hours apart per channel (`schedule.py`), held by the worker rather than YouTube's scheduled publishing so it works for Studio uploads too; thumbnails whose idea and prompt Claude writes and an OpenRouter image model draws from frames of the video (`thumbnails.py`), set through the API after upload; and views read back for each upload (`stats.py`), compared against Claude's score and fed back to Claude as the show's best titles, with a weekly Discord summary.

**Phase 4: monitors and automation.**
- YouTube, Twitch and Kick monitors, the rules engine, the scheduler, ntfy notifications, phone share ingest and the watched folder.
- The `xbrowser` service: live view, X feed list and preshow rule. Done in the upload browser instead (see the X section).
- As built (Oct 2026): notifications go to an ntfy topic and/or a Discord webhook (`notify.py`; ntfy can't forward to Discord). Downloads, processing, renders and uploads can each be paused, as can one video or clip; running work stops at its next progress report.

**Phase 5: auto mode and polish.**
- Per-source auto-publish thresholds, Shorts layouts and captions, chat-spike signals, cleanup of old files, and per-source approval-rate stats (used to decide when auto mode can be trusted).
- As built (Oct 2026), simpler than thresholds: each show offers its N best-scored chapters and Shorts per stream (Claude now scores chapters too) and keeps the rest as extras; per kind (full episode, chapters, Shorts) it can review, render without review, or render and upload. Cleanup runs as soon as a stream is done, and each render is deleted once uploaded.
- Per-creator segment style (requested after the first test): for the 8-hour Kick streams, longer and broader chapters that follow specific people (such as a regular guest), split where the hosts announce a new segment.
- A stacked Shorts layout for two-webcam shows: each webcam cropped and placed one above the other.

---

## Setup in the app (planned Nov 9, 2026, not started)

The first time the app is opened, it walks you through getting set up, instead of leaving that to the README. You pick settings in the app, and the app does what's needed behind the scenes: it signs in to the AI, saves keys and checks that things work.

### How it fits together
- **One place for settings.** Every setting the app manages is read from the database (`app_setting`) first, then `.env`, then the built-in default. The worker and the web app both read it this way, so a change applies without a restart. Code that reads `.env` directly moves over to it: the OpenRouter key (`ai.py`, `thumbnails.py`), notifications (`notify.py`) and the Google client (`youtube.py`).
- **Secrets can be written but not read back.** The OpenRouter key, the Google client secret, the Discord webhook and the ntfy token are stored in the database, like YouTube sign-ins already are. The API only says whether each is set, with its last 4 characters. Secrets are never logged.
- **A Settings screen** is where these settings live from then on: the AI, connections, notifications and the system. The AI card moves there from Videos.
- **The setup reuses the Settings screen's parts**, one step at a time. So "change it later" means opening Settings, and nothing is built twice.

### What moves into the app, and what stays in `.env`
- **Moves into the app:**
  - the AI's provider, sign-in and models, and the OpenRouter key
  - Discord and ntfy, with a test
  - the app's address (taken from the browser's address bar)
  - the Google client ID and secret, the YouTube API key and the upload category
  - the upload browser's view address (guessed, and editable)
  - the time zone (filled in from the browser), when the day starts, and the language
  - the encoder and hwaccel (picked by the system check)
  - the thumbnail model, the Shorts layout and the minimum free space
- **Stays in `.env`**, because Docker needs it before the app starts:
  - `CLIPPER_HOST_DATA_DIR` and `CLIPPER_HOST_MEDIA_DIR` (volume mounts)
  - `CLIPPER_PORT` and `CLIPPER_BIND`
  - `CLIPPER_WORKER_MEMORY` (a compose limit)
  - `CLIPPER_BROWSER_PASSWORD` (passed to the browser container)
  - the app password, unless decided otherwise (see the open questions below)
- Anything still set in `.env` stays as the default, so existing installs keep working.

### The setup, step by step
0. **Welcome and password**, if the password moves into the app.
1. **System check.** This is `doctor` in the app:
   - Can the data folder be written to, and how much space is free?
   - Does NVENC work? If not, it switches to libx264 by itself.
   - Is the GPU there for Whisper?
   - Can the upload browser be reached?

   It also starts downloading the Whisper model (about 1.6 GB) in the background, so the first video doesn't wait for it. A problem that can only be fixed in `.env` says which line to set.
2. **Basics:** the time zone, when the day starts, and the language.
3. **The AI.** Pick Claude Code, Codex or OpenRouter (more than one is fine):
   - **Codex:** the app runs `codex login --device-auth` in its container, shows the link and code, and moves on once the sign-in goes through.
   - **Claude Code:** the app shows the sign-in link, and you paste the code back into the app.
   - **OpenRouter:** paste the key; it's checked right away. Then pick from OpenRouter's models that support structured outputs.

   Then **Test it** (`llm-check`) and pick the model. Sign out is in Settings.
4. **Where videos go.** Add a YouTube channel, then pick how it uploads:
   - **API:** a guided Google Cloud walkthrough that shows the exact redirect URI. Paste the client ID and secret into the app, then press Connect. It warns first if the app isn't open on an https `ts.net` address (`google_accepts`).
   - **The upload browser:** goes on to step 5.
   - **By hand.**

   Rumble can be turned on here too.
5. **Upload browser.** Only shown if needed, for watching X, Studio uploads or Rumble. It shows the noVNC link and checks the YouTube, X and Rumble sign-ins as they happen.
6. **First show.** A simpler version of the show editor:
   - the name, what to make and what its videos are
   - the title pattern, with an example title as you type
   - a channel to watch, with a check that lists its recent videos
   - where uploads go
   - whether each kind waits for review, is rendered, or is uploaded
7. **Notifications:** Discord and/or ntfy, with **Send a test**.
8. **Done:** what's set up and what was skipped, and "Paste your first video".

Each step saves as soon as it's done, any step can be skipped, and the setup carries on where you left off. It only appears on a first run: an install that already has shows counts as set up.

### Phases
- **Phase 0: checks before building.** These are small, and run on the server.
  - How Claude Code signs in without a terminal in the container: `claude auth login` or `setup-token`, and whether it needs a pseudo-terminal.
  - What Codex's device sign-in prints.
  - OpenRouter's endpoints for checking a key and listing models.

  The answers decide how Phase 2 is built. (Neither CLI is installed on the Windows PC.)
- **Phase 1: settings foundation.**
  - The database-then-`.env` lookup and write-only secrets, with the code that reads settings moved over to them.
  - The Settings screen, with the AI card, downloads and disk, and notifications moved into it.
  - Tests that a setting overrides `.env` and that a secret is never returned.
- **Phase 2: AI accounts in the app.**
  - Sign-in sessions in the web container: start the CLI, read the link and code, take the pasted code, report the status, time out.
  - The OpenRouter key check and model list, Test, and Sign out.
  - The "run this on the server" messages become **Sign in** buttons.
- **Phase 3: the rest of `.env` moves in:** notifications, the Google client, the YouTube API key, the app's address, the browser view address, the time zone and language, and picking the encoder.
- **Phase 4: the setup itself, and its first steps.** First-run detection, `#/setup`, progress, carrying on where you left off, and skipping. Then steps 1–3.
- **Phase 5: its other steps:** channels and YouTube, the upload browser, the first show, notifications, and Done.
- **Phase 6: docs and a test on a fresh install.**
  - The README's quick start comes down to installing Docker and the NVIDIA toolkit, running `docker compose up` and opening the app.
  - `.env.example` gets shorter.
  - A full install from scratch (empty data folder and volumes), locally on the CPU and on the server.

Phases 1–3 are useful even before the setup exists, and help the GitHub release (`docs/todo.md`).

### Open questions
1. **The app password.**
   - (a) It stays in `.env`.
   - (b) It's set in the setup, with a one-time setup code printed in `docker compose logs web`, so only someone with access to the server can claim the app.
   - (c) Whoever opens the app first sets it.

   Leaning (b): it's as easy as (c), without the risk.
2. **Where Settings goes on a phone.** The tab bar already has 6 tabs. Leaning towards a gear in the header, plus an entry in the desktop sidebar.
3. **Is the first show part of the setup?** It makes the setup longer, but without it a new install does nothing until you find Shows. Leaning yes, kept simple, with the full editor afterwards.

---

## Running costs (estimates)

| Item | Cost |
|---|---|
| Claude segmentation through Claude Code | Already covered by your Claude subscription. Each 8-hour VOD sends about 250k tokens across the two passes, which counts toward your plan's usage limits. Max handles several VODs a week easily; on Pro, long VODs may hit the 5-hour limit, in which case the job just waits. |
| X monitoring through the logged-in browser | free |
| yt-dlp, faster-whisper, ffmpeg, Playwright, noVNC, YouTube/Twitch/Kick APIs, Tailscale personal plan, ntfy | free |

---

## Risks and gotchas
- **Platform changes break yt-dlp now and then.** Pin the version, update it weekly, and send a notification when a download fails. Kick is the most fragile.
- **Twitch VODs** can come back with muted sections, and they expire after 7 or 60 days. Download promptly, or record live with streamlink.
- **AI cut points won't always be right.** The review app, snapping to sentences and silences, and approval-rate tracking all address that. Auto mode stays per-source and gated by a confidence threshold.
- **YouTube's reused-content policy:** clips need titles, captions and framing that add value, which helps keep them monetizable.
- **Disk space:** the cleanup policy, plus a free-space check before each download.
- **Using your Claude subscription:** Anthropic's terms let you run the unmodified Claude Code program with your own subscription. Its plan limits assume ordinary individual use, and other people must not use the app through your login.
  - The app only ever calls the `claude` command; it never takes the login token and uses it some other way.
  - If you ever offer the app to other people, switch to the paid-API backend.
- **The X browser:** it can get logged out, hit a captcha, or break when X changes its site. The fixes are the live view (log back in), the ntfy alert when logged out, and phone share as the backup. It runs on a secondary account.

---

## How we'll test it
- **Unit tests (pytest):**
  - rule matching, snapping cut points, quota accounting, transcript formatting and the JSON from segmentation
  - the `claude_code` wrapper, tested against recorded `claude -p` JSON output, including the missing-`structured_output` and usage-limit cases
  - the X post parser, tested against recorded post data
- **Smoke test on the server:**
  - `clipper llm-check` runs one tiny `claude -p` call with a schema and shows the login status.
  - `clipper x-check` loads the profile in `xbrowser` and lists the last 5 posts.
- **Segmentation test set:**
  - You hand-label the chapter starts in 2–3 real VODs.
  - Target: most predicted chapter starts within ±30 s of yours.
  - Highlights are judged by your approval rate in the app.
- **Monitor dry run:** monitors run for a week logging what they would have done, without downloading, and the log is checked against what actually happened.
- **Upload test:** uploads to a throwaway YouTube channel (private) before any real clip channel is connected.
- **End-to-end test:**
  1. `docker compose up` on the server.
  2. Share a short test VOD URL from your phone.
  3. An ntfy push arrives and the clips appear in the Inbox.
  4. Trim one and approve it.
  5. Check that the private video appears in YouTube Studio with the right title, description, thumbnail and schedule.
