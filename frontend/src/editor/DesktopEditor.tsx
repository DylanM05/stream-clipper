// The clip editor on a computer, laid out like an editing app: the viewer and its transport with the clip's
// details beside it, and the timeline across the bottom. Most things have a key (press ? for the list).

import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Line } from "../api";
import {
  BackIcon, FitIcon, FrameBackIcon, FrameForwardIcon, KeyboardIcon, LoopIcon, MagnetIcon, MutedIcon, PauseIcon, PhoneIcon,
  PlayIcon, PlusIcon, RedoIcon, ScissorsIcon, SparkIcon, ToEndIcon, ToStartIcon, TrashIcon, UndoIcon, UploadIcon,
  VolumeIcon, ZoomInIcon, ZoomOutIcon,
} from "../icons";
import { cn, go, goBack, loadSetting, saveSetting } from "../lib";
import { PlaylistSelect } from "../PlaylistSelect";
import { Button, ClipStatusChip, DestinationSelect, ErrorNote, KIND_LABEL, useToast } from "../ui";
import { indexAt, startAt, useClock, useSettled, type Clock } from "./clock";
import { FRAME, MAX_SHORT_S, parseTimecode, round2, timecode, type Range } from "./edit";
import { Timeline } from "./Timeline";
import type { Editor } from "./useEditor";

const RATES = [0.5, 1, 1.5, 2];
const TEXT_FIELDS =
  "textarea, [contenteditable=true], input:not([type=checkbox], [type=radio], [type=range], [type=button], [type=submit], [type=color], [type=file])";
// The transcript lists this many lines around the playhead, and moves along only once the playhead's line comes within
// MARGIN of either end, so playing and scrubbing don't rebuild the whole list.
const TRANSCRIPT_WINDOW = 120;
const TRANSCRIPT_MARGIN = 25;

