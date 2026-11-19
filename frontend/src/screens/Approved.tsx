import type { ReactNode } from "react";
import { api, errorText, type Clip, type UploadState } from "../api";
import { Thumb } from "../ClipCard";
import { keptLength } from "../editor/edit";
import { InboxIcon, LinkIcon, PauseIcon, PlayIcon, RefreshIcon, TrashIcon, UploadIcon } from "../icons";
import { cn, firstSentence, length, upcoming, usePoll } from "../lib";
import { WorkStrip } from "../Work";
import { ErrorNote, Fold, KIND_LABEL, Screen, ShowMore, useToast } from "../ui";

// Rendered clips whose upload has been started, in the order they go up; a failed one is back to ready.
export const GOING_UP: UploadState[] = ["uploading", "queued", "scheduled", "retrying"];

/** Clips whose upload has started, in the order they go up: the one uploading, the one next, then the scheduled ones and
 * the retries together by when they go (a retry keeps its place in the queue, so newer clips can come after it). */
const STARTED_RANK: Partial<Record<UploadState, number>> = { uploading: 0, queued: 1 };
export const inUploadOrder = (a: Clip, b: Clip) =>
  (STARTED_RANK[a.upload_state] ?? 2) - (STARTED_RANK[b.upload_state] ?? 2) ||
  (a.next_upload ?? "").localeCompare(b.next_upload ?? "");

/**
 * Approved clips, by what each needs: the failures first, so they don't get lost among the rest, then the ones
 * ready to upload, the ones going up, the ones being rendered, and the uploaded ones folded away at the bottom.
 */
export function Approved() {
  const { data: clips, error, refresh } = usePoll(
    () => api.clips(["approved", "rendering", "failed", "rendered", "uploaded"]),
    5000,
  );
  const rendered = clips?.filter((c) => c.status === "rendered") ?? [];
  const attention = clips?.filter((c) => c.status === "failed" || (c.status === "rendered" && c.upload_state === "failed")) ?? [];
  const ready = rendered.filter((c) => !GOING_UP.includes(c.upload_state) && c.upload_state !== "failed");
  const scheduled = rendered.filter((c) => GOING_UP.includes(c.upload_state)).sort(inUploadOrder);
  const working = clips?.filter((c) => ["approved", "rendering"].includes(c.status)) ?? [];
  const uploaded = clips?.filter((c) => c.status === "uploaded") ?? [];

  return (
    <Screen title="Upload" subtitle="Approved clips, ready to post to YouTube." wide>
      <div className="lg:max-w-md">
        <WorkStrip stacked parts={["renders", "uploads"]} className="rounded-2xl bg-panel p-1.5 lg:hidden" />
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}
      {clips?.length === 0 && <p className="mt-10 text-center text-sm text-graphite">Nothing approved yet.</p>}
      <div className="lg:max-w-5xl">
        <Section id="upload:attention" title="Needs you" tone="bg-tally/15 text-red-300" clips={attention}
          row={(c) => (c.status === "failed" ? <RenderRow clip={c} onChanged={refresh} /> : <UploadRow clip={c} onChanged={refresh} />)} />
        <Section id="upload:ready" title="Ready" clips={ready} row={(c) => <UploadRow clip={c} onChanged={refresh} />} />
        <Section id="upload:scheduled" title="Going up" clips={scheduled} row={(c) => <UploadRow clip={c} onChanged={refresh} />} />
        <Section id="upload:rendering" title="Rendering" clips={working} row={(c) => <RenderRow clip={c} onChanged={refresh} />} />
        {/* Grows with every upload, so it starts folded and shows a dozen at a time. */}
        <Section id="upload:uploaded" title="Uploaded" clips={uploaded} defaultOpen={false} row={(c) => <UploadedRow clip={c} />} />
      </div>
    </Screen>
  );
}

