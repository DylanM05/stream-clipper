# To do

Ideas agreed on but not built yet, newest first.

## The Library: what's left

Since Nov 1, 2026, the Library works through folders of videos on the server (`CLIPPER_HOST_MEDIA_DIR`) and channels' past videos a few a day (monitor.py), and a show's videos can be TV episodes or other videos for the AI (`prompts/segment_episode.md`, `segment_other.md`). Still to do:
- `{episode}` (S05E04, from the file name) in title patterns.
- The file's own subtitles instead of Whisper: exact dialogue, but the editor needs each word's timing.
- **Copyright:** Paramount uses Content ID heavily on South Park. Expect claims or blocks, and strikes from manual takedowns. YouTube usually turns down monetization for channels of lightly edited TV clips ("reused content"). Try it on a throwaway channel first.

## Clip editor: a more professional design

Redesign the clip editor (trimming, the preview, title and description, the Short) so it looks and feels polished, not just functional.

## Back button after approving clips

Make Back behave as expected while approving clips. After approving (or rejecting) a clip in the editor, the app moves on to the next clip, or back to Videos when there's none, so Back can land somewhere unexpected.

## Description footer for each show

A show setting with text added to the end of every upload's description, such as the creators' channel links (Oct 30, 2026). Asking the AI for it in the show's notes doesn't work reliably, and a video posted whole (a pre-show from X) gets the post's own text as its description, which the AI never writes, so the link never gets in.

## People shared between shows

One list of the people the shows feature or talk about: who each is, the names they go by, their real name and what they look like, which several shows can use instead of each describing them in its own notes and thumbnail rules (Oct 30, 2026). The same people are described in three shows now, so a change has to be made three times, and the copies drift apart.

## Learn from how past clips did: what's left

Since Oct 30, 2026, the AI gets each show's track record when it picks a stream's clips (learning.py): how past uploads did against the show's usual, how the owner changed its picks (with the words a moved start or end took in or left out) or turned them down, and the owner's notes. Still to do, roughly most useful first:
- **Where viewers leave:** how much of each clip people watched, and its retention curve, from the YouTube Analytics API, matched to the transcript ("most viewers left when the sponsor read started"). The best teacher for cut points. Connected channels would need to grant one more permission.
- **Subscribers gained per clip** (the same API): which clips grow the channel, not just get views.
- **Topic and people tags:** the AI tags each pick, so the track record can say "segments about X do 2× usual" instead of hoping the AI spots it in titles. Keeping the tags consistent means showing it the ones it used before.
- **Click-through rate:** how often people clicked when YouTube showed the video, from YouTube's reach reports (to be confirmed that the API has it). It separates a good title and thumbnail from a good clip, and could teach the thumbnail ideas too.
- **Comments:** a short summary of what viewers quoted, asked for or complained about (YouTube Data API, cheap on quota).
- **Live chat and viewer clips while picking:** chat spikes in the stream, and the moments viewers clipped themselves on Twitch or Kick. Twitch chat replays can be downloaded; Kick's is less certain.
- **Rumble views:** not read yet.
- **Scores that steer Standouts:** if the AI's high scores haven't beaten the rest, Standouts could warn, or wait for more proof before uploading by itself.
- **How it went up, for the upload timing:** whether the day and time, the gap between a stream's clips, how soon after the stream, the channel or having a thumbnail made a difference. The app already keeps these; they'd steer the upload schedule rather than the picks.

## Setup when the app first starts

The first time the app is opened, it walks you through getting set up and working, instead of leaving that to the README (Oct 30, 2026):
- **The AI:** which one to use (Claude Code, Codex or a model on OpenRouter), signing in to it, and which of its models. You can add, remove or change these later.
- **Whatever else a new setup needs:** for example the first show (what to make, the title pattern, where its videos go), connecting a YouTube channel or signing in to the upload browser, and notifications.

Today the AI is chosen under Videos, and signing in to it means running a command on the server (README, "The AI"). Signing in from the app instead means running each sign-in in the app's container and passing on what it shows: Codex's device sign-in gives a link and a code, and Claude Code's gives a link and a code to paste back. The OpenRouter key would move from `.env` into the app's settings, kept secret. Some setup only lives in `.env` (the app password, the data folder, notifications): move what makes sense into the app, and have the setup say what to set for the rest.

## User experience pass

Go through the whole app, on a phone and on a computer, and make sure everything works properly and is smooth to use (Oct 29, 2026): every flow (adding a video, reviewing clips, editing, uploading, shows and channels, settings), with each step leading to the next properly and nothing confusing or dead-ended.

## Security review

Make sure everything is secure and no secrets are exposed (Oct 29, 2026). Check for vulnerabilities before anything is released: the app's login and sessions; that every API route checks it; the files served under /media (no reaching outside the data folder); uploaded images (backgrounds, overlays); the commands run with input from links and titles (ffmpeg, yt-dlp, the AI's command line); who can reach the upload browser's debugging port and its live view; secrets in .env and the database; and dependencies with known problems.

## Release on GitHub, selfh.st and r/selfhosted

Get the project ready to publish on GitHub, then post it to selfh.st and r/selfhosted (Oct 29, 2026), after the security review:
- **Documentation for someone else's setup:** the README is written around this one server. Add a quick start, a reference for every setting in .env, and screenshots.
- **Nothing private in it:** check the code and its whole git history for keys and tokens, this server's addresses, and the channels and people used in examples and tests. Pick a licence.
- **Generic test data:** the prompts, the app's example texts, the README and the code's comments use made-up shows and people since Oct 30, 2026, but the tests' sample data still names the owner's shows and the people on them (about 250 lines). Swap those for made-up names too.
- **Be upfront about the risky parts:** YouTube Studio uploads (YouTube's terms only allow the API), reading X through a signed-in browser (X's terms), Rumble requests passed off as Chrome, and that clipping someone's streams needs their permission.

## Intro on videos (on hold, Oct 27, 2026)

A short intro (3–5 seconds; longer costs watch time) at the start of each video, set per show like the overlay: upload an intro video, with switches for clips and full episodes.
- **Clips (chapters, highlights):** joined on in the render that happens anyway, so it only adds the intro's own seconds to the encode.
- **Full episodes:** they go up as the original file, with no re-encode. Encode the intro to match each stream (resolution, frame rate, codec, audio) and join the two without re-encoding: seconds of GPU and a few minutes of disk. Re-encoding the whole stream instead would take an estimated 1–2½ hours of GPU per 8-hour stream.
- **Shorts:** off by default. Viewers swipe away in the first second or two, and the show's overlay already brands them.

## Watching subreddits (on hold, Oct 25, 2026)

Watch subreddits for their top videos (livestream fails, mostly) and queue them like videos from a watched channel.


think of rebrand name
make prompts for text spaces more generic.

Get claude to explain and tell me exactly what prompts are being sent through this program to claude

anything thats not generic in the prompts, assist in moving those details over to the specific show prompt, i will give you the current show prompt for that show and we can work on it together.

the aim is to make it generic so when i release it, it works for everyone, but is still very customizable.