export function DesktopEditor({ ed }: { ed: Editor }) {
  const toast = useToast();
  const { draft, clip } = ed;
  const making = ed.source.mode === "new";
  const [selection, setSelection] = useState<Range | null>(null);
  const [selectedCut, setSelectedCut] = useState(-1);
  const [snapOn, setSnapOn] = useState(() => loadSetting("clipper.snap", true));
  const [showKeys, setShowKeys] = useState(false);
  const edge = useRef<"start" | "end">("end"); // the edge , and . nudge: the last one moved

  const toggleSnap = () =>
    setSnapOn((on) => {
      saveSetting("clipper.snap", !on);
      return !on;
    });

  const cutSelection = () => {
    if (!selection) return;
    const [a, b] = selection;
    if (b <= draft.start || a >= draft.end) {
      toast({ text: "That's outside the clip. Select part of the clip to cut it out." });
      return;
    }
    ed.cutOut(selection);
    setSelection(null);
    setSelectedCut(-1);
  };
  const newClipFrom = ([a, b]: Range) => {
    if (ed.dirty && !confirm("Leave this clip without saving your changes?")) return;
    go(`new/${ed.vodId}/${round2(a)}/${round2(b)}`);
  };
  const nudge = (seconds: number) => {
    if (edge.current === "start") ed.setStart(draft.start + seconds, false);
    else ed.setEnd(draft.end + seconds, false);
  };
  /** What ⌘↵ does: approve, make the clip, or save. */
  const primary = () => {
    if (ed.source.mode === "new") return draft.title.trim() && ed.create(true);
    const status = ed.source.clip.status;
    if (status === "proposed" || status === "extra") return draft.title.trim() && ed.decide(true);
    if (ed.dirty) return ed.save();
  };

  // --- Keys ---

  const latest = useRef({ ed, selection, selectedCut, cutSelection, nudge, primary, toggleSnap });
  latest.current = { ed, selection, selectedCut, cutSelection, nudge, primary, toggleSnap };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const { ed, selection, selectedCut, cutSelection, nudge, primary, toggleSnap } = latest.current;
      const target = e.target as HTMLElement;
      // Only a box being typed in keeps the keys: a checkbox, slider or menu that was clicked last doesn't.
      const typing = !!target.closest(TEXT_FIELDS);
      const mod = e.metaKey || e.ctrlKey;
      const key = e.key;
      if (mod && key.toLowerCase() === "s") {
        e.preventDefault();
        if (ed.source.mode === "edit") void ed.save();
        return;
      }
      if (mod && key === "Enter") {
        e.preventDefault();
        void primary();
        return;
      }
      if (typing) {
        if (key === "Escape") target.blur();
        return;
      }
      if (mod && key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) ed.redo();
        else ed.undo();
        return;
      }
      if (mod && key.toLowerCase() === "y") {
        e.preventDefault();
        ed.redo();
        return;
      }
      if (mod || e.altKey) return; // the browser's own shortcuts
      // Letters by the letter, with Shift read on its own: Caps Lock makes capitals without it.
      const k = key.length === 1 ? key.toLowerCase() : key;
      const shift = e.shiftKey;
      let handled = true;
      switch (k) {
        case " ": ed.togglePlay(); break;
        case "k": ed.pause(); break;
        case "l": ed.faster(); break;
        case "j": ed.skip(-5); break;
        case "ArrowLeft": ed.skip(shift ? -5 : -1); break;
        case "ArrowRight": ed.skip(shift ? 5 : 1); break;
        case "ArrowUp": ed.toLine(-1); break;
        case "ArrowDown": ed.toLine(1); break;
        case "[": ed.playFirst(5); break;
        case "{": ed.playFirst(10); break;
        case "]": ed.playLast(5); break;
        case "}": ed.playLast(10); break;
        case "i":
          if (shift) ed.seek(ed.draft.start);
          else {
            ed.setStart(ed.clock.get(), false, true);
            edge.current = "start";
          }
          break;
        case "o":
          if (shift) ed.seek(ed.draft.end);
          else {
            ed.setEnd(ed.clock.get(), false, true);
            edge.current = "end";
          }
          break;
        case "Home": ed.seek(ed.draft.start); break;
        case "End": ed.seek(ed.draft.end); break;
        case "p": ed.playClip(); break;
        case "r": ed.setLoop((l) => !l); break;
        case "x": if (selection) cutSelection(); else handled = false; break;
        case "Delete": case "Backspace":
          if (selectedCut >= 0) {
            ed.restoreCut(selectedCut);
            setSelectedCut(-1);
          } else if (selection) cutSelection();
          else handled = false;
          break;
        case ",": nudge(shift ? -1 : -FRAME); break;
        case "<": nudge(-1); break;
        case ".": nudge(shift ? 1 : FRAME); break;
        case ">": nudge(1); break;
        case "=": case "+": ed.zoom(1 / 1.6); break;
        case "-": case "_": ed.zoom(1.6); break;
        case "z": if (shift) ed.fitAll(); else ed.fitClip(); break;
        case "s": toggleSnap(); break;
        case "Escape": setSelection(null); setSelectedCut(-1); setShowKeys(false); break;
        case "?": setShowKeys((v) => !v); break;
        default: handled = false;
      }
      if (handled) e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const vodTitle = clip ? clip.vod_title : ed.source.mode === "new" ? ed.source.vod.title || ed.source.vod.url : "";

  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-zinc-950">
      {/* Top bar: where you are, undo, and what to do with the clip */}
      <header className="flex shrink-0 items-center gap-3 border-b border-white/5 px-3 py-2">
        <button
          onClick={() => goBack(making ? `videos/${ed.vodId}` : "inbox")}
          className="rounded-full p-2 hover:bg-white/5 active:bg-white/10"
          aria-label="Back"
          title="Back"
        >
          <BackIcon className="size-5" />
        </button>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold">{vodTitle}</p>
          <p className="truncate text-xs text-zinc-400">
            {making ? `New ${KIND_LABEL[draft.kind].toLowerCase()}` : KIND_LABEL[draft.kind]} · {timecode(ed.duration, 1)}
            {draft.cuts.length > 0 && ` · ${draft.cuts.length} part${draft.cuts.length === 1 ? "" : "s"} cut out`}
            {ed.dirty && <span className="text-amber-300"> · unsaved changes</span>}
          </p>
        </div>
        {clip && <ClipStatusChip status={clip.status} paused={clip.paused} upload={clip.upload_state} />}
        <div className="flex items-center gap-1">
          <IconButton title="Undo (⌘Z)" onClick={ed.undo} disabled={!ed.canUndo}>
            <UndoIcon className="size-4" />
          </IconButton>
          <IconButton title="Redo (⇧⌘Z)" onClick={ed.redo} disabled={!ed.canRedo}>
            <RedoIcon className="size-4" />
          </IconButton>
          <IconButton title="Keyboard shortcuts (?)" onClick={() => setShowKeys(true)}>
            <KeyboardIcon className="size-4" />
          </IconButton>
        </div>
        <div className="flex items-center gap-2">
          {actions()}
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          {/* The viewer */}
          <div className="relative flex min-h-0 flex-1 items-center justify-center bg-black/40 p-3">
            <div className="relative aspect-video max-h-full max-w-full overflow-hidden rounded-lg bg-black" style={{ height: "100%" }}>
              <video {...ed.videoProps} className="size-full cursor-pointer" onClick={ed.togglePlay} />
              {!ed.playing && !ed.videoError && (
                <div className="pointer-events-none absolute inset-0 grid place-items-center">
                  <span className="rounded-full bg-black/60 p-4">
                    <PlayIcon className="size-8" />
                  </span>
                </div>
              )}
              {ed.videoError && (
                <p className="absolute inset-0 grid place-items-center p-6 text-center text-sm text-zinc-400">
                  The preview is still being made, so this can't play yet. Read the transcript, or come back in a bit.
                </p>
              )}
              {!ed.playing && <CutBadge clock={ed.clock} cuts={draft.cuts} />}
            </div>
          </div>
          <Transport ed={ed} onEdge={(which) => (edge.current = which)} />
        </div>

        <aside className="w-[22rem] shrink-0 overflow-y-auto border-l border-white/5 xl:w-[26rem]">
          <Inspector ed={ed} selectedCut={selectedCut} onSelectCut={setSelectedCut} />
        </aside>
      </div>

      {/* The timeline */}
      <div className="shrink-0 border-t border-white/5 px-3 pb-3 pt-2">
        <div className="mb-2 flex items-center gap-2 text-xs">
          {selection ? (
            <>
              <span className="font-semibold text-sky-300">
                Selected {timecode(selection[0])}–{timecode(selection[1])} ({timecode(selection[1] - selection[0], 2)})
              </span>
              {ed.editable && (
                <ToolButton onClick={cutSelection} title="Cut this part out of the clip (X)" tone="danger">
                  <ScissorsIcon className="size-3.5" /> Cut out
                </ToolButton>
              )}
              {ed.editable && (
                <ToolButton onClick={() => { ed.trimTo(selection); setSelection(null); }} title="Make the clip exactly this part">
                  Trim to this
                </ToolButton>
              )}
              <ToolButton onClick={() => newClipFrom(selection)} title="Make a new clip of this part of the stream">
                <PlusIcon className="size-3.5" /> New clip from this
              </ToolButton>
              <ToolButton onClick={() => ed.playRange(selection[0], selection[1])} title="Play the selection">
                <PlayIcon className="size-3" /> Play
              </ToolButton>
              <ToolButton onClick={() => setSelection(null)} title="Clear the selection (Esc)">
                Clear
              </ToolButton>
            </>
          ) : selectedCut >= 0 && draft.cuts[selectedCut] ? (
            <>
              <span className="font-semibold text-red-300">
                Cut out {timecode(draft.cuts[selectedCut][0])}–{timecode(draft.cuts[selectedCut][1])}
              </span>
              <ToolButton onClick={() => ed.previewJoin(selectedCut)} title="Hear the join: a couple of seconds either side">
                <PlayIcon className="size-3" /> Hear the join
              </ToolButton>
              <ToolButton onClick={() => { ed.restoreCut(selectedCut); setSelectedCut(-1); }} title="Put this part back (Delete)">
                Put back
              </ToolButton>
            </>
          ) : (
            <span className="text-zinc-500">
              {ed.editable
                ? "Drag the yellow edges to trim. Drag across the timeline to select a part, or double-click a word, then press X to cut it out."
                : "A full episode goes up as the stream is. Select a part of it to make a clip of that."}
            </span>
          )}
          <span className="flex-1" />
          <ToolButton onClick={toggleSnap} title="Snap to words, lines, cuts and the playhead (S); hold Alt while dragging to ignore it" active={snapOn}>
            <MagnetIcon className="size-3.5" /> Snap
          </ToolButton>
          <IconButton title="Zoom out (−)" onClick={() => ed.zoom(1.6)}>
            <ZoomOutIcon className="size-4" />
          </IconButton>
          <IconButton title="Zoom in (+)" onClick={() => ed.zoom(1 / 1.6)}>
            <ZoomInIcon className="size-4" />
          </IconButton>
          <ToolButton onClick={ed.fitClip} title="Zoom to the clip (Z)">
            <FitIcon className="size-3.5" /> Clip
          </ToolButton>
          <ToolButton onClick={ed.fitAll} title="Zoom out to the whole stream (⇧Z)">
            Whole stream
          </ToolButton>
        </div>
        <Timeline
          vodId={ed.vodId}
          length={ed.length}
          view={ed.view}
          onView={ed.setView}
          onZoom={ed.zoom}
          geometry={draft}
          editable={ed.editable}
          clock={ed.clock}
          lines={ed.lines}
          selection={selection}
          onSelection={(r) => {
            setSelection(r);
            if (r) setSelectedCut(-1);
          }}
          selectedCut={selectedCut}
          onSelectCut={setSelectedCut}
          onRemoveCut={(i) => {
            ed.restoreCut(i);
            setSelectedCut(-1);
          }}
          snapOn={snapOn}
          onSeek={ed.seek}
          live={ed.live}
          onEdge={(which) => (edge.current = which)}
        />
      </div>

      {showKeys && <Shortcuts onClose={() => setShowKeys(false)} />}
    </div>
  );

  // A function rather than a component, so its buttons aren't remade each time the playhead moves.
  function actions() {
    const busy = ed.busy;
    if (ed.source.mode === "new") {
      const ready = !busy && !!draft.title.trim();
      return (
        <>
          <Button disabled={!ready} onClick={() => ed.create(false)} className="py-2">
            Save to inbox
          </Button>
          <Button tone="approve" disabled={!ready} onClick={() => ed.create(true)} className="py-2">
            Approve
          </Button>
        </>
      );
    }
    const c = ed.source.clip;
    return (
      <>
        {(c.status === "proposed" || c.status === "extra") && (
          <>
            <Button tone="danger" disabled={busy} onClick={() => ed.decide(false)} className="py-2">
              Reject
            </Button>
            {ed.dirty && (
              <Button disabled={busy} onClick={ed.save} className="py-2">
                Save
              </Button>
            )}
            <Button tone="approve" disabled={busy || !draft.title.trim()} onClick={() => ed.decide(true)} className="py-2">
              Approve
            </Button>
          </>
        )}
        {c.status === "rejected" && (
          <Button disabled={busy} onClick={ed.restore} className="py-2">
            Back to inbox
          </Button>
        )}
        {["approved", "rendering", "failed", "rendered"].includes(c.status) && (
          <Button tone="danger" disabled={busy} onClick={ed.discard} className="px-3 py-2">
            <TrashIcon className="size-4" />
            <span className="sr-only">Delete</span>
          </Button>
        )}
        {["approved", "rendering", "failed"].includes(c.status) && (
          <Button tone="primary" disabled={busy || !ed.dirty} onClick={ed.save} className="py-2">
            {!ed.dirty ? "No changes" : ed.needsRender ? "Save and render again" : "Save"}
          </Button>
        )}
        {["rendered", "uploaded"].includes(c.status) && (
          <>
            {ed.dirty && (
              <Button disabled={busy} onClick={ed.save} className="py-2">
                {ed.needsRender ? "Save and render again" : "Save"}
              </Button>
            )}
            {!(ed.dirty && ed.needsRender) && (
              <Button tone="primary" onClick={() => go(`upload/${c.id}`)} className="py-2">
                <UploadIcon className="size-4" /> {c.status === "uploaded" ? "Uploaded" : "Upload"}
              </Button>
            )}
          </>
        )}
      </>
    );
  }
}

