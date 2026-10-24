import { useRef, useState, type PointerEvent, type ReactNode } from "react";
import { media, type Clip } from "./api";
import { CheckIcon, PhoneIcon, XIcon } from "./icons";
import { clock, cn, go, length } from "./lib";
import { ClipStatusChip, KIND_LABEL } from "./ui";

const SWIPE_DISTANCE = 96; // px to approve (right) or reject (left)

/** A clip in a list: tap to open; in the inbox, swipe or use the buttons to approve or reject. */
export function ClipCard(props: {
  clip: Clip;
  onApprove?: () => void;
  onReject?: () => void;
  showStatus?: boolean;
  footer?: ReactNode;
}) {
  const { clip } = props;
  const swipe = useSwipe(props.onApprove, props.onReject);

  return (
    <div className="relative overflow-hidden rounded-2xl bg-zinc-900 ring-1 ring-white/5">
      {swipe.dx !== 0 && (
        <div
          className={cn(
            "absolute inset-0 flex items-center px-6 text-sm font-semibold",
            swipe.dx > 0 ? "justify-start bg-emerald-600/25 text-emerald-300" : "justify-end bg-red-600/25 text-red-300",
          )}
        >
          <span className="flex items-center gap-2" style={{ opacity: Math.min(1, Math.abs(swipe.dx) / SWIPE_DISTANCE) }}>
            {swipe.dx > 0 ? <CheckIcon /> : <XIcon />}
            {swipe.dx > 0 ? "Approve" : "Reject"}
          </span>
        </div>
      )}
      <div
        {...swipe.handlers}
        className="relative touch-pan-y bg-zinc-900"
        style={{ transform: `translateX(${swipe.dx}px)`, transition: swipe.dx === 0 ? "transform 0.2s" : "none" }}
      >
        <button onClick={() => go(`clip/${clip.id}`)} className="flex w-full gap-3 p-3 text-left active:bg-white/[0.03]">
          <div className="relative aspect-video w-36 shrink-0 overflow-hidden rounded-lg bg-zinc-800">
            <img src={media.thumb(clip)} alt="" loading="lazy" className="size-full object-cover" />
            <span className="absolute bottom-1 right-1 rounded bg-black/75 px-1.5 py-0.5 text-[11px] font-semibold tabular-nums">
              {length(clip.end - clip.start)}
            </span>
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 text-[11px] font-medium text-zinc-400">
              <span>{KIND_LABEL[clip.kind]}</span>
              {clip.make_short && (
                <span className="inline-flex items-center gap-0.5 text-zinc-400">
                  · <PhoneIcon className="size-3" /> Short
                </span>
              )}
              {clip.kind !== "whole" && (
                <span className="ml-auto tabular-nums text-zinc-500">score {clip.score.toFixed(2)}</span>
              )}
            </div>
            <h3 className="mt-1 line-clamp-2 text-[15px] font-semibold leading-snug">{clip.title}</h3>
            <p className="mt-1 truncate text-xs text-zinc-500">
              {clip.kind === "whole" ? clip.vod_title : `${clock(clip.start)} · ${clip.vod_title}`}
            </p>
            {props.showStatus && (
              <div className="mt-2">
                <ClipStatusChip status={clip.status} />
              </div>
            )}
          </div>
        </button>
        {props.onApprove && props.onReject && (
          <div className="flex border-t border-white/5">
            <button
              onClick={props.onReject}
              className="flex flex-1 items-center justify-center gap-1.5 py-2.5 text-sm font-medium text-zinc-400 active:bg-white/5"
            >
              <XIcon className="size-4" /> Reject
            </button>
            <div className="w-px bg-white/5" />
            <button
              onClick={props.onApprove}
              className="flex flex-1 items-center justify-center gap-1.5 py-2.5 text-sm font-semibold text-emerald-300 active:bg-white/5"
            >
              <CheckIcon className="size-4" /> Approve
            </button>
          </div>
        )}
        {props.footer}
      </div>
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
