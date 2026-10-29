# To do

Ideas agreed on but not built yet, newest first.

## User experience pass

Go through the whole app, on a phone and on a computer, and make sure everything works properly and is smooth to use (Oct 29, 2026).

## Security review

Make sure everything is secure and no secrets are exposed (Oct 29, 2026).

## Back button after approving clips

After approving clips one after another in the clip editor, Back steps back through every clip approved instead of to where you started, so you have to go back through all of them. It should go straight back (Oct 29, 2026).

## Release on GitHub, selfh.st and r/selfhosted

Get the project ready to publish on GitHub, then post it to selfh.st and r/selfhosted (Oct 29, 2026).

## Fix uploading to Rumble

Uploads to Rumble go through its upload page in the upload browser (rumble_upload.py). The first real one, a Short on Oct 27, 2026, failed with "Please select or drag a video"; commit 85b92c5 waits for the page to settle and chooses the file again if the upload doesn't start. The next, a full show on Oct 28, failed before uploading anything with "Cannot transfer files larger than 50Mb to a browser not co-located with the server": Playwright sent the file's bytes over its connection to the browser, which it refuses past 50 MB. Uploads now hand the browser the file by its path (studio.connect, is_local), which both containers see under /data; YouTube Studio uploads had the same limit and get the same fix. Still to do: deploy it, retry that upload (it also tries again by itself at 12:01 AM), and if it fails again, go by what the new error says.

## Intro on videos (on hold, Oct 27, 2026)

A short intro (3–5 seconds; longer costs watch time) at the start of each video, set per show like the overlay: upload an intro video, with switches for clips and full episodes.
- **Clips (chapters, highlights):** joined on in the render that happens anyway, so it only adds the intro's own seconds to the encode.
- **Full episodes:** they go up as the original file, with no re-encode. Encode the intro to match each stream (resolution, frame rate, codec, audio) and join the two without re-encoding: seconds of GPU and a few minutes of disk. Re-encoding the whole stream instead would take an estimated 1–2½ hours of GPU per 8-hour stream.
- **Shorts:** off by default. Viewers swipe away in the first second or two, and the show's overlay already brands them.

## Watching subreddits (on hold, Oct 25, 2026)

Watch subreddits for their top videos (livestream fails, mostly) and queue them like videos from a watched channel.
