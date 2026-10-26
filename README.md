# Stream Clipper

Turns long stream VODs (Twitch, YouTube, Kick, X) into chapter videos and highlight clips for a creator's clips channel, with a phone app to review them.

**Status:** see the [plan](docs/plan.md).
- **Phase 1:** the processing pipeline.
- **Phase 2:** the review app.
- **Phase 3:** uploads through the YouTube API, with YouTube Studio automation as a fallback, plus the upload kit for posting by hand.
- **Phase 4:** shows, watching Kick, Twitch and YouTube channels and X accounts, notifications (ntfy and Discord), and pausing.
- **Phase 5, part:** each show offers only its best few clips per stream, and can let kinds of video skip review.

How it works:

1. List the **YouTube channels** you upload to, such as your clips channels.
2. Set up each **show** once:
   - what to make from each video: clips, the full episode, or both
   - title patterns
   - context and guidance for Claude
   - which channel the clips and the full episodes go to
   - the Kick, Twitch or YouTube channels, or X accounts, to watch
3. New streams are picked up from watched channels once they've ended, and videos posted on X as soon as they're seen. You can also paste any link in the app.
4. The worker downloads the video. Then it transcribes the video, has Claude (through Claude Code on your subscription) pick chapters and highlights the way the show's guidance asks, and snaps the cuts to sentence boundaries. While Claude works, it makes a small preview copy for the phone (`CLIPPER_PREVIEW_WITH_TRANSCRIPTION=true` starts that during transcription instead, which is sooner but heavier on the server). Titles follow the show's patterns, e.g. `KINO CASINO Oct 23, 2026 - <title Claude wrote>`. A full episode gets Claude's episode title and summary, plus YouTube chapter timestamps in its description.
5. The show's best few chapters and Shorts per stream, by Claude's score (5 of each unless you change it), land in your **Inbox**, along with the full episode. Claude's other picks wait on the video's page as extras. Swipe right to approve and left to reject, or tap one to trim it on the preview, fix the title, pick its channel, and choose whether it also becomes a vertical Short. A show can also let a kind of video skip review: rendered and left ready to upload, or rendered and uploaded.
6. Approved clips are rendered on the GPU and land in **Upload** with everything needed to post them. Once the worker uploads a clip, its file is deleted; once every clip of a stream is done, so is the stream's video.

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
   docker compose run --rm worker doctor      # NVENC, GPU previews, GPU for Whisper, Claude login, data folder
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

8. **Set up uploading through the YouTube API.** Do this once in Google Cloud; it's free.
   1. [Create a project](https://console.cloud.google.com/projectcreate), then [enable the YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com) in it.
   2. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview) and click **Get started**:
      - App name: anything.
      - Contact email: yours.
      - Audience: **External**.
   3. Under **Branding**, fill in:
      - Application home page: `https://<server-name>.<your-tailnet>.ts.net`.
      - Privacy policy: `https://<server-name>.<your-tailnet>.ts.net/privacy.html`. The app serves this page without a login.
      - Authorized domains: `<your-tailnet>.ts.net`. `ts.net` is on the public suffix list, so Google treats each tailnet as a domain of its own.
      - Terms of service: leave empty.
   4. Under **Audience**, click **Publish app**. While the app is in testing, Google signs channels out after 7 days. Google doesn't need to verify it, since only you use it.
   5. Under **Clients**, create a client:
      - Type: **Web application**.
      - Authorized redirect URI: `https://<server-name>.<your-tailnet>.ts.net/api/youtube/callback`. The app shows the exact address to copy under **Shows → YouTube channels**.
   6. Put its ID and secret in `.env` as `CLIPPER_GOOGLE_CLIENT_ID` and `CLIPPER_GOOGLE_CLIENT_SECRET`, then run `docker compose up -d`.
   7. In the app, go to **Shows → YouTube channels** and tap **Connect** on each channel. Pick that channel when Google asks which account to use. Google warns that it hasn't verified the app: click **Advanced → Go to … (unsafe)**.
