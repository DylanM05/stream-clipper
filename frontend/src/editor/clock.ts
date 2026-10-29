// The playhead's time, kept outside React's state. It changes every frame while playing, and as state it made the
// whole editor redraw every frame (the transcript, the timeline's lanes, the details), which stuttered on slower
// computers. Now the parts that show it subscribe, and each redraws only when what it shows changes.

import { useEffect, useState, useSyncExternalStore } from "react";

export interface Clock {
  get(): number;
  set(t: number): void;
  subscribe(listener: () => void): () => void;
}

export function createClock(start: number): Clock {
  let time = start;
  const listeners = new Set<() => void>();
  return {
    get: () => time,
    set: (t) => {
      if (t === time) return;
      time = t;
      for (const listener of listeners) listener();
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** The playhead's time, or something worked out from it: the component redraws only when that changes, so `pick`
 * should give a number, string or boolean (an object would be new every time). */
export function useClock<T extends number | string | boolean = number>(clock: Clock, pick?: (t: number) => T): T {
  return useSyncExternalStore(clock.subscribe, () => (pick ? pick(clock.get()) : clock.get()) as T);
}

/** `value` once it has stayed the same for `ms`: for work that costs too much to redo on every step of a drag or a
 * zoom, like asking for stills or rebuilding a list. */
export function useSettled<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

/** The index of the last of `items`, sorted by start, starting at or before `t`; -1 if none does. */
export function indexAt(items: readonly { start: number }[], t: number): number {
  let lo = 0;
  let hi = items.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (items[mid].start <= t) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

/** The start of the one of `items` that `t` is inside, or -1: what to highlight as being played. */
export function startAt(items: readonly { start: number; end: number }[], t: number): number {
  const i = indexAt(items, t);
  return i >= 0 && t < items[i].end ? items[i].start : -1;
}
