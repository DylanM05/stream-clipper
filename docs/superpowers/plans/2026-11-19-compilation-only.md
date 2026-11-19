# Compilation-only streams Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, in one session, with the context cleared after every task (see "Starting and finishing a task" below). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In a stream that makes a compilation, approving a clip keeps it for the compilation (new status `kept`) instead of rendering and posting it; the compilation is made once the stream's clips are reviewed.

**Architecture:** A new clip status `kept` sits beside `approved` and is handled explicitly everywhere a status is read, so the render queue and Upload ignore it by construction. `Vod.makes_compilation` fixes at processing time whether a stream works this way. The compilation records the plan it was first made from (`picked_*`) so edits to it, and a changed set of kept clips, can be detected and learned from.

**Tech Stack:** FastAPI + SQLModel (SQLite), pytest; React + Tailwind v4 (edit-bay tokens), TypeScript.

**Spec:** `docs/superpowers/specs/2026-11-17-compilation-only-design.md` (sections are cited below as "Spec › Name"; the screens' copy and layout are in Spec › Screens and are not repeated here). This plan does **not** cover `2026-11-19-compilation-mode-design.md`, which is built afterwards on top of this.

## Global Constraints

- Run backend tests from `backend/` with `CLIPPER_MIN_FREE_GB=1 .venv/bin/python -m pytest`. Known machine-caused failures: 2 in `test_cleanup.py` (disk threshold) and 5 in `test_editor.py` (ffmpeg without libass). Compare against `git stash -u` before calling anything a regression.
- Edit-bay condition 1: `ShowForm`'s `formFields`, `BLANK_SHOW` and the save body are not changed (only wording and layout around the inputs).
- Edit-bay condition 2: the worker test that `learning.track_record` reaches `segment.run` keeps passing.
- Edit-bay tokens (index.css `@theme`): mark yellow `#F0B429` only for in/out points, selection, the one main action and focus; red = reject, green = approve/uploaded; sentence-case labels; Martian Mono for timecodes only.
- New columns need a default so `db._add_missing_columns` adds them; no migration script.
- Words the app uses are fixed in Spec › Words the app uses ("Keep", "Kept for the compilation", "In compilation", "Rebuild from kept clips", …).
- Frontend verification steps use the `local-testing` skill (`npm run build` / type-check, then the running app at phone and desktop sizes).

## Review Focus

- A clip kept, then its ends edited in the editor: it stays `kept` (must not flip to `approved` and render).
- Delete on a kept clip: Undo returns it to `kept`, not `approved`.
- Keep pressed twice, or on a clip already `kept`/`approved`: no error, no second status change.
- A stream whose show turned `compilation` off after processing: its clips still say Keep (`makes_compilation` is per stream).
- All kept clips rejected afterwards: the stream's compilation is not made, no empty compilation row, files not cleaned while one is still `kept`.
- Two kept clips where one overlaps the other (a highlight inside a chapter): the compilation joins them once, and a cut in the overlap is credited to the shorter clip.

## Starting and finishing a task

The owner clears the conversation after each task, so nothing from an earlier task is remembered. The plan, the spec, the git log and the "Handoff notes" at the bottom are the only memory.

**Starting (do this every time):**
1. You're on branch `kept-clips`. Run `git status -sb` (must be clean) and `git log --oneline -8`.
2. Find the first task whose boxes are not all ticked in this file. That's yours; don't touch later ones.
3. Read the "Handoff notes" at the bottom, the task's **Interfaces**, and its **Read first** list. Read only what it lists, plus the Spec sections it cites. Line numbers in this plan were taken before any edits and drift: find code by name (`grep -n`), not by number.
4. Use `superpowers:executing-plans` and, for code, `superpowers:test-driven-development`.

**Finishing (every task ends with this, after its own last step):**
1. Everything the task changed is committed; `git status -sb` is clean.
2. Tick the task's boxes in this file. Under "Handoff notes", add a few lines on anything the next task can't get from the Interfaces: a name you chose differently, a test that fails for a machine reason, a decision you made. Commit those two together as `Plan: task N done`.
3. Stop. Tell the owner "Task N is done and committed; clear the context and start the next one", with one line on what you verified. Don't begin the next task.

If a task turns out to be wrong or too big, stop and say so instead of reshaping the plan quietly.

## File Structure

- `backend/clipper/db.py`: `kept` in `CLIP_STATUSES`; `Vod.makes_compilation`, `ClipRecord.picked_cuts`; start-up backfill.
- `backend/clipper/cuts.py`: `removed(start, end, cut_list, picked_start, picked_end, picked_cut_list)`.
- `backend/clipper/compilation.py`: `INCLUDED` gains `kept`; `Plan` already carries what `picked_*` stores; `is_changed(compilation, plan)`.
- `backend/clipper/worker.py`: processing, `compile_next`, cleanup.
- `backend/clipper/web/app.py`: keep / rebuild endpoints, `set_status`, status traps, `ClipOut` and `CompilationOut`, `GET /api/compilations`.
- `backend/clipper/learning.py`: `APPROVED`, compilation cuts.
- `frontend/src/`: `api.ts`, `ui.tsx`, new `CompilationStrip.tsx`, `ClipCard.tsx`, `screens/Inbox.tsx`, `screens/Videos.tsx`, `screens/Approved.tsx`, `screens/Shows.tsx` (wording only), `screens/Overview.tsx`, `editor/useEditor.ts`, `editor/DesktopEditor.tsx`, `editor/PhoneEditor.tsx`.
- Mockups: `../design-mockups/edit-bay.html` (outside the repo).

---

### Task 1: Mockups of the new screens

**Files:** Modify `/Users/yirpz/Documents/stream-clipper/design-mockups/edit-bay.html` (outside the repo; **no commit step**).

**Read first:** Spec › Screens; the existing `design-mockups/edit-bay.html`. This task's "finishing" tick and handoff note are the only commit (of this plan file).

- [x] **Step 1:** Add mockups for: the compilation strip (waiting, making, one-kept states), the inbox group with Keep / Keep and post cards (phone and desktop), the stream page's compilation panel (each row of Spec › Screens' table), and the editor's button sets for a proposed, kept and compilation clip. Use Spec › Screens for layout and copy, and the existing mockups' tokens.
- [x] **Step 2:** Open the file in the browser and check it at phone and desktop widths. Send the owner the file path to look at; carry on once they've seen it, since later tasks follow it.