function Section(props: {
  id: string;
  title: string;
  clips: Clip[];
  tone?: string;
  defaultOpen?: boolean;
  row: (c: Clip) => ReactNode;
}) {
  if (props.clips.length === 0) return null;
  return (
    <Fold id={props.id} title={props.title} count={props.clips.length} tone={props.tone} defaultOpen={props.defaultOpen}>
      <ShowMore items={props.clips}>
        {(shown) => (
          <ul className="divide-y divide-line">
            {shown.map((c) => (
              <li key={c.id}>{props.row(c)}</li>
            ))}
          </ul>
        )}
      </ShowMore>
    </Fold>
  );
}

/**
 * A clip in the queue: its still and title (either opens its upload page), where it goes, how it stands, and its
 * buttons, down the right on a desktop and under it on a phone.
 */
function Row({ clip, status, children }: { clip: Clip; status: ReactNode; children: ReactNode }) {
  const to = `#/upload/${clip.id}`;
  const duration = length(clip.cuts?.length ? keptLength(clip) : clip.end - clip.start);
  return (
    <div className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-3 gap-y-3 py-3.5 sm:grid-cols-[11rem_minmax(0,1fr)] sm:gap-x-4 lg:grid-cols-[12rem_minmax(0,1fr)_auto]">
      <a href={to} tabIndex={-1} aria-hidden="true" className="relative block aspect-video self-start overflow-hidden rounded-lg bg-panel">
        <Thumb clip={clip} />
        <span className="absolute bottom-1.5 left-1.5 rounded bg-ink/85 px-1.5 py-0.5 font-mono text-[11px]">{duration}</span>
      </a>
      <div className="min-w-0">
        <a href={to} className="line-clamp-2 font-semibold leading-snug hover:underline">
          {clip.title}
        </a>
        <p className="mt-0.5 truncate text-[13px] text-graphite">
          {clip.kind === "whole" && `${KIND_LABEL.whole} to `}
          {clip.kind === "compilation" && `Compilation of ${clip.compilation_clips} clips to `}
          {clip.destination_name || "No channel chosen yet"}
          {clip.has_short && " + Short"}
        </p>
        <div className="mt-1.5 text-[13px] leading-snug">{status}</div>
      </div>
      <div className="col-span-2 flex flex-wrap items-center gap-1.5 lg:col-span-1 lg:flex-nowrap lg:self-start">{children}</div>
    </div>
  );
}

/** A row's button. `main` is the one thing to do next with it; `danger` deletes. */
function Act(props: { onClick?: () => void; href?: string; external?: boolean; tone?: "main" | "danger" | "good"; children: ReactNode }) {
  const className = cn(
    "inline-flex min-h-9 items-center justify-center gap-1.5 rounded-lg px-3 text-[13px] font-semibold",
    props.tone === "main"
      ? "bg-panel-2 text-chalk hover:bg-zinc-700"
      : props.tone === "danger"
        ? "text-graphite hover:bg-tally/10 hover:text-red-300"
        : props.tone === "good"
          ? "text-emerald-300 hover:bg-cue/10"
          : "text-graphite hover:bg-white/5 hover:text-chalk",
  );
  if (props.href) {
    return (
      <a href={props.href} className={className} {...(props.external && { target: "_blank", rel: "noreferrer" })}>
        {props.children}
      </a>
    );
  }
  return (
    <button type="button" onClick={props.onClick} className={className}>
      {props.children}
    </button>
  );
}

