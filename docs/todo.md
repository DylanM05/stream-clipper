# To do

Ideas agreed on but not built yet, newest first.

## Fix uploading to Rumble

Uploads to Rumble go through its upload page in the upload browser (rumble_upload.py). The first real one, a Short on Oct 27, 2026, failed with "Please select or drag a video": the page made its thumbnails from the file but never started uploading it, likely because the file was chosen before the page's upload script was ready. Commit 85b92c5 waits for the page to settle and for the upload's percentage to show, chooses the file again once if nothing starts, and otherwise names the page's script errors and failed requests in the error. Still to do: deploy it, retry that upload (or a private test one), and if it fails again, go by what the new error says.

## Intro on videos (on hold, Oct 27, 2026)

A short intro (3–5 seconds; longer costs watch time) at the start of each video, set per show like the overlay: upload an intro video, with switches for clips and full episodes.
- **Clips (chapters, highlights):** joined on in the render that happens anyway, so it only adds the intro's own seconds to the encode.
- **Full episodes:** they go up as the original file, with no re-encode. Encode the intro to match each stream (resolution, frame rate, codec, audio) and join the two without re-encoding: seconds of GPU and a few minutes of disk. Re-encoding the whole stream instead would take an estimated 1–2½ hours of GPU per 8-hour stream.
- **Shorts:** off by default. Viewers swipe away in the first second or two, and the show's overlay already brands them.

## Watching subreddits (on hold, Oct 25, 2026)

Watch subreddits for their top videos (livestream fails, mostly) and queue them like videos from a watched channel.
