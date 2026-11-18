# Compilation-only streams: approving keeps a clip for the compilation

2026-11-17. Builds on Compilations (PR #2, merged into main) and the edit-bay redesign (PR #1).

## Intent

For a show that makes compilations, the owner still reviews the AI's clips one by one, but the point of the stream is
the compilation, not the clips. Today approving a clip renders it and puts it on the Upload page, and the compilation
comes along as one more video. The owner wants it the other way round:

- Approving a clip **keeps it for the compilation**. It is not rendered or offered for upload by itself.
- A clip goes up by itself only when the owner says so, clip by clip, while reviewing ("Keep and post it by itself too").
- Once the stream's clips are reviewed, the app makes the compilation, and that is what gets uploaded.
- The AI keeps learning from the review, **including what the owner cuts out**, both from clips and from the
  compilation itself.

The flow should feel finished: at every point the owner can see what the compilation will contain, what's left to
review, and what happens next.

## Goals

1. In a stream that makes a compilation, the inbox's main approve action is **Keep**. A kept clip goes into the
   compilation and nowhere else.
2. A second action, **Keep and post it by itself too**, on the inbox card and in the editor, keeps it and also renders it
   and puts it on Upload, as approving does today.
3. The stream's compilation is made once none of its clips is left to review, from the kept clips (and the ones posted
   too), and follows the show's existing "Without review → Compilation" choice: to the inbox for review, rendered, or
   rendered and uploaded.
4. Clips kept for the compilation never render on their own, never show on Upload, and don't count toward the Upload
   badge.
5. What the owner trims or cuts out of a kept clip teaches the AI exactly as it does for an approved clip today. What
   they cut out of the compilation in the editor is counted against the clip each cut falls inside, so it reads like a
   cut made on that clip.
6. The owner can see the compilation take shape: a strip on each stream in the inbox and on the stream's page showing
   which parts of the stream are kept, what's still to review, and how long it runs so far.

## Non-goals

- Shows without compilations: nothing changes for them.
- Full episodes: they can't have parts cut out today, and that stays.
- The compilation's own views on YouTube don't feed the AI's track record of its picks (that section is per pick).
- Parts the owner puts back into the compilation (removing one of the gaps between clips, or moving its start earlier)
  aren't learned from. Only what they cut out is.
- Streams processed before this ships, from shows without compilations, aren't converted.

## Anti-goals (would make this a failure even if it works)

- A kept clip renders or shows up on Upload by itself because some path still treats it as "approved".
- An existing show field is lost, renamed or reset (the redesign's condition 1: change only layout and wording around
  ShowForm's inputs, keep formFields / BLANK_SHOW / the save body as they are).
- "What the AI learns from past clips" stops reaching the AI that picks clips (condition 2: the worker test that
  `learning.track_record` reaches `segment.run` must still pass).
- The stream's files are cleaned up while a kept clip could still be needed for a compilation.

## Constraints

- Backend: FastAPI + SQLModel on SQLite. New columns are added by `db._add_missing_columns` on start, so a new field with
  a default needs no migration script.
- Frontend: React + Tailwind v4, edit-bay tokens (index.css @theme): page #1F2023, panels #2A2C30, chalk #ECE9E2,
  graphite #9A9CA3, mark yellow #F0B429 only for in/out points, selection, the one main action and focus; red = reject,
  green = approve/uploaded. Archivo (condensed for titles), Martian Mono for timecodes only. Sentence-case labels.
- Mockups live in `../design-mockups/edit-bay.html` (outside the repo). New screens get added there too.
- Backend tests run with `CLIPPER_MIN_FREE_GB=1` on the owner's Mac.
- PR #2 may already be deployed (main deploys on push). If a show already has compilations turned on, its streams in
  progress move to the new flow when this ships (see Migration).

## Approaches

**Marking a clip as kept for the compilation**

- **A new clip status, `kept` ("In compilation" in the app). Picked.** It sits beside `approved`: reviewed and wanted,
  but not rendered. "Keep and post it by itself too" sets `approved` as today. Every place that lists clips by status
  handles `kept` explicitly, so the render queue and Upload ignore it without any extra check.
- Keep `approved` and add a "post by itself" flag. Fewer states, but the render queue, Upload, the work strip, the
  summary counts and cleanup all read `approved` today, and each would need the flag; missing one renders a kept clip.
  Rejected for that risk.

**Learning from cuts in the compilation**

- **Count each cut against the clip it falls inside. Picked.** A cut at 1:02:10 in the compilation counts as an edit to
  the kept clip that covers 1:02:10, and the AI reads it in the same "Picks they changed" lines. The gaps between clips,
  which the app cut itself, are left out.
- A separate "cuts in the compilation" section. Simpler, but the AI sees cuts with no link to the pick they changed, so
  each teaches it less. Rejected.

**Where the owner says "post this one too"**: clip by clip while reviewing (picked by the owner), rather than a show
setting or both.

## The design

### Words the app uses

| Thing | In the app |
| --- | --- |
| Status `kept` | "In compilation" |
| Main approve action in a compiling stream | "Keep" |
| Second action | "Keep and post it by itself too" (short form on tight buttons: "Keep and post") |
| Toast after Keep | "Kept for the compilation" (Undo) |
| Toast after Keep and post | "Kept. It will also render to post by itself." (Undo) |
| Taking a posted clip back to compilation-only | "Compilation only" → toast "It stays in the compilation and won't be posted by itself" |
| Rebuilding a compilation | "Rebuild from kept clips" |

The action keeps its name through the flow: "Keep" makes "Kept", "Rebuild" makes "Rebuilt".

### Data

- `Vod.makes_compilation: bool = False` (new). Set when the stream is processed, from its show's `compilation`, at the
  same place `compilation_due` is set today (worker.py, `status="ready"`). It says "approving here means keep" for the
  stream's life, so turning the show's setting off or on later doesn't change streams already processed. (The existing
  copy already says only streams processed after you turn it on get one.) `compilation_due` keeps its meaning: the
  compilation is still to be made.