/** The playhead's time, to the hundredth: redrawn when that changes, not with the whole transport. */
function Now({ clock }: { clock: Clock }) {
  return <>{useClock(clock, (t) => timecode(t))}</>;
}

/** Over the paused video, when the playhead is in a part cut out of the clip. */
function CutBadge({ clock, cuts }: { clock: Clock; cuts: Range[] }) {
  const inCut = useClock(clock, (t) => cuts.some(([a, b]) => t >= a && t < b));
  return inCut ? <span className="absolute right-2 top-2 rounded bg-red-500/80 px-1.5 py-0.5 text-xs font-semibold">Cut out</span> : null;
}

/** Under the viewer: the playhead's time, playing and stepping, the clip's ends to type in, speed and volume. */
function Transport({ ed, onEdge }: { ed: Editor; onEdge: (which: "start" | "end") => void }) {
  const { draft } = ed;
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-t border-white/5 px-3 py-2">
      <span className="w-24 font-mono text-base tabular-nums text-zinc-100" title="Where the playhead is">
        <Now clock={ed.clock} />
      </span>
      <div className="flex items-center gap-1">
        <IconButton title="Go to the start (⇧I)" onClick={() => ed.seek(draft.start)}>
          <ToStartIcon className="size-4" />
        </IconButton>
        <IconButton title="Back a frame" onClick={() => ed.step(-1)}>
          <FrameBackIcon className="size-4" />
        </IconButton>
        <button
          onClick={ed.togglePlay}
          title="Play or pause (Space)"
          className="grid size-9 place-items-center rounded-full bg-zinc-100 text-zinc-950 hover:bg-white"
        >
          {ed.playing ? <PauseIcon className="size-4" /> : <PlayIcon className="ml-0.5 size-4" />}
        </button>
        <IconButton title="Ahead a frame" onClick={() => ed.step(1)}>
          <FrameForwardIcon className="size-4" />
        </IconButton>
        <IconButton title="Go to the end (⇧O)" onClick={() => ed.seek(draft.end)}>
          <ToEndIcon className="size-4" />
        </IconButton>
      </div>
      <div className="flex items-center gap-1">
        <ToolButton onClick={ed.playClip} title="Play the clip as it comes out, jumping over cuts (P)">
          <PlayIcon className="size-3" /> Play clip
        </ToolButton>
        <ToolButton onClick={() => ed.setLoop((l) => !l)} active={ed.loop} title="Loop the clip (R)">
          <LoopIcon className="size-3.5" /> Loop
        </ToolButton>
      </div>
      {/* The clip's ends as they come out, to check it doesn't start late or get cut off */}
      <div className="flex items-center gap-1 text-xs">
        <span className="mr-0.5 text-zinc-500">First</span>
        <ToolButton onClick={() => ed.playFirst(5)} title="Play the clip's first 5 seconds ([)">5s</ToolButton>
        <ToolButton onClick={() => ed.playFirst(10)} title="Play the clip's first 10 seconds (⇧[)">10s</ToolButton>
        <span className="ml-2 mr-0.5 text-zinc-500">Last</span>
        <ToolButton onClick={() => ed.playLast(5)} title="Play the clip's last 5 seconds (])">5s</ToolButton>
        <ToolButton onClick={() => ed.playLast(10)} title="Play the clip's last 10 seconds (⇧])">10s</ToolButton>
      </div>
      {ed.editable && (
        <div className="flex items-center gap-2 text-xs">
          <TimeField label="In" value={draft.start} onSet={(t) => { ed.setStart(t, false, true); onEdge("start"); }} hint="Set to the playhead (I)" onHere={() => { ed.setStart(ed.clock.get(), false, true); onEdge("start"); }} />
          <TimeField label="Out" value={draft.end} onSet={(t) => { ed.setEnd(t, false, true); onEdge("end"); }} hint="Set to the playhead (O)" onHere={() => { ed.setEnd(ed.clock.get(), false, true); onEdge("end"); }} />
          <span className="text-zinc-500">
            Length <span className="font-mono tabular-nums text-zinc-200">{timecode(ed.duration)}</span>
          </span>
        </div>
      )}
      <span className="flex-1" />
      <select
        value={ed.rate}
        onChange={(e) => ed.setRate(Number(e.target.value))}
        title="Playback speed (L plays faster)"
        className="rounded-lg bg-zinc-800 px-2 py-1.5 text-xs font-semibold outline-none hover:bg-zinc-700/80 focus:ring-2 focus:ring-violet-500"
      >
        {RATES.map((r) => (
          <option key={r} value={r}>
            {r}×
          </option>
        ))}
      </select>
      <div className="flex items-center gap-1.5" title="Preview volume only. The clip's own sound isn't changed.">
        <IconButton
          title={ed.muted ? "Unmute" : "Mute"}
          onClick={() => {
            if (ed.muted && ed.volume === 0) ed.setVolume(0.5);
            ed.setMuted((m) => !m);
          }}
        >
          {ed.muted || ed.volume === 0 ? <MutedIcon className="size-4" /> : <VolumeIcon className="size-4" />}
        </IconButton>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={ed.muted ? 0 : ed.volume}
          onChange={(e) => {
            const level = Number(e.target.value);
            ed.setVolume(level);
            ed.setMuted(level === 0);
          }}
          aria-label="Preview volume"
          className="hidden h-1.5 w-20 accent-violet-500 2xl:block"
        />
      </div>
    </div>
  );
}

