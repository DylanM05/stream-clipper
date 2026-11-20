# Notification switches

## Why

The Discord channel is too noisy. The user wants to turn off kinds of notification they find they don't need.

## Goals

- Six on/off switches, one per kind of notification, in Settings → Notifications. All on by default, so nothing
  changes until one is turned off.
- Each notification carries a fixed kind, set in code where it's sent; sending skips a kind that's switched off.

## Non-goals

- Per-show settings, quiet hours, routing kinds to different channels.
- Separate switches for Discord and ntfy (the switches apply to every service; only Discord is in use).

## The kinds

| Kind | Notifications |
|------|---------------|
| `review` | a stream is ready; a compilation to review |
| `new` | new on a watched channel |
| `uploaded` | uploaded, with its link |
| `failure` | processing failed, render failed, upload failed, can't check a channel, waiting for disk space, not uploaded (no YouTube channel set) |
| `heads_up` | waiting for the AI, no intro, only one clip kept |
| `digest` | the Monday YouTube summary |

"Not uploaded: no YouTube channel set" is under `failure` because it needs the user to act (our call).

## Approaches

- **A kind on each notification, checked when sending (chosen).** The user asked for it, and it's exact.
- Filter by tone: rejected, since failures and heads-ups share the `bad` tone.
- Settings per service: more than needed for now.

## Design

- `Note` gets a `kind` field. `notify.send` checks the switch for that kind and skips it when it's off.
- Switches are stored as app settings (like pause and cleanup days), so they change without editing `.env` or
  restarting.
- "Send a test" always sends, whatever the switches say.
- When the digest is off, the Monday summary is skipped entirely, and the week isn't marked as sent.
- Settings → Notifications shows the six switches under "Sent to".

## Left to the builder

The exact setting keys, the API shape, and the switch labels, following the existing settings code.

## Tests

Each notification has the right kind; a kind that's off isn't sent; the switches save and load.
