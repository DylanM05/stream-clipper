# Compilation mode: many short funny moments, reviewed by playing them through

2026-11-19. Builds on Compilations (PR #2, merged) and the compilation-only flow (PR #5, design awaiting approval:
`2026-11-17-compilation-only-design.md`). This doc assumes PR #5's `kept` status ("In compilation") exists.

## Intent

A show that makes compilations should get a tight video: many short funny moments with little downtime between them,
not a few 20-minute pieces. Today compilation is an on/off switch layered on the clips mode, and it joins whatever
chapters and highlights were approved. Chapters run 5-40 minutes and highlights 15-180 seconds (about 3 an hour), so
the result is long pieces with dead air.

The owner wants:

- Compilation as a **video setting** beside clips, full episodes and both.
- In that mode the AI looks for funny moments the way it does for highlights, but is free to cut **as many short
  pieces as it sees fit**.
- The owner still reviews **every** piece. Reviewing 30-60 cards one by one is tedious, so the owner reviews by
  **playing the pieces back to back**, seeing which piece is playing, and approving or rejecting it as it plays.

## Goals

1. `Show.mode` gains `compilation`. In it, a stream gets no chapters, highlights or full episode: only the AI's short
   pieces, and the compilation made from the approved ones.
2. A prompt for this mode (new file in `backend/clipper/prompts/`, picked per video type like the existing ones) asks
   for every funny moment worth including, with no per-hour cap. Keep each piece as short as the moment allows.
   A piece can run several minutes when the setup, progression and payoff need that time. Cut at natural boundaries
   (the start of the necessary setup, the reaction after the payoff) and leave out downtime.
3. The AI's pieces are stored as ordinary clips (`kind="highlight"`, status `proposed`), so scoring, titles' learning
   and the editor keep working on them.
4. A **review player** for a stream's proposed pieces: it plays the stream through the pieces in stream order, jumping
   over everything between them (the same jump-over-cuts playback the editor has), and shows "Piece 7 of 34 · title ·
   length" over the video. Keys: **A** approves (`kept`, per PR #5) and moves on, **R** rejects and moves on, **Left /
   Right** go to the previous / next piece, **Space** plays or pauses. Rejected pieces drop out of the sequence;
   approved ones are marked on the strip. A strip below shows all pieces in order with their state.
5. From the player the owner can open a piece in the existing editor to trim its ends, and come back to the same place.
6. Once no piece is left to review, the compilation is made as today (`Worker.compile_next`), following the show's
   "Without review -> Compilation" choice.
7. Pieces whose gap is tiny are joined so the video doesn't jump around: the existing merge (`cuts.EDGE_S` in
   `compilation.plan`) is raised for this mode to a small gap (start with 1.5 s; the builder may tune it).

## Non-goals

- Shows in `segment`, `whole` or `both` mode: nothing changes for them, including their existing compilation switch.
- Rendering each piece on its own, or uploading pieces on their own, in this mode (see "More decisions").
- A phone version of the review player. The desktop one first; on a phone the existing inbox cards still work.
- Re-running the AI on streams already processed.

## Anti-goals (a failure even if it technically works)

- Reviewing a stream's pieces still means opening each one separately, or the player cannot be used without a mouse.
- A piece the owner rejected ends up in the compilation, or an approved one is left out.
- A rejected or approved piece in this mode renders or appears on Upload by itself.
- A field of `Show` is lost, renamed or reset (edit-bay condition 1: keep ShowForm's `formFields` / `BLANK_SHOW` / the
  save body; add the mode, don't rework the form), or `learning.track_record` stops reaching `segment.run` (condition
  2; the worker test must still pass).
- The stream's files are cleaned up while pieces are still to review.

## Constraints

- Backend: FastAPI + SQLModel on SQLite; new columns come from `db._add_missing_columns`. Run backend tests with
  `CLIPPER_MIN_FREE_GB=1`.
- Frontend: React + Tailwind v4, edit-bay tokens: mark yellow only for in/out points, selection, the one main action
  and focus; green approve, red reject. Mockup for the player goes in `../design-mockups/edit-bay.html` first.
- The editor plays the original stream file, not a render, so the player must too: nothing is rendered to review.
- Segmentation output is cached in `segmentation.json`; the new mode needs its own result shape (a list of pieces with
  start, end, title, score) or reuses `Clip` with the existing `highlights` field; the builder decides.

## Approaches

**Reviewing many short pieces**

- **Review player: play pieces back to back with approve/reject. Picked by the owner.** Matches how the owner thinks
  about the product (watching the compilation); pieces are ordinary clips so nothing else changes. Most new frontend
  work.
- Keep the inbox cards and approve one by one. Least work, but 30-60 cards per stream is the tedium that prompted
  this. Remains the fallback: the pieces are inbox clips either way, so it works from day one.

**The setting**

- **A fourth value of `Show.mode`. Picked (my call).** It's a video setting like the others and needs no new column.
- Keep the separate switch and add a "many short pieces" option under it: leaves two overlapping settings.

## Decisions (mine, to check)

- The existing `Show.compilation` switch stays in the data and keeps working for `segment` and `both` shows. In
  `compilation` mode it's implied. The Shows screen shows the four modes; the old switch is shown only for the modes
  where it still means something.
- No cap on the number of pieces per stream in this mode; the owner's review is the filter. A very high count is
  shown in the player's header so it isn't a surprise.
- Titles of pieces are short and not shown to viewers (the compilation's own chapter list on YouTube uses them), so
  the prompt asks for plain descriptive titles.

## More decisions (mine, from the builder check; to check)

- **Every piece is reviewed.** In this mode the show's "without review" rules and `auto_upload_score` don't apply:
  every piece is stored `proposed`. (The owner said they want to review them all.)
- **No cap on offered pieces.** `best_picks` / `max_shorts` / `highlights_per_hour` are bypassed in this mode; every
  piece goes to the player, none become `extra`.
- **No "post it by itself" in this mode.** The control is hidden and the approve endpoint refuses it for pieces of a
  `compilation`-mode stream. (This replaces the "can stay available" non-goal above.)
- **The merge gap is a parameter of `compilation.plan`**, not a change to `cuts.EDGE_S`. Rebuild, PR #5's out-of-date
  check and the learning step's removed-parts calculation all pass the same gap, so a compilation never shows as
  "changed" by itself.
- **The player resumes at the first `proposed` piece.** **Z** undoes the last A/R and goes back to that piece, within
  PR #5's delay before the compilation is made.
- **`compilation` is also a value of `Vod.mode`** (it shares `MODES`), so the stream-level "makes a compilation" flag
  (`compilation_due`) is set from the show's mode as well as from `Show.compilation`. A video with no show can't choose
  it, since compiling needs a show.

## Left to the builder

Name and shape of the new prompt and result schema; how the player is laid out (a screen on the stream's page, reached
from the inbox card for the stream); the exact merge gap; keyboard help text; tests (segmentation of this mode with a
fake AI, the player's piece sequence and skip behavior, status changes on approve/reject, compile once none is left).
