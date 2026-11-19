import { forwardRef, useRef, useState, type PointerEvent, type ReactNode } from "react";
import { media, type Clip } from "./api";
import { CheckIcon, PhoneIcon, ScissorsIcon, UploadIcon, XIcon } from "./icons";
import { keptLength } from "./editor/edit";
import { clock, cn, go, length } from "./lib";
import { ClipStatusChip, KIND_LABEL, isPick } from "./ui";

const SWIPE_DISTANCE = 96; // px to approve (right) or reject (left)

/** A clip in a list, such as a video's clips: tap to open its editor. */
export function ClipCard({ clip, showStatus }: { clip: Clip; showStatus?: boolean }) {
  const duration = length(clip.cuts?.length ? keptLength(clip) : clip.end - clip.start);
  return (
    <button
      onClick={() => go(`clip/${clip.id}`)}
      className="flex w-full gap-3 rounded-xl bg-panel p-3 text-left hover:bg-zinc-800 active:bg-zinc-800"
    >
      <div className="relative aspect-video w-36 shrink-0 overflow-hidden rounded-lg bg-panel-2">
        <Thumb clip={clip} />
        <span className="absolute bottom-1 left-1 rounded bg-ink/85 px-1.5 py-0.5 font-mono text-[11px]">{duration}</span>
      </div>
      <div className="min-w-0 flex-1">
        <ClipMeta clip={clip} />
        <h3 className="mt-1 line-clamp-2 text-[15px] font-semibold leading-snug">{clip.title}</h3>
        <p className="mt-1 truncate text-xs text-graphite">
          {!isPick(clip) ? clip.vod_title : <><span className="font-mono">{clock(clip.start)}</span> in {clip.vod_title}</>}
        </p>
        {showStatus && (
          <div className="mt-2">
            <ClipStatusChip status={clip.status} paused={clip.paused} upload={clip.upload_state} />
          </div>
        )}
      </div>
    </button>
  );
}

