import { useEffect, useState, type ComponentType } from "react";
import { api } from "./api";
import { ChartIcon, FilmIcon, InboxIcon, LibraryIcon, TvIcon, UploadIcon } from "./icons";
import { cn, useDesktop, usePoll, useRoute } from "./lib";
import { Approved } from "./screens/Approved";
import { ClipEditor, NewClip } from "./screens/ClipEditor";
import { Inbox } from "./screens/Inbox";
import { AddToLibrary, BacklogPage, Library } from "./screens/Library";
import { Login } from "./screens/Login";
import { Overview } from "./screens/Overview";
import { Settings } from "./screens/Settings";
import { ShowEditor, Shows, UploadChannels } from "./screens/Shows";
import { Stats } from "./screens/Stats";
import { UploadKit } from "./screens/UploadKit";
import { VideoDetail, Videos } from "./screens/Videos";
import { Sidebar } from "./Sidebar";
import { ToastProvider } from "./ui";

export default function App() {
  const [auth, setAuth] = useState<"checking" | "in" | "out">("checking");

  useEffect(() => {
    api.session().then(
      (s) => setAuth(s.authenticated ? "in" : "out"),
      () => setAuth("out"),
    );
    const signedOut = () => setAuth("out");
    window.addEventListener("clipper:signed-out", signedOut);
    return () => window.removeEventListener("clipper:signed-out", signedOut);
  }, []);

  if (auth === "checking") return null;
  if (auth === "out") return <Login onDone={() => setAuth("in")} />;
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  );
}

/** A phone gets the tab bar along the bottom; a desktop, from 1024px wide, gets the sidebar down the left instead. */
function Shell() {
  const route = useRoute();
  const [screen = "inbox", param] = route;
  const desktop = useDesktop();
  const { data: summary, refresh } = usePoll(api.summary, 10000);
  const toReview = summary?.to_review ?? 0;
  const toUpload = summary?.to_upload ?? 0;

  useEffect(() => {
    const onChange = () => void refresh();
    window.addEventListener("clipper:changed", onChange);
    return () => window.removeEventListener("clipper:changed", onChange);
  }, [refresh]);

  useEffect(() => {
    document.title = toReview ? `(${toReview}) Stream Clipper` : "Stream Clipper";
  }, [toReview]);

  // The clip editor has the whole screen, on a desktop too: it has a back button, and room for the video.
  if (screen === "clip" && param) return <ClipEditor key={param} id={Number(param)} />;
  // A new clip from a stream (#/new/12), optionally of a part of it (#/new/12/600.5/660).
  if (screen === "new" && param) {
    const [from, to] = route.slice(2, 4).map(Number);
    return <NewClip key={route.join("/")} vodId={Number(param)} from={from || undefined} to={to || undefined} />;
  }

  let page;
  if (screen === "videos") page = param ? <VideoDetail key={param} id={Number(param)} /> : <Videos />;
  else if (screen === "library")
    page = param === "new" ? <AddToLibrary /> : param ? <BacklogPage key={param} id={Number(param)} /> : <Library />;
  else if (screen === "approved") page = <Approved />;
  else if (screen === "stats") page = <Stats />;
  else if (screen === "overview") page = <Overview summary={summary} />;
  else if (screen === "settings") page = <Settings />;
  else if (screen === "upload" && param) page = <UploadKit key={param} id={Number(param)} />;
  else if (screen === "shows" && param === "youtube") page = <UploadChannels />;
  else if (screen === "shows")
    page = param ? <ShowEditor key={param} id={param === "new" ? null : Number(param)} /> : <Shows />;
  else page = <Inbox />;

  return (
    <>
      {desktop && <Sidebar screen={screen} summary={summary} />}
      <main className="lg:pl-60">{page}</main>
      <nav className="fixed inset-x-0 bottom-0 z-30 border-t border-white/5 bg-zinc-950/90 pb-[env(safe-area-inset-bottom)] backdrop-blur lg:hidden">
        <div className="mx-auto grid max-w-2xl grid-cols-6">
          <Tab to="inbox" label="Inbox" icon={InboxIcon} active={screen === "inbox"} badge={toReview} />
          <Tab to="videos" label="Videos" icon={FilmIcon} active={screen === "videos"} />
          <Tab to="library" label="Library" icon={LibraryIcon} active={screen === "library"} />
          <Tab to="approved" label="Upload" icon={UploadIcon} active={screen === "approved" || screen === "upload"} badge={toUpload} />
          <Tab to="stats" label="Stats" icon={ChartIcon} active={screen === "stats"} />
          <Tab to="shows" label="Shows" icon={TvIcon} active={screen === "shows"} />
        </div>
      </nav>
    </>
  );
}

function Tab(props: { to: string; label: string; icon: ComponentType<{ className?: string }>; active: boolean; badge?: number }) {
  const Icon = props.icon;
  return (
    <a
      href={`#/${props.to}`}
      className={cn(
        "relative flex flex-col items-center gap-1 py-2.5 text-[11px] font-medium",
        props.active ? "text-violet-300" : "text-zinc-500",
      )}
    >
      <Icon className="size-6" />
      {props.label}
      {!!props.badge && (
        <span className="absolute left-1/2 top-1 ml-2 min-w-5 rounded-full bg-violet-500 px-1.5 text-center text-[11px] font-bold leading-5 text-white">
          {props.badge}
        </span>
      )}
    </a>
  );
}