/** A clip's start or end, typed as a time ("1:02:03.45"), or set to the playhead. */
function TimeField(props: { label: string; value: number; onSet: (t: number) => void; onHere: () => void; hint: string }) {
  const [text, setText] = useState<string | null>(null);
  const [bad, setBad] = useState(false);
  const commit = () => {
    if (text === null) return;
    const t = parseTimecode(text);
    if (t === null) {
      setBad(true);
      return;
    }
    props.onSet(t);
    setText(null);
    setBad(false);
  };
  return (
    <span className="flex items-center gap-1">
      <button onClick={props.onHere} title={props.hint} className="rounded-md px-1.5 py-1 font-semibold text-zinc-400 hover:bg-white/5 hover:text-zinc-200">
        {props.label}
      </button>
      <input
        value={text ?? timecode(props.value)}
        onFocus={(e) => {
          setText(timecode(props.value));
          e.currentTarget.select();
        }}
        onChange={(e) => {
          setText(e.target.value);
          setBad(false);
        }}
        onBlur={() => {
          commit();
          setText(null);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            commit();
            e.currentTarget.blur();
          }
        }}
        title="Type a time, like 1:02:03.45"
        className={cn(
          "w-[5.5rem] rounded-md bg-zinc-900 px-2 py-1 font-mono text-xs tabular-nums outline-none ring-1 focus:ring-2",
          bad ? "ring-red-500" : "ring-white/10 focus:ring-violet-500",
        )}
      />
    </span>
  );
}

