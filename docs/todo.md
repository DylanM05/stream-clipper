# To do

Ideas agreed on but not built yet, newest first.

## Clip editor: a more professional design

Redesign the clip editor (trimming, the preview, title and description, the Short) so it looks and feels polished, not just functional.

## User experience pass

Go through every flow in the app (adding a video, reviewing clips, editing, uploading, shows and channels, settings) and make sure each step leads to the next properly, with nothing confusing or dead-ended.

## Back button after approving clips

Make Back behave as expected while approving clips. After approving (or rejecting) a clip in the editor, the app moves on to the next clip, or back to Videos when there's none, so Back can land somewhere unexpected.

## Security review

Check for vulnerabilities before anything is released: the app's login and sessions; that every API route checks it; the files served under /media (no reaching outside the data folder); uploaded images (backgrounds, overlays); the commands run with input from links and titles (ffmpeg, yt-dlp, Claude Code); who can reach the upload browser's debugging port and its live view; secrets in .env and the database; and dependencies with known problems.

## Release on GitHub, selfh.st and r/selfhosted

Prepare it for others to run, with documentation, after the security review:
- **Documentation for someone else's setup:** the README is written around this one server. Add a quick start, a reference for every setting in .env, and screenshots.
- **Nothing private in it:** check the code and its whole git history for keys and tokens, this server's addresses, and the channels and people used in examples and tests. Pick a licence.
- **Be upfront about the risky parts:** YouTube Studio uploads (YouTube's terms only allow the API), reading X through a signed-in browser (X's terms), Rumble requests passed off as Chrome, and that clipping someone's streams needs their permission.

## Fix uploading to Rumble

Uploads to Rumble go through its upload page in the upload browser (rumble_upload.py). The first real one, a Short on Oct 27, 2026, failed with "Please select or drag a video": the page made its thumbnails from the file but never started uploading it, likely because the file was chosen before the page's upload script was ready. Commit 85b92c5 waits for the page to settle and for the upload's percentage to show, chooses the file again once if nothing starts, and otherwise names the page's script errors and failed requests in the error. Still to do: deploy it, retry that upload (or a private test one), and if it fails again, go by what the new error says.

## Desktop layout

A layout for the app on a computer, keeping the phone layout as it is, at the same address. It could show more on a big screen, such as stats and an overview of the upload queue. Everything has to keep working the same on both. Planned next, after Rumble uploads are working (Oct 2026).

## Intro on videos (on hold, Oct 27, 2026)

A short intro (3–5 seconds; longer costs watch time) at the start of each video, set per show like the overlay: upload an intro video, with switches for clips and full episodes.
- **Clips (chapters, highlights):** joined on in the render that happens anyway, so it only adds the intro's own seconds to the encode.
- **Full episodes:** they go up as the original file, with no re-encode. Encode the intro to match each stream (resolution, frame rate, codec, audio) and join the two without re-encoding: seconds of GPU and a few minutes of disk. Re-encoding the whole stream instead would take an estimated 1–2½ hours of GPU per 8-hour stream.
- **Shorts:** off by default. Viewers swipe away in the first second or two, and the show's overlay already brands them.

## Watching subreddits (on hold, Oct 25, 2026)

Watch subreddits for their top videos (livestream fails, mostly) and queue them like videos from a watched channel.