### Task 2: The `kept` status and the new columns

**Files:** Modify `backend/clipper/db.py:28,292-312,317-380,545`, `backend/clipper/compilation.py:15`, `backend/clipper/learning.py:38`; Test `backend/tests/test_compilation.py`, `backend/tests/test_api.py`.

**Read first:** `db.py` (`CLIP_STATUSES`, `Vod`, `ClipRecord`, `get_engine`, `_add_missing_columns`), `compilation.py`, `learning.py` lines 30-45, the top 30 lines of `tests/test_compilation.py` (its `clip()` helper) and `tests/test_api.py` (`settings`, `client`, `add_clip`).

**Interfaces:**
- Produces: `"kept"` in `db.CLIP_STATUSES`; `compilation.INCLUDED` and `learning.APPROVED` include `"kept"`; `Vod.makes_compilation: bool = False`; `ClipRecord.picked_cuts: str = ""`. **`CLIP_DONE` is not changed** (kept counts as done only conditionally, Task 5).

- [ ] **Step 1: Failing tests.** In `test_compilation.py`: `test_kept_clips_are_included_in_the_compilation` (two `kept` clips plan a compilation with both as `picks`). In `test_api.py`: `test_the_backfill_marks_streams_that_already_make_a_compilation` (a Vod with `compilation_due=True`, and another with a `kind="compilation"` clip, both get `makes_compilation` after `get_engine`; a plain one doesn't).
- [ ] **Step 2:** Run them; expected FAIL (unknown status / column).
- [ ] **Step 3:** Add the status, the two columns, and the tuple edits. Add `_backfill_makes_compilation(engine)` in `db.py`, called from `get_engine` after `_add_missing_columns`: one idempotent `UPDATE vod SET makes_compilation = 1` for rows with `compilation_due` or with a `compilation` clip (chosen over `_FILLED` because the second case needs a subquery). Update the comment above `CLIP_STATUSES`.
- [ ] **Step 4:** Run both; expected PASS. Run the whole of `test_learning.py` and `test_compilation.py`; expected PASS.
- [ ] **Step 5:** Commit `Add the kept clip status and the columns compilation-only streams need`.

### Task 3: Keep, approve from kept, and the status leaks

**Files:** Modify `backend/clipper/web/app.py` (`ACTIVE_CLIP` at 71, `set_status` ~989, `update_clip` ~1972, `approve` ~1980, `restore`/`reject` ~2175-2195, `make_clip` ~2260, `NewClip` body); Test `backend/tests/test_api.py`.

**Read first:** Spec › Server behaviour (Keep, Approve, Restore/reject, Making a clip, Whenever a pick changes status); in `app.py` the functions `set_status`, `discard_render`, `approve`, `reject`, `restore`, `update_clip`, `make_clip`, `ACTIVE_CLIP`; `tests/test_api.py` helpers (`add_clip`, `set_status`).

**Interfaces:**
- Produces: `POST /api/clips/{id}/keep -> ClipOut`; `NewClip.keep: bool = False`; helper `compilation.mark_due(vod: Vod, clips: list[ClipRecord]) -> None` (in `compilation.py`, so the worker shares it) that sets `vod.compilation_due = True` when `vod.makes_compilation` and the stream has no `compilation` clip (called by keep, approve, restore, reject, make_clip).
- Consumes: Task 2's status and columns.

- [ ] **Step 1: Failing tests** (names, with the asserted behaviour):
  - `test_keep_sets_kept_and_marks_it_reviewed`: status `kept`, `approved_in_review` true, `reviewed_at` set, vod `compilation_due` true.
  - `test_keep_is_refused_outside_a_compiling_stream` (422), `..._from_uploaded` (409, "It's on YouTube already. It stays in the compilation."), `..._after_the_files_are_deleted` (409).
  - `test_keep_from_approved_discards_the_render` (rendered clip → `kept`, `video_path` file removed, queued upload job cancelled).
  - `test_keep_twice_changes_nothing`.
  - `test_approve_from_kept_is_keep_and_post` (status `approved`).
  - `test_editing_a_kept_clips_ends_leaves_it_kept` (PATCH start/end/cuts/make_short → still `kept`).
  - `test_restore_and_reject_work_from_kept`.
  - `test_make_clip_with_keep_creates_it_kept`.
  - `test_a_kept_clip_is_not_listed_as_ready_to_upload_or_counted` (`/api/clips?status=rendered` and the summary's upload count exclude it).
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement. `keep` follows `approve`'s shape and Spec › Server behaviour › Keep (allowed from `proposed`, `extra`, `rejected`, and from `approved`/`rendering`/`rendered`/`failed` via `discard_render`). Change `set_status` so `approved_in_review` is true for `approved` **and** `kept`. In `update_clip`, the re-render flip to `approved` (the `changed.keys() & {...}` line) only applies to `ACTIVE_CLIP` statuses, which excludes `kept`; keep `ACTIVE_CLIP` as is and say so in a comment. `approve` returns early only for `approved`/`rendering`/`rendered`/`uploaded`, so `kept` falls through to `set_status`. Call `mark_due` in the five places named above.
- [ ] **Step 4:** Run `test_api.py`; expected PASS.
- [ ] **Step 5:** Commit `Keep a clip for its stream's compilation`.

### Task 4: Processing and compile_next

**Files:** Modify `backend/clipper/worker.py` (`_add_clips` ~434-465, ready summary ~280-296 and `_added_summary` ~1284, `compilation_due` at 301, `compile_next` 705-750); Test `backend/tests/test_worker.py`, `backend/tests/test_compilation.py`.

**Read first:** Spec › Server behaviour (Processing, compile_next, Automatic uploads); in `worker.py` `_add_clips`, `_added_summary`, the `status="ready"` block of `process`, `compile_next`; in `tests/test_worker.py` the fixtures and `add_show`/`add_vod` helpers and the existing `compile_next` tests; the `track_record` → `segment.run` test.

**Interfaces:**
- Produces: module constant `worker.COMPILE_DELAY_S = 10`; `_add_clips` gives a pick the show takes without review `status="kept"` (not `approved`) when the stream makes a compilation; `compile_next` sets `picked_start`, `picked_end`, `picked_cuts` on the compilation.
- Consumes: `Vod.makes_compilation`, Task 3's `compilation.mark_due`.

- [ ] **Step 1: Failing tests:**
  - `test_a_pick_taken_without_review_in_a_compiling_stream_is_kept` (also `auto_upload_score`): status `kept`, `reviewed_at` set.
  - `test_the_stream_is_marked_as_making_a_compilation_when_processed` (`makes_compilation` follows `show.compilation` then, and later changing the show doesn't change it).
  - `test_a_kept_clip_never_renders` (`render_next_clip` leaves a `kept` clip alone).
  - `test_a_pick_in_a_compiling_stream_never_uploads_by_the_show_rules` (clip rules / score don't queue an upload; a clip approved by the owner follows `upload_approved`).
  - `test_compile_waits_ten_seconds_after_the_last_decision` (decision at `utcnow()` → `compile_next()` is False; `reviewed_at` moved `COMPILE_DELAY_S + 1` s into the past → it makes the compilation).
  - `test_compilation_records_what_it_was_first_made_as` (`picked_start/end/cuts` equal the plan's).
  - `test_one_kept_clip_makes_no_compilation_and_says_so` (stays `kept`; one notification "Only one clip was kept from …", linking `videos/{id}`; none sent again on a second pass).
  - `test_keeping_another_after_one_makes_the_compilation` (stream due again, then made).
  - `test_none_kept_does_nothing`.
  - Existing `track_record` → `segment.run` test must still pass.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement per Spec › Server behaviour › Processing and › compile_next. Set `makes_compilation` next to `compilation_due` at `status="ready"`. The "Ready" note says "N clips to keep or reject for the compilation" for a compiling stream. In `compile_next`, skip a stream whose newest `reviewed_at` among its picks is within `COMPILE_DELAY_S`. A one-clip stream gets the notification once per due-marking: `compile_next` already clears `compilation_due` when it deals with a stream, so the note goes out in that same pass, and only if the single included clip is still `kept` (not `approved`).
- [ ] **Step 4:** Run `test_worker.py` and `test_compilation.py`; expected PASS.
- [ ] **Step 5:** Commit `Streams that make a compilation keep their clips and make it once they're reviewed`.

### Task 5: Cleanup, and the compilation's removed parts

**Files:** Modify `backend/clipper/worker.py` (`clean_up_next` ~1195, `clean_up`), `backend/clipper/web/app.py` (the delete-files endpoint and its warning), `backend/clipper/cuts.py`; Test `backend/tests/test_cleanup.py`, new tests in `backend/tests/test_cuts.py` if present, else `test_compilation.py`.

**Read first:** Spec › Server behaviour (What the owner removed, Cleanup); `cuts.py` (`tidy`, `kept`, `Cut`); in `worker.py` `clean_up_next` and `clean_up`; the delete-files endpoint in `app.py` (`grep -n delete-files`); `tests/test_cleanup.py`.

**Interfaces:**
- Produces: `cuts.removed(start: float, end: float, cut_list: list[Cut], picked_start: float, picked_end: float, picked_cuts: list[Cut]) -> list[Cut]`: what the owner took out of a compilation: `picked_start→start`, `end→picked_end`, and `cut_list` minus `picked_cuts` as an interval difference (a new cut touching a gap was merged into it by `tidy`, so only the new part counts). Sorted, merged.
- Cleanup rule: a stream with a `kept` clip is not cleaned until its compilation is uploaded or rejected; "Delete this stream's video files" leaves `kept` clips alone once the compilation is uploaded, else rejects them, and its warning counts them.

- [ ] **Step 1: Failing tests:** `test_removed_parts_include_a_new_cut_that_merged_into_a_gap` (picked cuts `[(130,500)]`, now `[(130,520)]` → `[(500,520)]`); `test_removed_parts_include_a_moved_start_and_end`; `test_removed_is_empty_when_nothing_changed`. In `test_cleanup.py`: `test_a_stream_with_a_kept_clip_and_no_uploaded_compilation_is_not_cleaned`, `test_it_is_cleaned_once_its_compilation_is_uploaded` and `..._rejected`.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement `cuts.removed`; in `clean_up_next` treat `kept` as done only when the stream's `compilation` clip is `uploaded` or `rejected`; mirror it in the delete-files endpoint and its warning count.
- [ ] **Step 4:** Run the cleanup and cuts tests; expected PASS (apart from the 2 known disk-threshold failures).
- [ ] **Step 5:** Commit `Keep a stream's files until its compilation is done, and work out what was cut from it`.

### Task 6: Rebuild, out-of-date detection and the API shapes

**Files:** Modify `backend/clipper/compilation.py`, `backend/clipper/web/app.py` (`ClipOut`, `clip_out`, new `CompilationOut`, `vod_out`, new routes); Test `backend/tests/test_compilation.py`, `backend/tests/test_api.py`.

**Read first:** Spec › Server behaviour (The compilation changed, Rebuild) and › API additions; `compilation.py` (`plan`); in `app.py` `ClipOut`, `clip_out`, `VodOut`, `vod_out`, `discard_render`, `set_status`; the `compile_next` code in `worker.py` as Task 4 left it (Rebuild must set status the same way).

**Interfaces:**
- Produces: `compilation.is_changed(clip: ClipRecord, plan: Plan | None) -> bool` (true when the plan from the stream's clips now differs in start, end or gaps from `picked_*`; false if the compilation has no `picked_start`); `ClipOut.in_compilation: bool`, `.compilation_clips: int`, `.compilation_changed: bool`; `CompilationOut` and `VodOut.compilation` exactly as in Spec › API additions; `GET /api/compilations -> list[CompilationOut]`; `POST /api/clips/{id}/rebuild -> ClipOut` (Spec › Rebuild: 409 when uploaded, files deleted, or fewer than two clips with the message "Only one clip is kept now. Post it by itself, or reject the compilation."; applies `cuts.removed` to the new plan; keeps title, channel and playlist; rewrites the description; discards the render; status follows `auto_compilation`).

- [ ] **Step 1: Failing tests:** `test_a_compilation_is_changed_once_a_kept_clip_is_rejected`; `test_it_is_not_changed_by_its_own_edits` (the owner's cuts alone don't make it "changed"); `test_rebuild_takes_the_new_clips_and_keeps_the_owners_cuts`; `test_rebuild_keeps_title_channel_and_playlist`; `test_rebuild_refusals` (uploaded 409, one clip left 409); `test_compilation_out_lists_kept_and_to_review_ranges_and_its_standing` (each `status` value: waiting, making, one, none, the compilation's own); `test_compilations_endpoint_only_returns_streams_with_something_to_say`.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement. `GET /api/compilations` is one query per call (no per-stream request); it must be registered before the `/api/clips/{clip_id}` style routes cannot shadow it (different prefix, so no conflict, but check the route order test).
- [ ] **Step 4:** Run `test_compilation.py` and `test_api.py`; expected PASS.
- [ ] **Step 5:** Commit `Say when a compilation is out of date, and rebuild it from the kept clips`.

### Task 7: Learning from kept clips and cuts in the compilation

**Files:** Modify `backend/clipper/learning.py` (`_changes` ~222, `_shorts` ~326, a new helper `_compilation_cuts`); Test `backend/tests/test_learning.py`.

**Read first:** Spec › Learning; `learning.py` (`_changes`, `_looked_at`, `_changed_line`, `_shorts`, `_Transcripts`); `tests/test_learning.py` helpers (`add_clip`); `cuts.removed` as Task 5 left it.

**Interfaces:**
- Consumes: `cuts.removed`. Produces: for each compilation of the show with `picked_start` set, the removed parts split across the included clips' kept parts they overlap and credited to that clip as an extra cut "in the compilation" (the shorter clip where two overlap); those clips count as changed even when kept without review; lines read "in the compilation they cut out 8 s: …"; they count in "cut parts out of N". `_shorts` skips kept highlights that weren't posted.

- [ ] **Step 1: Failing tests:** `test_a_kept_picks_cut_appears_in_the_changed_picks`; `test_a_cut_in_the_compilation_is_credited_to_the_clip_it_falls_in_and_not_the_gaps`; `test_a_cut_across_two_clips_is_split_between_them`; `test_an_overlap_goes_to_the_shorter_clip`; `test_a_compilation_without_picked_start_is_skipped`; `test_kept_highlights_are_not_counted_as_shorts`.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Implement per Spec › Learning. A clip kept without review stays out of `_looked_at`'s "approved" list unless it has a compilation cut (the owner made that cut).
- [ ] **Step 4:** Run `test_learning.py` and the `track_record` test; expected PASS.
- [ ] **Step 5:** Commit `The AI learns from kept clips and from what is cut out of a compilation`.

### Task 8: Frontend types, labels and the compilation strip

**Files:** Modify `frontend/src/api.ts` (`ClipStatus` at 17, `Clip`, `Vod`, new `Compilation` type, `api.keep`, `api.rebuild`, `api.compilations`, `NewClip.keep`), `frontend/src/ui.tsx` (`CLIP_STATUS` ~44: `kept: ["In compilation", "bg-panel-2 text-chalk"]`); Create `frontend/src/CompilationStrip.tsx`.

**Read first:** Spec › Screens › the strip; the Task 1 mockups; `frontend/src/api.ts` (`Clip`, `Vod`, the `api` object), `ui.tsx` (`CLIP_STATUS`, `Chip`), `ClipCard.tsx` (`Slate`); the backend's `CompilationOut` and `ClipOut` as Task 6 left them (the TypeScript types must match field for field).

**Interfaces:**
- Produces: `CompilationStrip({ compilation, highlight? }: { compilation: Compilation; highlight?: [number, number] })` per Spec › Screens › compilation strip (6px bar, chalk kept blocks, graphite outlines to review, mark yellow only for `highlight`, header "Compilation" + counts + running length in mono; 200 ms fill, none with reduced motion).

- [ ] **Step 1:** Add the types and API calls; add the `kept` label; build the strip against the mockups from Task 1.
- [ ] **Step 2:** Type-check and build (`local-testing` skill); expected no errors.
- [ ] **Step 3:** Check the strip in the running app at phone and desktop sizes with a fake stream (kept, to-review and empty states).
- [ ] **Step 4:** Commit `Add the compilation strip and the kept status to the app`.

### Task 9: Inbox

**Files:** Modify `frontend/src/screens/Inbox.tsx`, `frontend/src/ClipCard.tsx` (`ReviewCard`).

**Read first:** Spec › Screens › Inbox and › Words the app uses; the Task 1 mockups; `Inbox.tsx`, `ClipCard.tsx`, `CompilationStrip.tsx`, and the `api` calls Task 8 added.

- [ ] **Step 1:** Build per Spec › Screens › Inbox: groups also built from `api.compilations()` (one extra request per poll); a compiling group's strip at the top inside the fold; Keep instead of Approve when `clip.in_compilation`; "Keep and post it by itself too" as a quiet text button; swipe right says "Keep"; keys A Keep, P Keep and post (does nothing elsewhere), legend shows "A Keep" only when a compiling stream is on screen; toasts exactly as in the words table; last-clip toast and "Being made" folded state; "Only one clip kept" with "Post it by itself"; the compilation's card; "All caught up" with any "Being made" strips under it; the subtitle doesn't count strip-only groups.
- [ ] **Step 2:** Type-check and build; expected no errors.
- [ ] **Step 3:** In the running app (`local-testing`): keep, keep-and-post and reject by mouse, keys and swipe; undo with Z restores the clip to the inbox; keep the last clip of a stream and watch the group fold to "Being made".
- [ ] **Step 4:** Commit `Keep clips for the compilation from the inbox`.

### Task 10: Editor

**Read first:** Spec › Screens › Editor; the Task 1 mockups; `useEditor.ts` (`decide`, `discard`, `create`), the button blocks of `DesktopEditor.tsx` and `PhoneEditor.tsx`, `CompilationStrip.tsx`.

**Files:** Modify `frontend/src/editor/useEditor.ts` (`decide` ~428, `discard` ~453, `create` ~464), `DesktopEditor.tsx` (`primary` ~63, buttons ~343-392, extra note ~660), `PhoneEditor.tsx` (~322-371).

- [ ] **Step 1:** Button sets per Spec › Screens › Editor. `decide`/`create` gain a keep variant calling `api.keep`/`NewClip.keep`. **`discard`'s Undo must call `api.keep` when the clip was `kept`** (today it calls `api.approve`, which would send a kept clip to render). Add "Post it by itself too" for kept and "Compilation only" for approved. The why-panel adds "In the compilation from 12:40" for a kept clip once the compilation exists. A compilation's editor shows the strip and the out-of-date notice with Rebuild (confirm text from the Spec).
- [ ] **Step 2:** Type-check and build; expected no errors.
- [ ] **Step 3:** In the running app: edit a kept clip's ends and confirm it stays "In compilation" and the compilation shows the changed notice; Delete then Undo on a kept clip returns it to kept.
- [ ] **Step 4:** Commit `Keep from the editor, and rebuild a compilation there`.

### Task 11: Stream page, Upload, show wording and counts

**Files:** Modify `frontend/src/screens/Videos.tsx` (`VideoDetail` ~349: compilation panel above its clips, "3 in compilation" counts, `deleteFiles` warning counts kept clips, kept clips' chip), `screens/Approved.tsx` (compilation row reads "Compilation of 4 clips to {channel}"; "Compilation only" beside "To inbox" for a posted-too clip; kept never listed), `screens/Shows.tsx` (**wording only**: the tick label, hint and the note under Without review, per Spec › Show editor › Compilation; no change to `formFields`/`BLANK_SHOW`/save body), `screens/Overview.tsx` (count kept as "in compilation").

**Read first:** Spec › Screens (Upload, Stream page, Show editor › Compilation, Overview and Videos list); the Task 1 mockups; `VideoDetail` in `Videos.tsx`, `Approved.tsx`, the Compilation group in `Shows.tsx` (~lines 790-830), the clip-status counts in `Overview.tsx`; `CompilationStrip.tsx`.

- [ ] **Step 1:** Build per Spec › Screens (the stand/line/action table for the panel).
- [ ] **Step 2:** Add a check in the app that a show save with every field set gives identical JSON before and after (Spec › Tests, condition 1).
- [ ] **Step 3:** Type-check and build; expected no errors.
- [ ] **Step 4:** In the running app at phone and desktop sizes: every row of the panel table; Upload never shows a kept clip; the Upload badge unchanged by kept clips.
- [ ] **Step 5:** Commit `Show a stream's compilation on its page and on Upload`.

### Task 12: Whole-branch verification and the PR

**Read first:** the Handoff notes, `git log --oneline origin/main..HEAD`, and the Spec's "Calls made for the owner to check". Because this is the last task, a fresh reviewer (a subagent on the most capable model) reads `git diff origin/main...HEAD` against the Spec and this plan's Review Focus before the PR is opened; fix what it finds.

- [ ] **Step 1:** Run the full backend suite with `CLIPPER_MIN_FREE_GB=1`; expected: only the 2 + 5 machine failures, which also fail on a `git stash -u` baseline.
- [ ] **Step 2:** `npm run build` and the type-check in `frontend/`; expected clean.
- [ ] **Step 3:** One end-to-end pass in the running app (`local-testing`): process a fake stream of a show with the compilation tick on, keep three clips and post one by itself, reject one, and watch the compilation get made after the 10 s; confirm only the posted-too clip appears on Upload.
- [ ] **Step 4:** Add the README paragraph on how a compiling show works (what Keep means, how to post one by itself). Commit.
- [ ] **Step 5:** Push and open the PR with `tea pr create` (the `forgejo` login), listing the six "Calls made for the owner to check" from the spec.

---

## Handoff notes

(Each task adds a few lines here when it finishes: names chosen, machine-only test failures, decisions made.)

- **Task 1 (mockups):** three pages added to `/Users/yirpz/Documents/stream-clipper/design-mockups/edit-bay.html` (outside the repo): `compinbox` (inbox with strip, Keep / Keep and post cards, "Being made" and "Only one clip kept" groups), `compstream` (every row of the stream-page panel table), `compeditor` (button sets per status, and a compilation's editor with the out-of-date notice). View one page with `edit-bay.html?only=compinbox` (add `&shot` to hide the headings). The mockup draws strip blocks wider than real ones (a 3-minute pick on a 6-hour stream is under 1% of the bar). The owner has not yet signed off on the look; Task 8 builds the strip from it, so ask them to glance at it first.

## Self-review notes

- Spec coverage: data (Task 2), Keep/approve/restore/reject/make-clip/due (3), processing + compile_next + one-clip + undo delay (4), cleanup + removed parts (5), out-of-date + Rebuild + API (6), learning (7), strip (8), inbox (9), editor (10), stream page / Upload / Shows / Overview (11), migration (2), tests (each task), mockups first (1).
- `CLIP_DONE` is deliberately unchanged; the conditional rule lives in cleanup (Task 5).
- The `mark_due` helper has one definition and is used by the endpoints and the worker (Task 3 / 4).
