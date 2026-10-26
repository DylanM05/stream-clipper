import { useCallback, useEffect, useRef, useState } from "react";

// --- Routing: #/inbox, #/clip/12, #/videos, #/videos/3, #/approved ---

export function useRoute(): string[] {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const onChange = () => setHash(location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash.replace(/^#\/?/, "").split("/").filter(Boolean);
}

export function go(path: string) {
  location.hash = `#/${path}`;
}

export function goBack(fallback: string) {
  if (history.length > 1) history.back();
  else go(fallback);
}

// --- Data loading that refreshes on an interval while the page is visible ---

export function usePoll<T>(load: () => Promise<T>, intervalMs: number, key: unknown = null) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const loadRef = useRef(load);
  loadRef.current = load;

  const refresh = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(undefined);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    if (!intervalMs) return;
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, intervalMs);
    return () => clearInterval(timer);
  }, [refresh, intervalMs, key]);

  return { data, error, refresh, setData };
}

// --- Formatting ---

const pad = (n: number) => String(n).padStart(2, "0");

/** Stream time: "1:02:03", "2:03", or with tenths "1:02:03.4". */
export function clock(seconds: number, tenths = false): string {
  const s = Math.max(0, seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = tenths ? (Math.floor((s % 60) * 10) / 10).toFixed(1).padStart(4, "0") : pad(Math.floor(s % 60));
  return h ? `${h}:${pad(m)}:${sec}` : `${m}:${sec}`;
}

/** A length: "0:45", "12:30" or "1h 05m". */
export function length(seconds: number): string {
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h ${pad(Math.floor((seconds % 3600) / 60))}m`;
  return `${Math.floor(seconds / 60)}:${pad(Math.floor(seconds % 60))}`;
}

export function ago(iso: string): string {
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function timeOfDay(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** A time coming up: "3:40 PM", "tomorrow 9:00 AM" or "Tue 3:40 PM". */
export function upcoming(iso: string): string {
  const day = new Date(iso);
  const today = new Date();
  const days = Math.round((new Date(day).setHours(0, 0, 0, 0) - new Date(today).setHours(0, 0, 0, 0)) / 86400000);
  const prefix = days <= 0 ? "" : days === 1 ? "tomorrow " : `${day.toLocaleDateString([], { weekday: "short" })} `;
  return prefix + timeOfDay(iso);
}

/** 1234 → "1.2K" */
export function compact(n: number): string {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

export function platformName(extractor: string): string {
  const key = extractor.toLowerCase();
  if (key.startsWith("twitch")) return "Twitch";
  if (key.startsWith("youtube")) return "YouTube";
  if (key.startsWith("twitter")) return "X";
  if (key.startsWith("kick")) return "Kick";
  if (key === "local") return "File";
  return extractor;
}

/** "Oct 23, 2026", the format used in video titles. */
export function titleDate(day = new Date()): string {
  return day.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

/** Fill in a show's title pattern, the way the server does (backend/clipper/titles.py). */
export function applyTitlePattern(pattern: string, title: string, date: string): string {
  return (pattern || "{title}").replaceAll("{date}", date).replaceAll("{title}", title).split(/\s+/).filter(Boolean).join(" ");
}

/** Per-device preferences, such as the preview volume. Storage can be unavailable (private browsing). */
export function loadSetting<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function saveSetting(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // not saved; it's only a convenience
  }
}

/** Copy to the clipboard, falling back to the old method where the clipboard API isn't allowed (plain http). */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

/** A file name from a title, the way the server names downloads (YouTube Studio pre-fills the title from it). */
export function fileName(title: string): string {
  const name = title.replace(/[\\/:*?"<>|\u0000-\u001f]+/g, " ").split(/\s+/).filter(Boolean).join(" ");
  return name.replace(/^[\s.]+|[\s.]+$/g, "") || "clip";
}

export function formatBytes(bytes: number): string {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1e6))} MB`;
}

export const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export const cn = (...classes: (string | false | null | undefined)[]) => classes.filter(Boolean).join(" ");
