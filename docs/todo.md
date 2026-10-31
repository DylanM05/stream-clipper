# To do

Ideas agreed on but not built yet, newest first.

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

Go through the whole app, on a phone and on a computer, and make sure everything works properly and is smooth to use (Oct 29, 2026).

## Security review

Make sure everything is secure and no secrets are exposed (Oct 29, 2026).

## Release on GitHub, selfh.st and r/selfhosted

Get the project ready to publish on GitHub, then post it to selfh.st and r/selfhosted (Oct 29, 2026).

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