/** The clip's details, its cuts, and the transcript. */
function Inspector({ ed, selectedCut, onSelectCut }: { ed: Editor; selectedCut: number; onSelectCut: (i: number) => void }) {
  const { draft, setDraft, clip, destinations } = ed;
  return (
    <div className="space-y-5 p-4">
      {/* First, since it's what to judge the clip against. */}
      {clip?.why && (
        <div className="rounded-xl bg-panel p-3.5 text-sm leading-snug">
          <p className="mb-1 flex items-center gap-1.5 text-[13px] font-medium text-graphite">
            <SparkIcon className="size-3.5" /> Why the AI picked it
          </p>
          {clip.why}
        </div>
      )}
      {ed.source.mode === "new" && (
        <Field label="What it is">
          <div className="grid grid-cols-2 gap-1 rounded-xl bg-zinc-800 p-1">
            {(["highlight", "chapter"] as const).map((kind) => (
              <button
                key={kind}
                onClick={() => setDraft((d) => ({ ...d, kind, make_short: kind === "highlight" && d.make_short }))}
                className={cn("rounded-lg py-1.5 text-sm font-semibold", draft.kind === kind ? "bg-zinc-700 text-chalk shadow-[inset_0_-2px_0_var(--color-mark)]" : "text-zinc-400 hover:text-zinc-200")}
              >
                {kind === "highlight" ? "Highlight" : "Clip"}
              </button>
            ))}
          </div>
        </Field>
      )}

      {ed.source.mode === "new" ? (
        <Field label="Title" hint={`${draft.title.length}/${ed.source.info.title_room}`}>
          <input
            value={draft.title}
            maxLength={ed.source.info.title_room}
            onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value }))}
            placeholder="What happens in it"
            className="w-full rounded-xl bg-zinc-900 px-3 py-2.5 text-sm font-semibold outline-none ring-1 ring-white/10 placeholder:font-normal placeholder:text-zinc-500 focus:ring-2 focus:ring-violet-500"
          />
          {ed.source.info.title_pattern !== "{title}" && (
            <span className="mt-1.5 block text-xs text-zinc-500">Goes up as “{ed.fullTitle || "…"}”</span>
          )}
        </Field>
      ) : (
        <Field label="Title" hint={`${draft.title.length}/100`}>
          <textarea
            value={draft.title}
            maxLength={100}
            rows={2}
            onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value.replace(/\s*\n\s*/g, " ") }))}
            className="w-full resize-none rounded-xl bg-zinc-900 px-3 py-2.5 text-sm font-semibold leading-snug outline-none ring-1 ring-white/10 focus:ring-2 focus:ring-violet-500"
          />
        </Field>
      )}
      <Field label="Description">
        <textarea
          value={draft.description}
          rows={4}
          onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))}
          className="w-full rounded-xl bg-zinc-900 px-3 py-2.5 text-sm outline-none ring-1 ring-white/10 focus:ring-2 focus:ring-violet-500"
        />
        {clip?.description_footer && !draft.description.includes(clip.description_footer) && (
          <p className="mt-1.5 line-clamp-2 text-xs text-zinc-500">Goes up with the show's footer under it: {clip.description_footer}</p>
        )}
      </Field>

      {draft.kind === "highlight" && (
        <label className="flex items-center gap-3 rounded-xl bg-zinc-900 p-3 ring-1 ring-white/10">
          <PhoneIcon className="size-5 text-zinc-400" />
          <span className="flex-1">
            <span className="block text-sm font-semibold">Also make a vertical Short</span>
            <span className="block text-xs text-zinc-500">
              {ed.duration > MAX_SHORT_S ? "Shorts can be at most 3 minutes" : "1080×1920 with word-by-word captions"}
            </span>
          </span>
          <input
            type="checkbox"
            checked={draft.make_short}
            disabled={ed.duration > MAX_SHORT_S}
            onChange={(e) => setDraft((d) => ({ ...d, make_short: e.target.checked }))}
            className="size-4 accent-violet-500"
          />
        </label>
      )}

      <Field label="Upload to">
        <DestinationSelect
          value={draft.destination_id}
          destinations={destinations}
          onChange={(destination_id) => setDraft((d) => ({ ...d, destination_id, playlist_id: "" }))}
          className="py-2.5 text-sm"
        />
        <PlaylistSelect
          destination={destinations?.find((d) => d.id === draft.destination_id)}
          value={draft.playlist_id}
          onChange={(playlist_id) => setDraft((d) => ({ ...d, playlist_id }))}
        />
      </Field>

      {clip?.manual && <p className="text-xs text-zinc-500">You made this clip, so it has no score from the AI.</p>}
      {clip?.status === "extra" && (
        <p className="rounded-xl bg-zinc-900 p-3 text-sm text-zinc-400 ring-1 ring-white/10">
          An extra: the AI scored it below this show's best few for the stream, so it isn't in your inbox. Approve it to
          use it anyway.
        </p>
      )}
      {clip?.status === "failed" && <ErrorNote>{clip.error}</ErrorNote>}

      {ed.editable && (
        <section>
          <div className="mb-1.5 flex items-center gap-2">
            <h2 className="flex-1 text-xs font-medium text-zinc-500">Cut out · {draft.cuts.length}</h2>
            <Tighten ed={ed} />
          </div>
          {draft.cuts.length === 0 ? (
            <p className="text-xs text-zinc-500">
              Nothing yet. Select a part on the timeline, or a line below, and cut it out: the video jumps over it.
            </p>
          ) : (
            <ul className="divide-y divide-white/5 overflow-hidden rounded-xl bg-zinc-900 ring-1 ring-white/10">
              {draft.cuts.map(([a, b], i) => (
                <li key={`${a}-${b}`} className={cn("flex items-center gap-2 px-3 py-2 text-xs", selectedCut === i && "bg-red-500/10")}>
                  <button onClick={() => { onSelectCut(i); ed.seek(a); }} className="flex-1 text-left font-mono tabular-nums text-zinc-300 hover:text-white">
                    {timecode(a)}–{timecode(b)} <span className="text-zinc-500">({timecode(b - a, 1)})</span>
                  </button>
                  <ToolButton onClick={() => ed.previewJoin(i)} title="Hear the join">
                    <PlayIcon className="size-3" />
                  </ToolButton>
                  <ToolButton onClick={() => ed.restoreCut(i)} title="Put this part back">
                    Put back
                  </ToolButton>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <TranscriptPanel ed={ed} />
    </div>
  );
}

const PAUSES = [1, 1.5, 2, 3]; // seconds: how long a pause between words has to be for Tighten to cut it out

/** Cut out the pauses between words at once, of at least the length picked (remembered on this device). */
function Tighten({ ed }: { ed: Editor }) {
  const [pause, setPause] = useState(() => loadSetting("clipper.tightenPause", 1));
  return (
    <span className="flex items-center gap-1">
      <select
        value={pause}
        onChange={(e) => {
          setPause(Number(e.target.value));
          saveSetting("clipper.tightenPause", Number(e.target.value));
        }}
        title="How long a pause between words has to be to cut it out"
        className="rounded-md bg-zinc-800 px-1.5 py-1 text-xs font-semibold text-zinc-300 outline-none hover:bg-zinc-700/80 focus:ring-2 focus:ring-violet-500"
      >
        {PAUSES.map((s) => (
          <option key={s} value={s}>
            pauses ≥ {s}s
          </option>
        ))}
      </select>
      <ToolButton
        onClick={() => void ed.tighten(pause)}
        title="Cut out every pause between words at least this long, leaving a moment either side. Undo puts them all back."
      >
        <ScissorsIcon className="size-3.5" /> Tighten
      </ToolButton>
    </span>
  );
}

/** The transcript around the playhead, or a search of the whole stream. A line plays when clicked, and can set the
 * clip's ends or be cut out. */
function TranscriptPanel({ ed }: { ed: Editor }) {
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const list = useRef<HTMLOListElement>(null);
  const q = query.trim().toLowerCase();
  const found = useMemo(
    () => (q.length >= 2 ? ed.lines.filter((l) => l.text.toLowerCase().includes(q)).slice(0, 200) : null),
    [q, ed.lines],
  );
  // The lines around the playhead's line. The window moves only once the playhead has stayed put a moment (so a scrub
  // across the stream rebuilds the list once, at the end, not at every step) and is near either end of it.
  const at = useSettled(useClock(ed.clock, (t) => indexAt(ed.lines, t)), 200);
  const first = useRef(0);
  const last = Math.max(0, ed.lines.length - TRANSCRIPT_WINDOW);
  if (at >= 0 && (at < first.current + TRANSCRIPT_MARGIN || at >= first.current + TRANSCRIPT_WINDOW - TRANSCRIPT_MARGIN)) {
    first.current = Math.min(last, Math.max(0, at - Math.floor(TRANSCRIPT_WINDOW / 3)));
  }
  first.current = Math.min(first.current, last);
  const start = first.current;
  const near = useMemo(() => ed.lines.slice(start, start + TRANSCRIPT_WINDOW), [ed.lines, start]);
  const shown = found ?? near;
  const current = useClock(ed.clock, (t) => startAt(ed.lines, t));

  // Keep the line being played in sight, scrolling only the transcript's own box (not the details above it).
  useEffect(() => {
    const box = list.current;
    const row = box?.querySelector<HTMLElement>(`[data-start="${current}"]`);
    if (!follow || found || !box || !row) return;
    if (row.offsetTop < box.scrollTop || row.offsetTop + row.offsetHeight > box.scrollTop + box.clientHeight) {
      box.scrollTop = row.offsetTop - box.clientHeight / 3;
    }
  }, [current, follow, found]);

  const { draft } = ed;
  return (
    <section>
      <div className="mb-1.5 flex items-center gap-2">
        <h2 className="flex-1 text-xs font-medium text-zinc-500">Transcript</h2>
        <label className="flex items-center gap-1.5 text-xs text-zinc-500">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} className="accent-violet-500" />
          Follow the playhead
        </label>
      </div>
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search what was said in the whole stream"
        className="mb-2 w-full rounded-lg bg-zinc-900 px-3 py-2 text-sm outline-none ring-1 ring-white/10 placeholder:text-zinc-500 focus:ring-2 focus:ring-violet-500"
      />
      {found && <p className="mb-1.5 text-xs text-zinc-500">{found.length === 0 ? "Nothing like that was said." : `${found.length}${found.length === 200 ? "+" : ""} found: click one to go there.`}</p>}
      {!found && ed.lines.length === 0 && <p className="text-xs text-zinc-500">No transcript for this stream.</p>}
      <ol ref={list} className="relative max-h-[28rem] divide-y divide-white/5 overflow-y-auto rounded-xl bg-zinc-900 ring-1 ring-white/10">
        {shown.map((l) => (
          <TranscriptRow
            key={l.start}
            line={l}
            current={l.start === current}
            inside={l.end > draft.start && l.start < draft.end}
            cut={draft.cuts.some(([a, b]) => l.start >= a && l.end <= b)}
            editable={ed.editable}
            onPlay={() => {
              if (found) ed.setView({ from: l.start - 30, to: l.end + 30 });
              ed.playRange(l.start, l.end + 0.3);
            }}
            onIn={() => ed.setStart(l.start - 0.2, false, true)}
            onOut={() => ed.setEnd(l.end + 0.3, false, true)}
            onCut={() => ed.cutOut([l.start, l.end])}
          />
        ))}
      </ol>
    </section>
  );
}

const TranscriptRow = memo(function TranscriptRow(props: {
  line: Line;
  current: boolean;
  inside: boolean;
  cut: boolean;
  editable: boolean;
  onPlay: () => void;
  onIn: () => void;
  onOut: () => void;
  onCut: () => void;
}) {
  const { line: l } = props;
  const mini = "rounded px-1.5 py-0.5 text-[11px] font-semibold text-zinc-300 hover:bg-zinc-700";
  return (
    <li data-start={l.start} className={cn("group flex items-start gap-2 px-3 py-1.5", props.current && "bg-violet-500/15")}>
      <button onClick={props.onPlay} className={cn("flex-1 text-left", !props.inside && "opacity-45", props.cut && "line-through opacity-40")}>
        <span className="mr-2 font-mono text-[11px] tabular-nums text-zinc-500">{timecode(l.start, 0)}</span>
        <span className="text-[13px] leading-snug">{l.text}</span>
      </button>
      {props.editable && (
        <span className="flex shrink-0 gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
          <button className={mini} onClick={props.onIn} title="Start the clip here">In</button>
          <button className={mini} onClick={props.onOut} title="End the clip here">Out</button>
          <button className={cn(mini, "text-red-300")} onClick={props.onCut} title="Cut this line out">Cut</button>
        </span>
      )}
    </li>
  );
}, (a, b) => a.line === b.line && a.current === b.current && a.inside === b.inside && a.cut === b.cut && a.editable === b.editable);

const KEYS: [string, string][] = [
  ["Space", "Play or pause"],
  ["J / K / L", "Back 5 seconds / pause / play (again: faster)"],
  ["← / →", "Back or ahead a second (with ⇧, 5 seconds)"],
  ["↑ / ↓", "To the start of this or the last line of the transcript / the next one"],
  ["[ / ]", "Play the clip's first / last 5 seconds (with ⇧, 10)"],
  ["I / O", "Start or end the clip at the playhead"],
  ["⇧I / ⇧O", "Go to the clip's start or end"],
  [", / .", "Nudge the last edge you moved by a frame (< and >: a second)"],
  ["P", "Play the clip as it comes out"],
  ["R", "Loop the clip"],
  ["Drag on the timeline", "Select a part (double-click: a word or line)"],
  ["X", "Cut the selected part out"],
  ["Delete", "Put a selected cut back"],
  ["S", "Snapping on or off (hold Alt while dragging to ignore it)"],
  ["+ / −", "Zoom in or out (or ⌘ / ctrl with the scroll wheel)"],
  ["Z / ⇧Z", "Zoom to the clip / the whole stream"],
  ["⌘Z / ⇧⌘Z", "Undo / redo"],
  ["⌘S", "Save"],
  ["⌘↵", "Approve (or make the new clip)"],
  ["Esc", "Clear the selection"],
];

function Shortcuts({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-6" onClick={onClose}>
      <div className="w-full max-w-lg rounded-2xl bg-zinc-900 p-5 shadow-2xl ring-1 ring-white/10" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center">
          <h2 className="flex-1 text-base font-bold">Keyboard shortcuts</h2>
          <button onClick={onClose} className="rounded-lg px-2 py-1 text-sm text-zinc-400 hover:bg-white/5">
            Close
          </button>
        </div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
          {KEYS.map(([key, what]) => (
            <div key={key} className="contents">
              <dt className="whitespace-nowrap font-mono text-xs leading-5 text-violet-200">{key}</dt>
              <dd className="text-zinc-300">{what}</dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 flex justify-between text-xs font-medium text-zinc-500">
        {label}
        {hint && <span className="font-normal normal-case tabular-nums">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

function IconButton(props: { title: string; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return (
    <button
      onClick={props.onClick}
      disabled={props.disabled}
      title={props.title}
      aria-label={props.title}
      // Clicking doesn't take the focus, so Space still plays rather than pressing the button again.
      onMouseDown={(e) => e.preventDefault()}
      className="grid size-8 place-items-center rounded-lg text-zinc-300 enabled:hover:bg-white/5 enabled:hover:text-white disabled:opacity-30"
    >
      {props.children}
    </button>
  );
}

function ToolButton(props: { onClick: () => void; title: string; active?: boolean; tone?: "danger"; children: ReactNode }) {
  return (
    <button
      onClick={props.onClick}
      title={props.title}
      onMouseDown={(e) => e.preventDefault()}
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-semibold",
        props.active
          ? "bg-violet-500/20 text-violet-200 ring-1 ring-violet-400/40"
          : props.tone === "danger"
            ? "bg-red-500/15 text-red-200 hover:bg-red-500/25"
            : "bg-zinc-800 text-zinc-300 hover:bg-zinc-700/80 hover:text-white",
      )}
    >
      {props.children}
    </button>
  );
}