/** A still from the clip, or a plain frame while there's none (it's made from the preview, which may not be ready). */
export function Thumb({ clip, className }: { clip: Clip; className?: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <div className="size-full bg-[linear-gradient(160deg,var(--color-panel-2),var(--color-bay))]" />;
  return <img src={media.thumb(clip)} alt="" loading="lazy" onError={() => setFailed(true)} className={cn("size-full object-cover", className)} />;
}

/** The AI's score (0 to 1, the scale a show's automatic uploads go by), as a number and ten ticks. */
export function Score({ clip }: { clip: Clip }) {
  if (clip.manual) return <span className="text-[13px] text-graphite">Made by you</span>;
  if (!isPick(clip)) return null;
  const filled = Math.round(clip.score * 10);
  return (
    <span className="inline-flex items-center gap-2 text-[13px] text-graphite" title={`The AI scored it ${clip.score.toFixed(2)}`}>
      AI score <span className="font-mono text-chalk">{clip.score.toFixed(2)}</span>
      <span className="flex gap-0.5" aria-hidden="true">
        {Array.from({ length: 10 }, (_, i) => (
          <i key={i} className={cn("h-3 w-1 rounded-[1px]", i < filled ? "bg-chalk" : "bg-panel-2")} />
        ))}
      </span>
    </span>
  );
}

/** The slate: where a clip goes in and out of its stream, how long it runs, and a bar showing where it falls. */
export function Slate({ clip }: { clip: Clip }) {
  const total = clip.vod_duration || clip.end;
  const left = Math.min(100, (clip.start / total) * 100);
  const width = Math.max(0.6, ((clip.end - clip.start) / total) * 100);
  return (
    <div>
      <div className="flex items-baseline gap-2 text-[13px]">
        <span className="font-mono">{clock(clip.start)}</span>
        <span className="text-graphite">to</span>
        <span className="font-mono">{clock(clip.end)}</span>
        <span className="ml-auto font-mono text-graphite">{length(clip.cuts?.length ? keptLength(clip) : clip.end - clip.start)}</span>
      </div>
      <div className="relative mt-2 h-1 rounded-full bg-panel-2" title="Where it is in the stream">
        <i className="absolute -top-0.5 h-2 rounded-sm bg-mark" style={{ left: `${left}%`, width: `${width}%` }} />
      </div>
    </div>
  );
}

/**
 * A clip waiting in the inbox, with what's needed to decide on it there: the AI's reason and score, and the slate.
 * Swipe it, or use its buttons, to approve or reject it; tap it to trim and edit. On a desktop it's a row, with the
 * buttons down its right side, and `selected` outlines the one the keyboard is on.
 *
 * A clip of a stream that makes a compilation (`clip.in_compilation`) says Keep instead of Approve, and has a quiet
 * `onKeepAndPost` under it. A compilation shows `strip` (its CompilationStrip) where a clip has its slate.
 */
export const ReviewCard = forwardRef<
  HTMLElement,
  {
    clip: Clip;
    selected?: boolean;
    onApprove: () => void; // Keep, for a clip in a compilation
    onKeepAndPost?: () => void;
    onReject: () => void;
    onHover?: (on: boolean) => void;
    strip?: ReactNode;
  }
>(function ReviewCard({ clip, selected, onApprove, onKeepAndPost, onReject, onHover, strip }, ref) {
  const swipe = useSwipe(onApprove, onReject);
  const open = () => go(`clip/${clip.id}`);
  const duration = length(clip.cuts?.length ? keptLength(clip) : clip.end - clip.start);
  const approve = clip.in_compilation ? "Keep" : "Approve";
  const compilation = clip.kind === "compilation";
  const why = compilation ? `The ${clip.compilation_clips} kept clips of this stream, in order.` : clip.why;
  return (
    <article
      ref={ref}
      className="relative overflow-hidden lg:rounded-xl"
      aria-current={selected || undefined}
      onPointerEnter={onHover && ((e) => e.pointerType === "mouse" && onHover(true))}
      onPointerLeave={onHover && (() => onHover(false))}
    >
      {swipe.dx !== 0 && (
        <div
          className={cn(
            "absolute inset-0 flex items-center px-6 text-sm font-semibold",
            swipe.dx > 0 ? "justify-start bg-cue/25 text-emerald-300" : "justify-end bg-tally/25 text-red-300",
          )}
        >
          <span className="flex items-center gap-2" style={{ opacity: Math.min(1, Math.abs(swipe.dx) / SWIPE_DISTANCE) }}>
            {swipe.dx > 0 ? <CheckIcon /> : <XIcon />}
            {swipe.dx > 0 ? approve : "Reject"}
          </span>
        </div>
      )}
      <div
        {...swipe.handlers}
        className={cn(
          "relative touch-pan-y bg-bay py-4 lg:grid lg:grid-cols-[18rem_minmax(0,1fr)_10rem] lg:gap-6 lg:rounded-xl lg:p-4",
          selected && "lg:bg-panel lg:shadow-[inset_0_0_0_2px_var(--color-mark)]",
        )}
        style={{ transform: `translateX(${swipe.dx}px)`, transition: swipe.dx === 0 ? "transform 0.2s" : "none" }}
      >
        <button onClick={open} className="relative block aspect-video w-full overflow-hidden rounded-lg bg-panel" aria-label={`Edit ${clip.title}`}>
          <Thumb clip={clip} />
          <span className="absolute bottom-2 left-2 rounded-md bg-ink/85 px-1.5 py-0.5 font-mono text-xs">{duration}</span>
        </button>
        <div className="min-w-0">
          <button onClick={open} className="block w-full text-left">
            <div className="mt-3 flex flex-wrap items-center gap-2 lg:mt-0.5">
              <span
                className={cn(
                  "rounded-full px-2.5 py-0.5 text-[13px] font-semibold",
                  clip.kind === "highlight" ? "bg-mark/15 text-mark" : "bg-panel-2 text-chalk",
                )}
              >
                {KIND_LABEL[clip.kind]}
              </span>
              {clip.make_short && (
                <span className="inline-flex items-center gap-1 rounded-full bg-panel-2 px-2.5 py-0.5 text-[13px] font-semibold">
                  <PhoneIcon className="size-3.5" /> Short
                </span>
              )}
              <span className="ml-auto">
                {compilation ? (
                  <span className="text-[13px] text-graphite">{clip.compilation_clips} clips</span>
                ) : (
                  <Score clip={clip} />
                )}
              </span>
            </div>
            <h3 className="mt-2 text-lg font-semibold leading-snug font-stretch-semi-condensed">{clip.title}</h3>
            {why && <p className="mt-1.5 line-clamp-3 max-w-[65ch] text-[15px] leading-snug text-graphite">{why}</p>}
          </button>
          {strip ? (
            <div className="mt-3 max-w-xl">{strip}</div>
          ) : (
            clip.kind !== "whole" && (
              <div className="mt-3 max-w-xl">
                <Slate clip={clip} />
              </div>
            )
          )}
        </div>
        <div className="mt-4 grid grid-cols-2 gap-2 lg:mt-0 lg:grid-cols-1 lg:content-start">
          <button
            onClick={onApprove}
            className="order-2 flex items-center justify-center gap-2 rounded-xl bg-cue py-3 font-semibold text-ink active:bg-emerald-700 lg:order-1 lg:py-2.5 lg:text-sm"
          >
            <CheckIcon className="size-4" /> {approve}
          </button>
          {onKeepAndPost && (
            <button
              onClick={onKeepAndPost}
              className="order-4 col-span-2 flex items-center justify-center gap-2 rounded-xl py-1.5 text-sm font-semibold text-graphite hover:text-chalk lg:order-2 lg:col-span-1 lg:justify-start lg:px-3"
            >
              <UploadIcon className="size-4" />
              <span className="lg:hidden">Keep and post it by itself too</span>
              <span className="hidden lg:inline">Keep and post</span>
            </button>
          )}
          <button
            onClick={open}
            className="hidden items-center justify-center gap-2 rounded-xl bg-panel-2 py-2.5 text-sm font-semibold hover:bg-zinc-700 lg:order-3 lg:flex"
          >
            <ScissorsIcon className="size-4" /> Edit
          </button>
          <button
            onClick={onReject}
            className="order-1 flex items-center justify-center gap-2 rounded-xl bg-tally/15 py-3 font-semibold text-red-300 active:bg-tally/25 lg:order-4 lg:py-2.5 lg:text-sm"
          >
            <XIcon className="size-4" /> Reject
          </button>
        </div>
      </div>
    </article>
  );
});

/** The line above a clip's title: what kind of clip, whether it makes a Short, and its score or who made it. */
function ClipMeta({ clip }: { clip: Clip }) {
  return (
    <div className="flex items-center gap-2.5 text-xs text-graphite">
      <span className="font-medium text-chalk">{KIND_LABEL[clip.kind]}</span>
      {clip.make_short && (
        <span className="inline-flex items-center gap-0.5">
          <PhoneIcon className="size-3" /> Short
        </span>
      )}
      {clip.manual ? (
        <span className="ml-auto">Made by you</span>
      ) : (
        isPick(clip) && <span className="ml-auto">AI score <span className="font-mono">{clip.score.toFixed(2)}</span></span>
      )}
    </div>
  );
}

/** Horizontal swipe on touch screens. Vertical drags still scroll the page (touch-action: pan-y). */
function useSwipe(onRight?: () => void, onLeft?: () => void) {
  const [dx, setDx] = useState(0);
  const start = useRef<{ x: number; y: number } | null>(null);
  const axis = useRef<"x" | "y" | null>(null);
  const swiped = useRef(false);

  if (!onRight || !onLeft) return { dx: 0, handlers: {} };

  const reset = () => {
    start.current = null;
    axis.current = null;
    setDx(0);
  };

  return {
    dx,
    handlers: {
      onPointerDown: (e: PointerEvent) => {
        if (e.pointerType === "mouse") return;
        start.current = { x: e.clientX, y: e.clientY };
        axis.current = null;
        swiped.current = false;
      },
      onPointerMove: (e: PointerEvent) => {
        if (!start.current) return;
        const moveX = e.clientX - start.current.x;
        const moveY = e.clientY - start.current.y;
        if (!axis.current && (Math.abs(moveX) > 8 || Math.abs(moveY) > 8)) {
          axis.current = Math.abs(moveX) > Math.abs(moveY) ? "x" : "y";
        }
        if (axis.current === "x") {
          swiped.current = true;
          setDx(moveX);
        }
      },
      onPointerUp: () => {
        if (axis.current === "x") {
          if (dx > SWIPE_DISTANCE) onRight();
          else if (dx < -SWIPE_DISTANCE) onLeft();
        }
        reset();
      },
      onPointerCancel: reset,
      // A swipe shouldn't also count as a tap that opens the clip.
      onClickCapture: (e: { stopPropagation: () => void; preventDefault: () => void }) => {
        if (swiped.current) {
          e.stopPropagation();
          e.preventDefault();
          swiped.current = false;
        }
      },
    },
  };
}