- Clip status `kept` (new value in `CLIP_STATUSES`, the `ClipStatus` type and `CLIP_STATUS` labels in ui.tsx).
- `ClipRecord.picked_cuts: str = ""` (new). For a compilation: the gaps the app cut between clips when it built it, as
  JSON like `cuts`. With `picked_start`/`picked_end` (now also set on a compilation, to the plan's start and end) it
  records the compilation as the app first made it, which is what the owner's edits are compared against, both for
  learning and for "the kept clips changed since it was made".
- `compilation.INCLUDED` gains `kept`.

### Server behaviour

**Processing.** In a stream with `makes_compilation`, an offered pick the show takes without review (auto_chapters /
auto_shorts not "review", or a score at least auto_upload_score) becomes `kept`, not `approved`. Nothing a pick does
without review posts it by itself; posting by itself is always the owner's choice on that clip. The "Ready" notification
says "N clips to keep or reject for the compilation".

**Keep** (`POST /api/clips/{id}/keep`, new). Only for a chapter or highlight of a stream with `makes_compilation`
(422 otherwise). Allowed from `proposed`, `extra`, `rejected`, and from `approved`/`rendering`/`rendered`/`failed`
("Compilation only": its render and queued uploads are discarded with `discard_render`, as restore does). Refused from
`uploaded` (409, "It's on YouTube already. It stays in the compilation.") and once the stream's files are deleted (409,
as approve). Sets `kept`, `reviewed_at`, `approved_in_review = True`.

**Approve** (existing) is unchanged, and also accepted from `kept`. In a compiling stream it is "Keep and post it by
itself too": the clip is in the compilation (INCLUDED has `approved` and on) and renders for Upload. A kept highlight
makes no Short; one that's posted too renders its Short as today.

**Restore / reject** work from `kept` as from any status (nothing to discard).

**Making a clip in the editor** (`POST /api/vods/{id}/clips`): in a compiling stream the new-clip editor offers "Keep"
and "Keep and post"; the body gets `keep: bool = False`, which creates it as `kept`.

**Whenever a pick of a compiling stream changes status** (keep, approve, restore, reject, made in the editor) and the
stream has no compilation yet, `compilation_due` is set again, so a stream that had too few kept clips gets one once
there are enough.

**compile_next** (existing) as today, with `kept` included, plus:
- It sets `picked_start`, `picked_end` and `picked_cuts` on the compilation it makes.
- It waits until the stream's last review decision is at least 10 s old, so the inbox's Undo can take it back first.
- With only one clip included, there's no compilation (MIN_CLIPS). Nothing is posted by itself: the clip stays `kept`,
  and a notification says "Only one clip was kept from {stream}. Keep another, or post this one by itself." linking to
  the stream's page, where "Post it by itself" (approve) is offered. The notification goes only while that clip is
  still only kept, so posting it doesn't send another. Keeping another later sets the stream due again (below), and its
  compilation is made then.