9. **The upload browser**, for watching X accounts and for channels you don't connect to the API.
   ```bash
   sudo tailscale serve --bg --https=8443 6080
   ```
   Open `https://<server-name>.<your-tailnet>.ts.net:8443/vnc.html` and enter `CLIPPER_BROWSER_PASSWORD`. You'll see a Chromium window. It stays signed in to whatever you sign in to there:
   - **To watch X accounts:** open a new tab and sign in at x.com with a spare X account (see [Watching X accounts](#watching-x-accounts)).
   - **For channels not connected to the API:** sign in to YouTube with the Google account that manages them.

   Set `CLIPPER_BROWSER_VIEW_URL` in `.env` to that address so the app links to it.

To update after pulling new code: `docker compose build && docker compose up -d`.

### Disk space

Keep `CLIPPER_HOST_DATA_DIR` on an SSD. Converting, previewing, transcribing and rendering all read or write the whole stream, which is several times slower on a hard drive.

An 8-hour stream takes about 45–60 GB at its peak: the source, the rendered chapters and clips, the preview copy and the audio.

**Cleanup:**
- Each rendered clip is deleted as soon as the worker has uploaded it. A full episode's file is the stream itself, which the other clips still need, so it goes with the stream.
- Once every clip of a stream is uploaded, rejected or an extra, the stream's big files (the video, preview copy, audio and renders) are deleted within a few minutes. To keep them a while in case you want to re-cut something, set a number of days on the Videos screen (`CLIPPER_CLEANUP_AFTER_DAYS`).
- To free a stream's space before then, open it on the Videos screen and tap **Delete this stream's video files**. Its clips that aren't uploaded yet are rejected, since there's nothing left to cut them from.
- The transcript and Claude's picks are always kept, but a stream's clips can't be re-cut once its files are gone.

**Low space:** while the disk has less than 40 GB free (`CLIPPER_MIN_FREE_GB`), new downloads wait.

To move the data to another disk:

```bash
docker compose down
sudo rsync -a /old/data/ /new/data/ && sudo chown -R 1000:1000 /new/data
# in .env: CLIPPER_HOST_DATA_DIR=/new/data
docker compose up -d
```

Anything that was mid-processing carries on from the files already there.

### Pausing

The switches on the Videos screen pause **Downloads**, **Processing** (preview copy, transcription and Claude's picks), **Rendering** and **Uploads** as a whole; the Upload screen has the last two as well. Each video also has a **Pause** button, and so does each clip waiting to render.
- What's running stops within a few seconds. A download keeps what it has and carries on from there; a transcription or render starts that step again. Claude's step and the audio analysis can't be stopped partway, so a pause takes effect once they finish.
- Pausing processing still lets downloads go ahead, and the other way round, so you can download overnight and process later.
- An upload that has started finishes first.

## Notifications

The worker can tell you when a video is ready to review, when something fails or needs you (a sign-in to redo, a full disk, Claude's usage limit), when a watched channel posts something new, and when a video is uploaded, with its link. It posts them to a Discord channel through a webhook, which is built into Discord, so there's nothing else to install. It can also send them to [ntfy](https://ntfy.sh), a separate push-notification app, instead or as well.

Set these in `.env`, then run `docker compose up -d`:
- **Discord:** in the channel's settings, go to **Integrations → Webhooks → New Webhook**, copy its URL and set `CLIPPER_DISCORD_WEBHOOK_URL` to it. An upload notification includes the YouTube link, which Discord shows as the video.
- **Links back to the app:** `CLIPPER_APP_URL=https://<server-name>.<your-tailnet>.ts.net` makes a notification link to the right page.
- **ntfy (optional):** `CLIPPER_NTFY_URL=https://ntfy.sh/<topic>`. Pick a long random topic name: anyone who knows a topic on ntfy.sh can read it. For your own ntfy server, use its address and, if the topic is protected, an access token in `CLIPPER_NTFY_TOKEN`. Subscribe to the topic in the ntfy phone app.

The Videos screen says where notifications go and has a **Send a test** link.

## Automatic uploads

Start one from a clip's **Upload** page and pick Public, Unlisted or Private. Uploads run one at a time and show their progress there. A channel connected to the YouTube API uploads through the API; any other channel uploads through YouTube Studio in the upload browser.

A show can also upload a kind of video by itself: under the show, set **Without review** to **Upload** for its full episode, chapters or Shorts, and pick the visibility. They're rendered and queued for upload as soon as they're processed, to the channel the show sends them to. A highlight goes up as its Short if it has one, else as a regular video.

**Standouts** does the same for single clips: set it to a score (Claude scores every clip from 0 to 1 and keeps 0.9 and up for standout moments), and any chapter or Short scored at least that uploads straight away, even if its kind is set to **Review**. It only applies to the clips offered per stream, so it never uploads more than **Clips offered per stream** allows. The **Stats** screen shows whether high scores actually get more views.

**Space clips out** stops a stream's clips all landing at once: with it set to, say, 3 hours, each of the show's chapters and Shorts goes up at least 3 hours after the last clip upload to the same YouTube channel, whether it uploads by itself or you tap **Upload**. Full episodes don't wait. A waiting upload says when it goes, on the Upload screen and on its page, where **Upload now** sends it straight away. The worker has to be running at that time; a clip that's waiting keeps its rendered file until then.

### Through the YouTube API

The worker uploads with the channel's Google sign-in, as "not made for kids", in the Entertainment category (`CLIPPER_YOUTUBE_CATEGORY_ID`). The file goes up in pieces, so a dropped connection or a worker restart carries on from what YouTube already has. It can't post a video twice.

- **Daily limits:** each Google Cloud project can upload 100 videos a day. YouTube also limits how many videos each channel can upload in 24 hours and doesn't say how many; one channel hit it after 6 uploads in a day (3 of them Shorts). Uploads over it fail with a message to retry once 24 hours have passed. Nothing was sent, so retrying can't post twice.
- **Private videos:** Google used to lock every upload from a project it hadn't audited to private. That no longer happens: since Oct 2026 Google's docs say so, and a test upload from an unaudited project stayed Unlisted (Oct 24, 2026). If YouTube ever makes an upload Private anyway, the upload says so. The project would then need YouTube's [API audit](https://support.google.com/youtube/contact/yt_api_form).
- **Playlists:** pick a playlist on the channel, or make a new one, under each show's **Full episodes go to** and **Clips go to** (for example one playlist per creator), or for a single clip on its page. Each upload is added to it, except Shorts, which never go in a playlist. This needs the permission to manage the YouTube account, which channels connected before Oct 25, 2026 don't have: tap **Reconnect** on them once. If a video can't go into its playlist, the upload still counts and says why. Uploads through YouTube Studio aren't added to playlists; add those in Studio.
- **Expired sign-ins:** if a channel's sign-in expires or you revoke it in your Google account, its uploads fail with a message to connect it again. Tap **Reconnect** under **Shows → YouTube channels**.
- **Where the sign-ins are stored:** in the database in the data folder. Disconnecting a channel deletes its sign-in and revokes it at Google.

### Through YouTube Studio

For channels not connected to the API, the worker drives YouTube Studio in the signed-in upload browser, making the same clicks you would:
- It picks the right channel, attaches the file and fills in the title and description.
- It sets "not made for kids" and the visibility you chose.
- It waits for the upload to finish and saves the video's link.

**Know the risks:**
- **YouTube's terms:** they only allow automated access through the official API, so this puts the channels at some risk of strikes or suspension.
- **Studio changes:** Studio is a web page. When YouTube changes it, uploads fail until the selectors in `backend/clipper/studio.py` are updated. A failed upload saves a screenshot (**What Studio showed**) of where it got stuck.
- **Language:** Studio must be set to English.
- **Interrupted uploads:** if the worker restarts mid-upload, the job is marked failed rather than retried, so it can't post twice. Check Studio before retrying.

The manual steps (share to the YouTube app, or download and drag into Studio) stay on the same page as a fallback.

## Thumbnails

A clip's Upload page can make a thumbnail for its 16:9 video, and a show can make one for every chapter and full episode once it's rendered (**Thumbnails** under the show). Claude reads what's said in the video and comes up with a funny idea that's about what actually happens, starring the people in it, plus a few words of text if they help. An image model on [OpenRouter](https://openrouter.ai) draws it, with frames of the video as references so the hosts and guests look like themselves. Type your own idea in the box to steer it, and tap **Make another** until you like one. Under the picture, **People** says who's in it and where each likeness came from.

**People the video talks about** (another streamer, a YouTuber, a celebrity) can be in it too. Claude names them, and the app looks up a photo of each, up to three: the lead picture of their Wikipedia article, or else the picture of the YouTube channel a search for them finds first. The photo only counts if the article or channel has their name, so a search that turns up someone else is ignored. The image model gets those photos as references along with the frames. Anyone with no photo is drawn from Claude's description of them, which is hit and miss. Private people (callers, chatters, someone in a news story who isn't famous) are never drawn.
- The YouTube search needs `CLIPPER_YOUTUBE_API_KEY` or a channel connected to the API. Each search costs 100 units of the project's 10,000-unit daily quota; it's only used when Wikipedia has nothing.
- Some image models won't draw well-known people and send nothing back; the thumbnail then fails with a note saying so. Try again, steer it with your own idea, or switch `CLIPPER_THUMBNAIL_MODEL`.

- **Setup:** make an account at openrouter.ai, add some credit, create a key under **Keys** and set it as `CLIPPER_OPENROUTER_API_KEY` in `.env`, then `docker compose up -d`. Each thumbnail costs a few cents there (see the model's page), plus a small Claude call on your plan. `CLIPPER_THUMBNAIL_MODEL` picks the model: any OpenRouter image model that takes reference images. The default is Google's Gemini 2.5 Flash Image.
- **Who looks like who:** Claude only gets the transcript, not the frames, so it goes by names. Under the show's **About the show**, say what the regulars look like (e.g. "Don wears a red cap and glasses") and which names belong to the same person (e.g. "Jer is Jeremy Hambly, who runs TheQuartering"). The photo lookup needs the real name: "Jer" on its own could be any Jeremy, so it finds nothing.
- **On YouTube:** an upload through the API sets the thumbnail right after the video goes up, and a video waits up to 30 minutes for a thumbnail that's still being made. For a video already uploaded through the API, **Use on YouTube** puts a new one on. YouTube only allows custom thumbnails on channels verified with a phone number (youtube.com/verify). For channels uploading through YouTube Studio, **Download** it and add it in Studio. Shorts don't use custom thumbnails, so they don't get one.
- **Keep it fair:** the thumbnails show real people, including people who never agreed to be on the channel. Claude's instructions keep them about what the video actually says, cartoonish rather than like a real photo, and free of anything sexual or demeaning or any made-up scandal, but look at each one before it goes out. A thumbnail that could pass for a real photo of someone doing something they didn't do can get a video taken down for being misleading, or worse. Check the creators are happy with AI thumbnails of them too.

## How uploads are doing

The **Stats** screen lists every upload the app made with its views, likes and comments, newest or most viewed first. The worker reads them every few hours for each upload's first month, and keeps the views a day and a week after it went up so newer and older videos can be compared fairly.
- **Does the score mean anything?** The top of the screen shows the median views of chapters and highlights by the score Claude gave them. If the 0.9-and-up videos don't do better than the rest, Standouts is just uploading at random; raise it or turn it off.
- **Better titles:** the titles of each show's most-viewed clips go back to Claude when it picks the next stream's clips, as examples of what lands with the channel's viewers.
- **Weekly summary:** on Monday mornings (9 AM in `CLIPPER_TIMEZONE`) a notification lists the past week's uploads, their views so far and the five most viewed.
- **Setup:** uploads to channels connected to the YouTube API are read with their sign-in. To read uploads to other channels, connect at least one channel or, better, create an API key in the same Google Cloud project (**APIs & Services → Credentials → Create credentials → API key**, restricted to the YouTube Data API v3) and set it as `CLIPPER_YOUTUBE_API_KEY`. Reading costs 1 unit of the project's 10,000-unit daily quota per 50 videos.
- **What's counted:** uploads the worker made. Ones from before Oct 26, 2026 get their views so far but usually no day-one or week-one numbers. Clips you uploaded by hand and marked as uploaded aren't read.

## Watching X accounts

Add `x.com/name` under a show's **Channels to watch**. X only shows an account's posts to signed-in visitors, and its API costs money, so the worker reads them through the upload browser, signed in to X (setup step 9). Every 10 minutes it opens the account's profile there and reads the posts from the data X's page loads.
- **What's picked up:** the account's own posts with a video, as soon as they're seen. Reposts and text-only posts are left out. The filters apply to the post's text and the video's length: **Only posts with** takes words every wanted post has, and a minimum length keeps short clips out. Keywords are comma-separated and any one will do; join words with `+` to need all of them. For the Kino Casino pre-show, `tonight on + kino casino` matches every pre-show post so far (Oct 2026). Posts a filter leaves out are listed as skipped, with a **Process** button.
- **Signed out:** the account's card under **Shows** says whether the browser is signed in to X. If X signs it out or asks it to verify itself, the card shows that; sign in again in the browser.
- **X's terms:** they don't allow automated access, even through your own sign-in. One profile looked at every 10 minutes is unlikely to stand out, but X could ask the account to verify itself or lock it. Use a spare X account, never your main one.
- **X changes its site:** if the card says it found no posts at all, X has changed how its page sends them, and `backend/clipper/xposts.py` needs updating. Pasting the post's link on **Videos** still works meanwhile.

## The phone app

| Screen | What you do there |
|---|---|
| **Inbox** | Review proposed clips: each show's best few per stream, and the full episode. Swipe right to approve, left to reject, or use the buttons. After each decision you get an **Undo**. |
| **Clip editor** | Tap a clip to open it. Drag the handles on the strip under the video, or fine-tune with **−1s / −0.2 / Here / +0.2 / +1s**; every change plays the few seconds around the new cut. **Last 5s** checks that nothing is cut off, and **« 10s / « 5s / 5s » / 10s »** jump back or ahead while you watch. Tap a transcript line to hear it, or use its **Start** and **End** buttons to cut there. Edit the title and description, and turn the vertical Short on or off. The volume slider only changes the preview, not the clip; on iPhones it's a mute button, because iOS only allows the hardware buttons. **Approve** takes you straight to the next clip. |
| **Videos** | Paste a link and it's matched to its show, which sets the mode, notes and title pattern. You can override the mode with **Clips**, **Full** or **Both**, and add notes for Claude. Shows each video's progress. Failed videos can be retried; videos that hit your Claude usage limit retry on their own once it resets. Downloads are capped at 50 Mbit/s by default so the rest of your internet stays usable: about an hour for an 8-hour stream. Tap **Change** under the form to adjust it (0 = no limit). The lines below it show free disk space, when finished streams' files are deleted, and where notifications go. The switches above them pause parts of the work, and each video in progress has its own **Pause** (see [Pausing](#pausing)). Open a video to see all its clips, including the **Extras** that didn't make the show's best few (open one and approve it to use it), and to delete its video files early. |
| **Upload** | Clips being rendered, clips ready to upload, and uploaded ones. A clip waiting to render can be paused or sent back **To inbox**; a ready one can go back **To inbox** or be **Deleted**, which deletes its render too. Tap **Upload** on a ready clip: pick its channel, then either **upload automatically** (Public, Unlisted or Private; the worker does it through the YouTube API, or YouTube Studio for channels not connected to it, see below) or do it by hand. By hand means sharing the file straight to the YouTube app from a phone, or downloading it on a computer (it's named after the title, so Studio fills that in). Copy the title and description, then **Mark as uploaded**. New cut points render the clip again; a new title or channel doesn't. The same page makes the video's **Thumbnail** (see [Thumbnails](#thumbnails)) and shows each upload's views. A clip whose upload is waiting for its turn (see **Space clips out**) says when it goes up. |
| **Stats** | Views, likes and comments of every upload, and whether Claude's score predicts them (see [How uploads are doing](#how-uploads-are-doing)). |
| **Shows** | One per show. **What to make** is Clips, Full episode or Both. **Title patterns** (one for clips, one for full episodes) use `{date}` for the stream date (e.g. Oct 23, 2026: the day it started in the creators' time zone, `CLIPPER_TIMEZONE`, Eastern by default, with a stream starting before 6 AM counting as the night before's) and `{title}` for the part Claude writes; Claude gets however many characters are left of YouTube's 100, but keeps its part short: a chapter leads with its segment, then the main topic or two if there's room ("TheQuartering Segment: Lawsuit, Twitch Ban"), and a highlight gets a short hook. Ask for something else under **What the chapters and clips should be**. **Clips go to / Full episodes go to** pick the YouTube channels, and a playlist on each that uploads go into. **Clips offered per stream** sets how many chapters and Shorts (highlights) of each stream reach the inbox, best-scored first; raise them as your YouTube upload limit grows. **Shorts** sets the background behind the video in its Shorts (your own 1080×1920 image, or a dimmed blur of the stream) and where the video sits on it and how big, with a preview; the captions follow the video. **Overlay on clips** puts the creator's branding on chapter and highlight videos: a 1920×1080 PNG with a transparent background. If its design goes all the way round a transparent screen in the middle (a TV, say), each clip is shrunk to fit that screen; anything else, like a corner logo, goes on top of the full-size clip. Full episodes get it too only if you switch that on, since it means re-encoding the whole stream. **Without review** sets, for the full episode, chapters and Shorts, whether they wait for **Review**, are rendered straight away (**Render**), or are rendered and uploaded (**Upload**, with the visibility you pick); **Standouts** uploads any clip Claude scores high enough. **Space clips out** spreads the show's clip uploads over the day, and **Thumbnails** makes one for each chapter and full episode (see [Automatic uploads](#automatic-uploads) and [Thumbnails](#thumbnails)). **About the show** gives Claude context; **What the chapters and clips should be** tells Claude what to look for, and it follows that over its defaults. **Links that belong to this show** match pasted links. **Channels to watch** takes `kick.com/name`, `twitch.tv/name`, `youtube.com/@name` or `x.com/name`, with optional filters: keywords (join words with `+` to need all of them), words to skip, minimum length. The worker checks each channel every 10 minutes and queues a stream once it has ended, or a video posted on X as soon as it's seen. Videos already on a channel when you add it are listed with a **Process** button but not processed automatically. To give a channel its own settings, such as an X account's pre-shows, make a show for it and move the channel there under its **Settings**; its link moves too, and what it has already seen stays seen. Your YouTube channels are listed under **Shows → YouTube channels you upload to**, where you connect each one to the YouTube API. |

## Command line

Everything the app does is also available from the command line, e.g. `docker compose run --rm worker <command>`:

| Command | What it does |
|---|---|
| `add "<url>" [--whole/--segment] [--notes "..."]` | Queue a video for the worker, same as the app's **Add video**. |
| `process "<url>" [--notes "..."] [--no-render] [--redo STEP] [--only ID]` | Run the whole pipeline in the foreground without the app; handy for testing prompts. |
| `doctor`, `llm-check` | Check the setup and the Claude Code login. |
| `browser` | On a desktop: open the upload browser (Chrome or Edge with its own profile) to sign in to YouTube or X. |
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
