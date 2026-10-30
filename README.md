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

To update after pulling new code: `deploy/deploy.sh .` (or `docker compose build && docker compose up -d`). To have every push to `main` deployed by itself, see [Automatic deploys](#automatic-deploys).

### Disk space

Keep `CLIPPER_HOST_DATA_DIR` on an SSD. Converting, previewing, transcribing and rendering all read or write the whole stream, which is several times slower on a hard drive.

An 8-hour stream takes about 45–60 GB at its peak: the source, the rendered chapters and clips, the preview copy and the audio.

**Cleanup:**
- Each rendered clip is deleted as soon as the worker has uploaded it. A full episode's file is the stream itself, which the other clips still need, so it goes with the stream.
- Once every clip of a stream is uploaded, rejected or an extra, the stream's big files (the video, preview copy, audio and renders) are deleted within a few minutes. To keep them a while in case you want to re-cut something, set a number of days on the Videos screen (`CLIPPER_CLEANUP_AFTER_DAYS`).
- To free a stream's space before then, open it on the Videos screen and tap **Delete this stream's video files**. Its clips that aren't uploaded yet are rejected, since there's nothing left to cut them from.
- A video you don't want at all, such as one a watched channel queued by mistake, has a **Delete** button on the Videos screen (or **Delete this video** on its page). It removes the video with its clips and files, and the channel doesn't queue it again. One that's being worked on needs pausing first, and one with clips on YouTube stays for their history.
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

### Automatic deploys

A push to `main` on your Forgejo is tested, checked and deployed to the server by itself, through Forgejo Actions (`.forgejo/workflows/deploy.yml`). The app's image is built from the pushed commit on the server and the backend tests run inside it, so they run on the same Ubuntu, Python and ffmpeg the app does. Building it also type-checks and builds the phone app. Then the [security checks](#security-checks) run. If all that passes, `deploy/deploy.sh` brings the checkout the stack runs from up to that commit and restarts the stack. Pushes to other branches are only tested and checked.
- **Between uploads:** a restart waits for an upload going up through the upload browser (YouTube Studio or Rumble) to finish, holding new uploads meanwhile, because those can't carry on after a restart. Everything else picks up by itself: videos and renders start their step again, and uploads through the YouTube API carry on where they were. Forgejo stops a job after 3 hours, so a deploy waits at most 2½ hours, then gives up and leaves the running version as it was. Run it again from the **Actions** tab once the upload is done.
- **If it breaks:** after restarting, the deploy checks that the app answers and the worker is running. If not, it shows their last log lines, goes back to the commit it had, and rebuilds and restarts that. It also refuses to deploy over changes made by hand in the server's checkout.
- **Set up, once:**
  1. You need a [Forgejo Runner](https://forgejo.org/docs/latest/admin/actions/) on the same server as the stack. As it comes, it runs `ubuntu-latest` jobs in a container that uses the server's Docker, which is what this needs. It also has to take this repository's jobs, so it should be listed under the repository's **Settings → Actions → Runners**. A runner registered for just one other repository won't be listed: register it for your user or the whole instance instead.
  2. On the server, make sure the folder the stack runs from (the one with `.env`) is a clone of the repository on your Forgejo, made over `http(s)://`, on `main`. The deploy fetches as the folder's owner, but without their SSH keys.
  3. In the repository's **Settings → Actions → Variables**, add `CLIPPER_DEPLOY_DIR` with that folder's full path, such as `/home/you/stream-clipper`.
  4. Push to `main`, and follow the run under **Actions**.
- **How it reaches the stack:** the job's container doesn't have the stack's folder, so `deploy/ci-deploy.sh` runs the deploy in a second container that has it, at the same path. That container runs as the folder's owner, so what git writes there stays theirs, and on the server's network, to check that the app answers. Cancelling the job stops it too.
- **Who can run things on the server:** jobs can use the server's Docker, which is as good as having root on it. So only people you'd give the server to should be able to push to this repository, and registration on your Forgejo should stay closed. Pull requests don't run the workflow.

### Security checks

Every push is also checked by `deploy/scan.sh`. Each scanner is pinned to an exact version and runs from its own image, so the server needs nothing more installed:
- **Secrets** (gitleaks): passwords, tokens and keys committed in the code or anywhere in its history, since one that was deleted later is still in it. The log shows them redacted.
- **Code** (bandit, shellcheck): risky Python, such as a shell command built from input, and mistakes in the shell scripts.
- **Known vulnerabilities** (trivy), on every push and each Monday: in the Python packages as the app's image installs them, in the phone app's packages, and in the upload browser's image, Chromium included. It counts only high and critical ones that have a fix. It also checks the Dockerfiles for risky settings.

A secret or a code finding stops the deploy. A vulnerability doesn't: it shows as a failed **Known vulnerabilities** job, since the running app has it already and a deploy may be what fixes it. (To have vulnerabilities stop deploys too, add `vulnerabilities` to the deploy job's `needs`.) To fix one:
- **A Python package:** raise its minimum version in `backend/pyproject.toml`, adding it there if it comes in through another package. Changing that file also makes the image install every package fresh.
- **A phone app package:** update it in `frontend/` (`npm update <package>`) and commit `package-lock.json`.
- **The upload browser:** rebuild its image from scratch on the server, `docker compose build --pull --no-cache browser && docker compose up -d browser`.

A finding that doesn't apply can be accepted, with a note on why: for bandit, a `# nosec B<number>` comment on the line; for shellcheck, `# shellcheck disable=SC<number>` on the line above; for gitleaks, its fingerprint in `.gitleaksignore`; for trivy, its ID in `.trivyignore`.

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

- **Daily limits:** each Google Cloud project can upload 100 videos a day. YouTube also limits how many videos each channel can upload in 24 hours and doesn't say how many: 10 for the clips channel as of Oct 26, 2026, and it grows as a channel does. Uploads over it fail, and nothing was sent, so retrying can't post twice.
- **Trying again by itself:** an upload that failed for a reason that clears up by itself is queued again at 12:01 AM (in `CLIPPER_TIMEZONE`), for up to 3 nights: the channel's daily limit, YouTube having trouble, or, through YouTube Studio, the upload browser being closed or signed out before the file got there. The channel's limit reset at midnight Eastern when it was hit after an evening of uploads (Oct 25, 2026), so 12:01 AM is the first moment it can go. The API quota resets at midnight Pacific, so uploads stopped by it go again at 12:01 AM Pacific. A failure that would happen the same way again (an expired sign-in, a bad title) isn't retried, and neither is a Studio upload that failed after the file reached Studio, which would post it twice. The upload's page says when it goes again; **Retry** sends it now and **Dismiss** cancels it. Uploading the clip another way, or marking it uploaded, cancels the retry too.
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

### Posting to Rumble too

A channel's uploads can go to Rumble instead of YouTube, or to both. Rumble's upload API needs an access key from its business team, so the worker fills in Rumble's upload page (rumble.com/upload.php) in the upload browser, the way it does YouTube Studio:
- **Set up:** open the upload browser and sign in at rumble.com with the account whose channel the videos go to (an account has one channel there). Then under **Shows → YouTube channels**, set a channel's **Uploads go to** to **Rumble** or **Both**, and pick the Rumble category its uploads go under. A show can choose for itself instead: under its **Clips go to** and **Full episodes go to**, **Post its clips on** and **Post its full episodes on** pick **YouTube**, **Rumble** or **Both** for that show only. Until a show picks, it goes by the channel's setting.
- **Uploading:** shows that upload by themselves post where the show picked, or else where their channel's uploads go. A clip's **Upload** page starts with the same choice, and has **YouTube**, **Rumble** and **Both** to pick something else for that upload. Each site gets its own upload, with its own progress, spacing (**Space clips out** counts each site separately) and retries.
- **What goes up:** the title, description, visibility and category, and as its thumbnail the clip's own (for the 16:9 video), else a frame from a third of the way in. A Short (vertical, 3 minutes or less) becomes a Rumble Short by itself. The rendered file is kept until it's up on every site it was asked to go to.
- **Licence:** every upload goes up under Rumble's **Rumble Only** licence: non-exclusive, like YouTube, and what Rumble picks by default. The two **Video Management** licences make Rumble the exclusive agent for the video, which only the creators could agree to. The uploader also confirms there's no exclusive agreement with anyone else and agrees to Rumble's terms, as the page asks; make sure both are true of the creators' permission.
- **When it fails:** a failed upload saves a screenshot (**What Rumble showed**). One that fails before it's submitted is tried again by itself the next night. One that fails after it's submitted isn't, because it may be on Rumble already: check the channel there before retrying. When Rumble changes its page, `clipper rumble-upload-check` (and `--html`) shows what it now asks for, and `backend/clipper/rumble_upload.py` needs updating to match.
- **Not yet:** Rumble videos have no view counts in **Stats**, and **Use on YouTube** is for YouTube only.

## Thumbnails

A clip's Upload page can make a thumbnail for its 16:9 video, and a show can make one for every chapter and full episode once it's rendered (**Thumbnails** under the show). Claude reads what's said in the video and comes up with a funny idea that's about what actually happens, starring the people in it, plus a few words of text if they help. An image model on [OpenRouter](https://openrouter.ai) draws it, with frames of the video as references so the hosts and guests look like themselves. Type your own idea in the box to steer it, and tap **Make another** until you like one. Under the picture, **People** says who's in it and where each likeness came from.

**What every thumbnail must have** (under the show's **Thumbnails**) sets rules for all of a show's thumbnails, which Claude follows over its own defaults: words that must be on each one (e.g. `Must have the words "Kino Casino Preshow" in a banner along the bottom`), a style, colours, something that's always in it. Required words don't count towards Claude's own few words of text. The image model draws the words, so check the spelling on each one.

**The prompt it was drawn from** (under the picture) is Claude's prompt for the image model, kept with the thumbnail. Change anything in it, say the joke, the style or the text, and **Draw it with your changes**: the same people go along again with fresh frames, without Claude starting over. That works after the video is uploaded too, as long as the stream's files are still there; then **Use on YouTube** puts it on the video. If a new try fails, the clip keeps the thumbnail it had.

**People the video talks about** (another streamer, a YouTuber, a celebrity) can be in it too. Claude names them, and the app looks up a photo of each, up to three: the lead picture of their Wikipedia article, or else the picture of the YouTube channel a search for them finds first. The photo only counts if the article or channel has their name, so a search that turns up someone else is ignored. The image model gets those photos as references along with the frames. Anyone with no photo is drawn from Claude's description of them, which is hit and miss. Private people (callers, chatters, someone in a news story who isn't famous) are never drawn.
- The YouTube search needs `CLIPPER_YOUTUBE_API_KEY` or a channel connected to the API. Each search costs 100 units of the project's 10,000-unit daily quota; it's only used when Wikipedia has nothing.
- Some image models won't draw well-known people and send nothing back; the thumbnail then fails with a note saying so. Try again, steer it with your own idea, or switch `CLIPPER_THUMBNAIL_MODEL`.

- **Setup:** make an account at openrouter.ai, add some credit, create a key under **Keys** and set it as `CLIPPER_OPENROUTER_API_KEY` in `.env`, then `docker compose up -d`. Each thumbnail costs a few cents there (see the model's page), plus a small Claude call on your plan. `CLIPPER_THUMBNAIL_MODEL` picks the model: any OpenRouter image model that takes reference images. The default is Google's Gemini 2.5 Flash Image.
- **Who looks like who:** Claude only gets the transcript, not the frames, so it goes by names. Under the show's **About the show**, say what the regulars look like (e.g. "Don wears a red cap and glasses") and which names belong to the same person (e.g. "Jer is Jeremy Hambly, who runs TheQuartering"). The photo lookup needs the real name: "Jer" on its own could be any Jeremy, so it finds nothing.
- **On YouTube:** an upload through the API sets the thumbnail right after the video goes up, and a video waits up to 30 minutes for a thumbnail that's still being made. For a video already on a channel connected to the API, **Use on YouTube** puts a new one on: the video the app uploaded, or, for one you uploaded yourself, the one whose link you gave when marking it uploaded (the 16:9 video's link, not the Short's). YouTube only allows custom thumbnails on channels verified with a phone number (youtube.com/verify). For channels uploading through YouTube Studio, **Download** it and add it in Studio. Shorts don't use custom thumbnails, so they don't get one.
- **Keep it fair:** the thumbnails show real people, including people who never agreed to be on the channel. Claude's instructions keep them about what the video actually says, cartoonish rather than like a real photo, and free of anything sexual or demeaning or any made-up scandal, but look at each one before it goes out. A thumbnail that could pass for a real photo of someone doing something they didn't do can get a video taken down for being misleading, or worse. Check the creators are happy with AI thumbnails of them too.

## How uploads are doing

The **Stats** screen lists every upload the app made with its views, likes and comments, newest or most viewed first. The worker reads them every few hours for each upload's first month, and keeps the views a day and a week after it went up so newer and older videos can be compared fairly.
- **Does the score mean anything?** The top of the screen shows the median views of chapters and highlights by the score Claude gave them. If the 0.9-and-up videos don't do better than the rest, Standouts is just uploading at random; raise it or turn it off.
- **Better titles:** the titles of each show's most-viewed clips go back to Claude when it picks the next stream's clips, as examples of what lands with the channel's viewers.
- **Weekly summary:** on Monday mornings (9 AM in `CLIPPER_TIMEZONE`) a notification lists the past week's uploads, their views so far and the five most viewed.
- **Setup:** uploads to channels connected to the YouTube API are read with their sign-in. To read uploads to other channels, connect at least one channel or, better, create an API key in the same Google Cloud project (**APIs & Services → Credentials → Create credentials → API key**, restricted to the YouTube Data API v3) and set it as `CLIPPER_YOUTUBE_API_KEY`. Reading costs 1 unit of the project's 10,000-unit daily quota per 50 videos.
- **What's counted:** uploads the worker made. Ones from before Oct 26, 2026 get their views so far but usually no day-one or week-one numbers. Clips you uploaded by hand and marked as uploaded aren't read.

## Watching Rumble channels

Add `rumble.com/c/name` (or `rumble.com/name`) under a show's **Channels to watch**, or paste a Rumble video's link on **Videos**. Rumble sits behind Cloudflare's bot check, which turns yt-dlp away, so for Rumble only, yt-dlp passes itself off as Chrome (its curl-cffi add-on, already in the image for Kick). That gets round the check on purpose; it was chosen knowingly on Oct 26, 2026.
- **How it's watched:** Rumble builds a channel's list of videos with JavaScript in the browser, so a plain download of the page doesn't have it. Every 10 minutes the worker opens the channel's **Live** tab (`rumble.com/c/NAME/livestreams`) in the upload browser, the same one that reads X, and takes the video links once the list has filled in. The browser has to be running, as it already is for X and Studio uploads. Each video's details then come from its own player, which its page names in its description of its own video for search engines: the first player on a Rumble video's page is the channel's newest or featured video's, which yt-dlp and Rumble's oEmbed API both take, so they report and download the wrong stream. If a page stops describing its video that way, reading it fails and says so, rather than guess. The list doesn't say which video is live or how long each is, so the worker reads those of the newest 5 it hasn't settled yet. Every Rumble page also links other channels' live streams, so a video only counts if Rumble says it belongs to the channel you added (by its channel link, else its name); other channels' are ignored, and not read again until the worker restarts. A stream is queued once it's no longer live and its replay is the same length on two checks in a row, like the other platforms. - **Check it works:** on the server, `docker compose exec worker clipper channel-check rumble.com/c/NAME` lists the videos the worker sees, with LIVE or their length, and what it skipped as another channel's. `--links` also lists every video link the browser found on the page, for working out what Rumble changed if it finds nothing. A 429 means Rumble thinks the server asked too often, which usually clears within the hour; a 403 means Cloudflare caught on and Rumble is blocked until a newer yt-dlp gets through. The channel's card shows either.

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
| **Clip editor** | Tap a clip to open it. Drag the handles on the strip under the video, or fine-tune with **−1s / −0.2 / Here / +0.2 / +1s**; every change plays the few seconds around the new cut. **Last 5s** checks that nothing is cut off, and **« 10s / « 5s / 5s » / 10s »** jump back or ahead while you watch. Tap a transcript line to hear it, or use its **Start** and **End** buttons to cut there. Edit the title and description, and turn the vertical Short on or off. The volume slider only changes the preview, not the clip; on iPhones it's a mute button, because iOS only allows the hardware buttons. Parts cut out of the middle on a computer (see below) are striped on the strip, playing jumps over them, and **Put them back** undoes that. **Approve** takes you straight to the next clip. |
| **Videos** | Paste a link and it's matched to its show, which sets the mode, notes and title pattern. You can override the mode with **Clips**, **Full** or **Both**, and add notes for Claude. Shows each video's progress. Failed videos can be retried; videos that hit your Claude usage limit retry on their own once it resets. Downloads are capped at 50 Mbit/s by default so the rest of your internet stays usable: about an hour for an 8-hour stream. Tap **Change** under the form to adjust it (0 = no limit). **Full speed hours** lift the cap at times nobody needs the connection, such as right after overnight streams end (in `CLIPPER_TIMEZONE`); a download that's running speeds up or slows down within a few seconds when they start and end. A video's clips reach the inbox, and the ones a show takes without review start rendering, as soon as Claude has picked them; the preview copy for watching them in the app can still be on its way, and the clip page says so. The lines below it show free disk space, when finished streams' files are deleted, and where notifications go. The switches above them pause parts of the work, and each video in progress has its own **Pause** (see [Pausing](#pausing)). Open a video to see all its clips, including the **Extras** that didn't make the show's best few (open one and approve it to use it), and to delete its video files early. |
| **Upload** | Clips being rendered, clips ready to upload, and uploaded ones. A clip waiting to render can be paused or sent back **To inbox**; a ready one can go back **To inbox** or be **Deleted**, which deletes its render too. Tap **Upload** on a ready clip: pick its channel, then either **upload automatically** (Public, Unlisted or Private; the worker does it through the YouTube API, or YouTube Studio for channels not connected to it, see below) or do it by hand. By hand means sharing the file straight to the YouTube app from a phone, or downloading it on a computer (it's named after the title, so Studio fills that in). Copy the title and description, then **Mark as uploaded**. If an uploaded video is broken on the site, for instance YouTube says "An error occurred" wherever it's played, **Render and upload again** (under **Done?**) renders the clip afresh and uploads it again wherever the app uploaded it, with the same visibility; delete the broken one there yourself. It needs the stream's files, so it works until cleanup deletes them. New cut points render the clip again; a new title or channel doesn't. The same page makes the video's **Thumbnail** (see [Thumbnails](#thumbnails)) and shows each upload's views. Once its upload is started, a clip moves to **Scheduled**, which says what's happening with it: uploading, going up next, scheduled for a time (it waits for its turn, see **Space clips out**), or failed and trying again later. A failed one goes back to **Ready to upload**, marked as failed. |
| **Stats** | Views, likes and comments of every upload, and whether Claude's score predicts them (see [How uploads are doing](#how-uploads-are-doing)). |
| **Shows** | One per show. **What to make** is Clips, Full episode or Both. **Title patterns** (one for clips, one for full episodes) use `{date}` for the stream date (e.g. Oct 23, 2026: the day it started in the creators' time zone, `CLIPPER_TIMEZONE`, Eastern by default, with a stream starting before 6 AM counting as the night before's) and `{title}` for the part Claude writes; Claude gets however many characters are left of YouTube's 100, but keeps its part short: a chapter leads with its segment, then the main topic or two if there's room ("TheQuartering Segment: Lawsuit, Twitch Ban"), and a highlight gets a short hook. Ask for something else under **What the chapters and clips should be**. **Clips go to / Full episodes go to** pick the YouTube channels, and a playlist on each that uploads go into, and under each, whether that show's uploads there go to YouTube, Rumble or both (until you pick, as the channel is set; see [Posting to Rumble too](#posting-to-rumble-too)). **Clips offered per stream** sets how many chapters and Shorts (highlights) of each stream reach the inbox, best-scored first; raise them as your YouTube upload limit grows. **Shorts** sets the background behind the video in its Shorts (your own 1080×1920 image, or a dimmed blur of the stream) and where the video sits on it and how big, with a preview; the captions follow the video. **Overlay on clips** puts the creator's branding on chapter and highlight videos: a 1920×1080 PNG with a transparent background. If its design goes all the way round a transparent screen in the middle (a TV, say), each clip is shrunk to fit that screen; anything else, like a corner logo, goes on top of the full-size clip. Full episodes get it too only if you switch that on, since it means re-encoding the whole stream. **Without review** sets, for the full episode, chapters and Shorts, whether they wait for **Review**, are rendered straight away (**Render**), or are rendered and uploaded (**Upload**, with the visibility you pick); **Standouts** uploads any clip Claude scores high enough. **Space clips out** spreads the show's clip uploads over the day, and **Thumbnails** makes one for each chapter and full episode (see [Automatic uploads](#automatic-uploads) and [Thumbnails](#thumbnails)). **About the show** gives Claude context; **What the chapters and clips should be** tells Claude what to look for, and it follows that over its defaults. **Links that belong to this show** match pasted links. **Channels to watch** takes `kick.com/name`, `twitch.tv/name`, `youtube.com/@name`, `rumble.com/c/name` or `x.com/name` (see [Watching Rumble channels](#watching-rumble-channels)), with optional filters: keywords (join words with `+` to need all of them), words to skip, minimum length. The worker checks each channel every 10 minutes and queues a stream once it has ended, or a video posted on X as soon as it's seen. Videos already on a channel when you add it are listed with a **Process** button but not processed automatically. Each channel's list is newest first and shows when each video was posted or streamed (roughly, on YouTube, which only says "3 days ago"); videos listed before dates were kept get theirs on the next check, except on Rumble. To give a channel its own settings, such as an X account's pre-shows, make a show for it and move the channel there under its **Settings**; its link moves too, and what it has already seen stays seen. Your YouTube channels are listed under **Shows → YouTube channels you upload to**, where you connect each one to the YouTube API. |

**On a computer** (a window at least 1024 pixels wide), the same address lays the app out for the big screen; phones and tablets keep the layout above. A sidebar takes the place of the tab bar, with the switches that pause the work and how full the disk is always in view. **Overview**, at the top of the sidebar, has everything at a glance: how many clips wait for review, rendering and upload; the upload queue in order, with when each one goes up; anything that failed or is held up; the videos being worked on; the latest uploads' views and the month's most viewed; and how each channel uploads. Lists spread into two columns on wide screens, a video's page says where it's from and where its clips stand, and a clip's **Upload** page puts uploading automatically beside uploading by hand. The clip editor works like an editing app (below). **Stats** adds totals and medians, which kind of upload does best, and a table of every upload that sorts by any of its numbers.

**The clip editor on a computer** has the video with its controls, the clip's details and transcript beside it, and a timeline across the bottom with stills of the stream, its waveform and its transcript (single words once you zoom in). Drag the purple edges to trim, or the bar above the clip to move all of it, or type a start and end into **In** and **Out**. To take a part out of the middle, drag across the timeline to select it (or double-click a word, or use **Cut** on a transcript line) and press **X**: the finished video jumps over it, the preview does too, and a Short's captions follow. Click a cut to move it or drag its edges, **Hear the join**, or **Put back**. Edges snap to words, lines, cuts and the playhead; **S** turns that off, and holding Alt while dragging ignores it. Zoom with ⌘ or ctrl and the scroll wheel (or a pinch), scroll sideways to pan, and drag the bar under the timeline to move along the whole stream. Hover over the ruler, or drag along it, to see a still of the moment under the pointer, there before the video gets there. Most things have a key: Space plays, J, K and L shuttle, ← and → step a frame, I and O set the start and end at the playhead, ⌘Z undoes, ⌘S saves and ⌘↵ approves. Press **?** for the full list. Full episodes go up as the stream is, so they can't have parts cut out. Once a stream is ready, the worker makes what the timeline shows of it zoomed out, once: a still every 10 seconds and the soundtrack's loudness, so zooming out doesn't have the server read through the whole stream while you wait.

**Your own clips:** on a video's page, **Make a clip from any part of it** opens the editor on the whole stream. Find the moment by searching the transcript or scrubbing, set the start and end, pick **Highlight** (with an optional Short) or **Chapter**, and write the title, which goes into the show's pattern with the stream's date, like Claude's do. Then **Save to inbox** or **Approve** it. On a computer, **New clip from this** makes one from a selection in any clip's editor. Clips you make are marked as yours: they have no score from Claude, so **Stats** leaves them out of whether the score predicts views.

## Command line

Everything the app does is also available from the command line, e.g. `docker compose run --rm worker <command>`:

| Command | What it does |
|---|---|
| `add "<url>" [--whole/--segment] [--notes "..."]` | Queue a video for the worker, same as the app's **Add video**. |
| `process "<url>" [--notes "..."] [--no-render] [--redo STEP] [--only ID]` | Run the whole pipeline in the foreground without the app; handy for testing prompts. |
| `doctor`, `llm-check` | Check the setup and the Claude Code login. |
| `rumble-upload-check [--html]` | Show what Rumble's upload page asks for, as the upload browser sees it, for when Rumble changes it (see [Posting to Rumble too](#posting-to-rumble-too)). Nothing is uploaded. |
| `timing-check [ID...] [--fix]` | Check that finished videos' transcripts (the 5 newest, or these ids) line up with the video. Videos transcribed before the sound kept the video's timing have their words, and so their Shorts' captions, timed early after any point where the stream dropped and came back; `--fix` puts the transcript right, moves the clips you haven't trimmed by hand to where Claude meant them, and renders again the affected ones that were rendered or approved (taking their queued uploads out of the queue). |
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