- With none included, nothing happens, as today.

**Automatic uploads in a compiling stream.** A pick there is never uploaded by the show's "Without review" rules for
clips and Shorts or by its score threshold (those now mean "keep without review"). A pick the owner chose to post too
follows Show.upload_approved, as anything else the owner approves. The compilation follows auto_compilation and
upload_approved as today.

**The compilation changed since it was made.** A compilation is *out of date* when `compilation.plan` on the stream's
clips now gives a different start, end or set of gaps than its `picked_*`. ClipOut reports it (below). Nothing rebuilds
by itself, so a compilation never changes under the owner while they review or edit it.

**What the owner removed from a compilation** is one interval set, used by both Rebuild and learning: the stretch
between `picked_start` and a later `start`, between an earlier `end` and `picked_end`, and `cuts` minus `picked_cuts`
(an interval difference, since `cuts.tidy` merges a new cut touching a gap into it).

**Rebuild** (`POST /api/clips/{id}/rebuild`, new). Compilation only; refused once uploaded or the files are deleted
(409). Recomputes the plan from the clips now; with fewer than two, 409 "Only one clip is kept now. Post it by itself,
or reject the compilation." Then takes the parts the owner removed back out of the new plan, so their cuts survive
wherever those parts are still in it, and stores the result as its start, end and cuts, with the plan's own as
`picked_*` (so learning still sees the owner's cuts). Its description's chapter list is written again; its title,
channel and playlist stay. Its render is discarded, and it goes to `proposed` or `approved` as the show's
auto_compilation says (as compile_next does). The button confirms: "Rebuild with the clips kept now? Its description is
written again, and the parts you cut out stay out."

**Cleanup.** `kept` counts as done (CLIP_DONE) for a stream once its compilation is uploaded or rejected. Until then,
including a stream with one kept clip and no compilation, the stream's files stay, as they do today for a clip that's
rendered and not uploaded. "Delete this stream's video files" leaves `kept` clips as they are when the compilation is
uploaded (they're part of it, and the AI learns from them); otherwise it rejects them as it does other clips that
aren't uploaded, and its warning counts them.

### API additions (all additive)

- `ClipOut.in_compilation: bool`: a chapter or highlight of a stream with `makes_compilation`. The frontend uses it to
  choose Keep over Approve.
- `ClipOut.compilation_clips: int`: for a compilation, how many clips it joins (0 otherwise).
- `ClipOut.compilation_changed: bool`: for a compilation not yet uploaded, whether it's out of date.
- `VodOut.compilation: CompilationOut | null`, null unless `makes_compilation`:
  `{ vod_id, vod_title,`
  `kept: [[start, end], ...]` (the parts it's made of, in stream order: before it's made, the kept parts of every
  included clip, merged; once made, the compilation's own kept parts, so it shows the owner's edits),
  `to_review: [[start, end], ...]` (the ranges of its picks still proposed), `clips: int` (included clips),
  `length: float` (seconds it runs, from `kept`), `clip_id: int | null` (the compilation, once made),
  `status: "waiting" | "making" | "one" | "none" | ClipStatus` ("waiting" while picks are left to review, "making" once
  none is and it isn't made yet, "one" with a single clip included and no compilation, "none" with none included, else
  the compilation's own status), `changed: bool }`.
- `GET /api/compilations` (new): the CompilationOut of every compiling stream the inbox has something to say about:
  picks to review, a compilation being made, or a compilation to review. One request per inbox poll.
- `POST /api/clips/{id}/keep`, `POST /api/clips/{id}/rebuild`, and `keep` on the new-clip body.

### Learning (learning.py)

- `APPROVED` gains `kept`, so kept picks count in "How the channel owner changed your earlier picks": moved starts and
  ends, cuts with their words, retitles, extras approved anyway, and the turned-down count's denominator.
- `_shorts` leaves out kept highlights that weren't posted: whether they'd make a Short was never the question.
- Compilation cuts: for each compilation of the show with `picked_*` set (compilations made before this ship have none
  and are skipped), the parts the owner removed (defined under Server behaviour) are split across the included clips'
  kept parts they overlap, and each piece is added to that clip as a cut "in the compilation"; where two included clips
  overlap (a highlight inside a chapter), the piece goes to the shorter one. These clips count as changed even if they
  were kept without review (the owner made the cut), their lines read "in the compilation they cut out 8 s: …", and
  they count in "cut parts out of N".
- `track_record` still reaches `segment.run` (condition 2); its existing test stays.

### Screens

The edit bay's look throughout. **The one new element is the compilation strip**, the stream's slate (ClipCard's
`Slate`) grown into a picture of the compilation:

```
Compilation                         3 kept, 2 to review   14:20
[▁▁▁▁████▁▁▁▁▁▁▁▁▁▁▁████▁▁▁▁▁░░░░▁▁▁▁▁▁████▁▁▁▁▁▁░░░▁▁▁▁▁▁▁]
```

- One bar the length of the stream, 6px, on panel-2 (Slate's is 4px).
- Kept parts are chalk blocks. Ranges still to review are graphite outlines with no fill. The clip the keyboard or a
  hover is on shows its range in mark yellow, the colour already used for a clip's in/out range on its slate.
- "Compilation" in Archivo semi-condensed; the counts in graphite; the running length in Martian Mono.
- When a clip is kept, its outline fills to chalk (about 200 ms); when one is rejected, its outline goes. No other
  motion; none with reduced motion.
- A `CompilationStrip` component, used on the inbox, the stream page and the compilation's editor.

**Inbox.** A stream that makes a compilation shows the strip at the top of its group, inside the fold. Its cards say
Keep instead of Approve:

```
phone                                   desktop row
┌──────────────────────────────┐        ┌───────────┬──────────────────────────┬──────────────┐
│ [thumbnail          12:04]   │        │ thumbnail │ Clip            ▮▮▮▮▮▮▮▯▯ │ ✓ Keep       │
│ Clip               ▮▮▮▮▮▮▮▯▯ │        │           │ Title                    │ ↑ Keep and   │
│ Title of the clip            │        │           │ Why the AI picked it     │   post       │
│ Why the AI picked it         │        │           │ 1:02:10 to 1:14:14  12:04 │ ✂ Edit       │
│ 1:02:10 to 1:14:14    12:04  │        │           │ [slate]                  │ ✕ Reject     │
│ [slate]                      │        └───────────┴──────────────────────────┴──────────────┘
│ [ ✕ Reject ]   [ ✓ Keep    ] │
│   ↑ Keep and post it by itself too  │
└──────────────────────────────┘
```

- Swipe right says "Keep"; swipe left "Reject".
- Keys: A Keep, P Keep and post, R Reject, E Edit, Z Undo. The legend shows "A Keep" and P only when a stream on screen
  makes a compilation; elsewhere A stays Approve and P does nothing.
- "Keep and post it by itself too" is a quiet text button (graphite, upload icon), so Keep stays the obvious action.
- The inbox is built from its proposed clips and `GET /api/compilations`, so a compiling stream's group stays after
  its last pick is decided.
- Keeping the last clip of a stream: toast "That was the last one. The compilation of 4 clips is being made." The
  group stays, folded down to its strip, with the line "Being made", until the compilation is made. Then, if the show
  reviews compilations, it arrives as that group's one card. If it doesn't, the group goes, and the stream's page and
  Upload take it from there.
- A stream with one clip kept shows its strip with "Only one clip kept" and a "Post it by itself" button, until the
  owner posts it, keeps another, or rejects it.
- The compilation card: "Compilation", "4 clips" where a pick has its score, its length, "The 4 kept clips of this
  stream, in order." Its buttons are Approve / Edit / Reject as today (approving the compilation posts it). Its card's
  slate is the strip.
- The subtitle counts clips to review as today; a group that's only a strip isn't counted. "All caught up" shows when
  there's nothing to review, with any "Being made" strips under it.

**Upload.** Kept clips never appear. The compilation's row reads "Compilation of 4 clips to {channel}". A row for a clip
of a compiling stream that's posted by itself too gets "Compilation only" beside "To inbox". The Upload badge counts
only rendered clips, as today, so kept ones never add to it.

**Stream page (VideoDetail).** Above its clips, a compilation panel: the strip, a line saying where it stands, and the
one next action.

| Stands | Line | Action |
| --- | --- | --- |
| waiting | "Made once the last 2 clips are reviewed" | Review them (to the inbox) |
| making | "Being made" | – |
| proposed | "Ready to review" | Review it (its editor) |
| approved / rendering | "Rendering" | Open |
| rendered | "Ready to upload" | Upload |
| uploaded | "Uploaded" | On YouTube |
| failed | "Render failed" | Open |
| changed (not uploaded) | adds "The kept clips changed since it was made." | Rebuild from kept clips (confirms, see Rebuild) |
| one | "Only one clip kept. Keep another, or post this one by itself." | Post it by itself |
| none | "No clips kept, so there's no compilation" | – |

Kept clips' cards show the "In compilation" chip (neutral: chalk on panel-2). Clips posted too keep their render and
upload chips. Video counts read "3 in compilation".

**Editor**, for a clip of a compiling stream:

| Status | Buttons |
| --- | --- |
| proposed / extra | Reject, Save (if changed), Keep and post, **Keep** |
| kept | Delete (reject), Save (if changed), **Post it by itself too** |
| approved and on | as today, plus "Compilation only" |
| new clip in a compiling stream | Save to inbox, Keep and post, **Keep** |

The panel saying why the AI picked it adds, for a kept clip, "In the compilation from 12:40" once it's made. Editing a
kept clip's ends or cuts after the compilation is made marks the compilation changed. A compilation's editor shows the
strip and, when it's out of date, the same notice and Rebuild button as the stream page.

**Show editor › Compilation.** Same two fields, new wording:

- Tick: "Make a compilation of each {stream}" (was "Also join each stream's approved clips and Shorts into one video").
- Hint: "Approving a clip keeps it for the compilation instead of posting it. To post one by itself too, choose Keep and
  post on it. Once none is left to review, the kept clips are joined in the order they happened, with a chapter for
  each, and the compilation goes where the clips go. Only {stream}s processed after you turn this on work this way."
- Under "Without review", when the tick is on, a note under Clips and Shorts: "With compilations on, clips taken
  without review are kept for the compilation, not posted by themselves." The fields and their values don't change.

**Overview and Videos list.** Count kept clips as "in compilation" where they count clips by status. Nothing else.

### Migration

On start, set `makes_compilation` for streams that have `compilation_due` set or already have a compilation clip. Their
clips already `approved` or further along stay as they are (they're posted too); their clips still `proposed` get Keep
from then on. No other data changes.

### Tests

- test_compilation.py: kept clips are included; one kept clip makes no compilation and stays kept; keeping another
  after that sets the stream due again and makes one; out-of-date detection; rebuild takes the new clips, keeps the
  owner's cuts where they still apply, and keeps title and channel; the compilation records `picked_*`.
- test_worker.py: picks taken without review in a compiling stream become `kept`; kept clips never render; a pick in a
  compiling stream never uploads by the show's clip rules or score, and a posted-too one follows upload_approved;
  cleanup waits for the compilation's upload or rejection; the existing `track_record` → `segment.run` test passes.
- learning tests: a kept pick's cut appears in the changed picks; a cut made in the compilation is credited to the clip
  it falls in and leaves out the gaps; a cut straddling two clips is split between them.
- API tests: keep from each allowed status and the refusals; approve from kept; Upload's clip list never returns kept.
- Frontend: typecheck and build; check the inbox, stream page, editor, Upload and show editor at phone and desktop sizes
  in the running app; a show save round-trip with every field set gives identical JSON before and after.

## Calls made for the owner to check

1. Picks a show takes without review are kept, never posted by themselves, in a compiling stream. The show's clip and
   Short "Without review" settings and its score threshold never upload a pick there.
2. With only one clip kept there's no compilation, and nothing is posted. The app says so and offers "Post it by
   itself", and the stream's files stay until you decide.
3. A compilation never rebuilds by itself after the kept clips change; it says it's out of date and offers Rebuild,
   which keeps the parts you cut out of it and writes its description again.
4. The compilation still follows the show's "Without review → Compilation" choice (review it in the inbox, or skip
   that).
5. Whether a stream works this way is fixed when it's processed, from the show's setting then.
6. "Compilation only" lets a clip you chose to post go back to being only in the compilation, until it's uploaded.

## Left to the builder

- Exact component structure.
- Server function and test names, and how the interval difference for compilation cuts is written.
- The order of the work, as long as the mockups of the new screens are added to `design-mockups/edit-bay.html` first.