/** Runs a clip action with a toast, and an undo where there is one. */
function useClipAction(onChanged: () => void) {
  const toast = useToast();
  return async (work: () => Promise<unknown>, done?: string, undo?: () => Promise<unknown>) => {
    try {
      await work();
      if (done) toast({ text: done, action: undo && { label: "Undo", run: () => void undo().then(onChanged) } });
      onChanged();
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };
}

/** For a clip of a compiling stream that's posted by itself too: back to being in the compilation only. */
function CompilationOnly({ clip, act }: { clip: Clip; act: ReturnType<typeof useClipAction> }) {
  if (!clip.in_compilation) return null;
  return (
    <Act onClick={() => act(() => api.keep(clip.id), "It stays in the compilation and won't be posted by itself", () => api.approve(clip.id))}>
      Compilation only
    </Act>
  );
}

/** What's happening with a clip's upload, and in what colour. */
function uploadLine(clip: Clip): [string, string] {
  const at = clip.next_upload ? upcoming(clip.next_upload) : "";
  return {
    "": ["Not started yet", "text-graphite"],
    uploading: ["Uploading now", "text-mark"],
    queued: ["Goes up as soon as the upload running is done", "text-chalk"],
    scheduled: [`Goes up ${at}`, "text-chalk"],
    retrying: [`Retries ${at}, in its turn${clip.upload_error ? `. Failed: ${firstSentence(clip.upload_error)}` : ""}`, "text-amber-300"],
    failed: [clip.upload_error ? `Upload failed: ${firstSentence(clip.upload_error)}` : "Upload failed", "text-red-300"],
  }[clip.upload_state] as [string, string];
}

function UploadRow({ clip, onChanged }: { clip: Clip; onChanged: () => void }) {
  const act = useClipAction(onChanged);
  const [line, tone] = uploadLine(clip);
  const started = GOING_UP.includes(clip.upload_state);
  return (
    <Row clip={clip} status={<p className={tone}>{line}</p>}>
      <Act href={`#/upload/${clip.id}`} tone="main">
        {clip.upload_state === "failed" ? <RefreshIcon className="size-4" /> : <UploadIcon className="size-4" />}
        {started ? "Open" : clip.upload_state === "failed" ? "Try again" : "Upload"}
      </Act>
      <Act onClick={() => act(() => api.restore(clip.id), "Back in the inbox. Its render was deleted.")}>
        <InboxIcon className="size-4" /> To inbox
      </Act>
      <CompilationOnly clip={clip} act={act} />
      <Act tone="danger" onClick={() => act(() => api.reject(clip.id), "Deleted", () => api.approve(clip.id))}>
        <TrashIcon className="size-4" /> Delete
      </Act>
    </Row>
  );
}

function RenderRow({ clip, onChanged }: { clip: Clip; onChanged: () => void }) {
  const act = useClipAction(onChanged);
  const failed = clip.status === "failed";
  const status = failed ? (
    <p className="line-clamp-3 break-words text-red-300">Render failed: {clip.error}</p>
  ) : clip.paused ? (
    <p className="text-amber-300">{clip.status === "rendering" ? "Pausing…" : "Render paused"}</p>
  ) : clip.status === "rendering" ? (
    <p className="text-mark">Rendering now</p>
  ) : (
    <p className="text-graphite">Waiting its turn to render</p>
  );
  return (
    <Row clip={clip} status={status}>
      {failed ? (
        <Act tone="main" onClick={() => act(() => api.approve(clip.id))}>
          <RefreshIcon className="size-4" /> Render again
        </Act>
      ) : (
        <Act tone="main" onClick={() => act(() => (clip.paused ? api.resumeClip(clip.id) : api.pauseClip(clip.id)))}>
          {clip.paused ? <PlayIcon className="size-4" /> : <PauseIcon className="size-4" />}
          {clip.paused ? "Resume" : "Pause"}
        </Act>
      )}
      <Act onClick={() => act(() => api.restore(clip.id), "Back in the inbox")}>
        <InboxIcon className="size-4" /> To inbox
      </Act>
      <CompilationOnly clip={clip} act={act} />
    </Row>
  );
}

function UploadedRow({ clip }: { clip: Clip }) {
  return (
    <Row clip={clip} status={<p className="text-emerald-300">Uploaded</p>}>
      {clip.youtube_url && (
        <Act href={clip.youtube_url} external tone="good">
          <LinkIcon className="size-4" /> On YouTube
        </Act>
      )}
      {clip.rumble_url && (
        <Act href={clip.rumble_url} external tone="good">
          <LinkIcon className="size-4" /> On Rumble
        </Act>
      )}
    </Row>
  );
}
