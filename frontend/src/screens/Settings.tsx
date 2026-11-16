import { useState, type ReactNode } from "react";
import { api, errorText, type AiProviderId, type AiSetup, type AppSettings } from "../api";
import { cn, goBack, useDesktop, usePoll } from "../lib";
import { Button, DiskMeter, Screen, useToast } from "../ui";

/**
 * The app's own settings: downloads, disk space, the AI and notifications. A desktop has it in the sidebar; a phone,
 * under More.
 */
export function Settings() {
  const desktop = useDesktop();
  return (
    <Screen
      title="Settings"
      subtitle="Downloads, disk space, the AI and notifications."
      onBack={desktop ? undefined : () => goBack("more")}
    >
      <div className="space-y-8">
        <DownloadAndDisk />
        <AiChoice />
        <Notifications />
      </div>
    </Screen>
  );
}

/** A card of settings, one row each. */
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h2 className="mb-3 text-2xl font-semibold font-stretch-condensed">{title}</h2>
      <div className="divide-y divide-line rounded-2xl bg-panel px-4">{children}</div>
    </section>
  );
}

/** One setting: what it's for, what it's set to, and its editor while it's being changed. */
function Row(props: {
  name: string;
  value: ReactNode;
  hint?: string;
  problem?: string;
  action?: string; // the button's label, Change unless said
  onAction?: () => void;
  editing?: boolean;
  children?: ReactNode; // the editor
}) {
  return (
    <div className="py-3.5">
      <div className="flex items-center gap-4">
        <div className="min-w-0 flex-1">
          <span className="block font-semibold">{props.name}</span>
          {!props.editing && <div className="mt-0.5 text-sm text-chalk">{props.value}</div>}
          {props.hint && <p className="mt-0.5 text-[13px] text-graphite">{props.hint}</p>}
          {props.problem && !props.editing && <p className="mt-1 text-[13px] text-amber-300">{props.problem}</p>}
        </div>
        {props.onAction && !props.editing && (
          <button
            type="button"
            onClick={props.onAction}
            className="-mr-2 shrink-0 rounded-lg px-2 py-2 text-sm font-semibold text-mark hover:bg-white/5"
          >
            {props.action ?? "Change"}
          </button>
        )}
      </div>
      {props.editing && <div className="mt-3">{props.children}</div>}
    </div>
  );
}

const EIGHT_HOUR_STREAM_GB = 25; // a typical 8-hour 1080p VOD, for the time estimate

/** Download speed and disk space. */
function DownloadAndDisk() {
  const { data: settings, setData } = usePoll(api.settings, 0);
  const { data: storage } = usePoll(api.storage, 60000);
  if (!settings) return null;

  const limit = settings.download_limit_mbps;
  const minutes = limit > 0 ? Math.round((EIGHT_HOUR_STREAM_GB * 8000) / limit / 60) : 0;
  const days = settings.cleanup_after_days;

  return (
    <>
      <Section title="Downloads">
        <NumberRow
          name="Speed limit"
          value={limit > 0 ? `${limit} Mbit/s, so an 8-hour stream takes about ${minutes} minutes.` : "None: downloads use your full internet speed."}
          current={limit}
          hint="In Mbit/s. Around half your internet speed keeps everything else usable. 0 means no limit. A download that's running switches within a few seconds."
          onSave={async (v) => setData(await api.updateSettings({ download_limit_mbps: v }))}
        />
        <FullSpeedHours settings={settings} onChange={setData} />
      </Section>
      <Section title="Disk">
        {storage && <Row name="Data disk" value={<div className="pt-1"><DiskMeter storage={storage} /></div>} />}
        <NumberRow
          name="Finished streams' files"
          value={`Deleted ${days === 0 ? "as soon as" : `${days} day${days === 1 ? "" : "s"} after`} all their clips are uploaded or rejected.`}
          current={days}
          hint="In days. The transcript and the AI's picks are kept; the clips can't be re-cut after this. Each rendered clip is deleted as soon as it's uploaded."
          onSave={async (v) => setData(await api.updateSettings({ cleanup_after_days: v }))}
        />
      </Section>
    </>
  );
}

const hourLabel = (h: number) => `${h % 12 || 12} ${h % 24 < 12 ? "AM" : "PM"}`;

/** "EDT" for America/New_York, or the zone's name if the browser doesn't know a short one. */
function zoneLabel(timezone: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone: timezone, timeZoneName: "short" }).formatToParts(new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value ?? timezone;
  } catch {
    return timezone;
  }
}

/** Hours of the day when downloads ignore the speed limit, such as when overnight streams end and nobody's online. */
function FullSpeedHours({ settings, onChange }: { settings: AppSettings; onChange: (s: AppSettings) => void }) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [from, setFrom] = useState(5);
  const [until, setUntil] = useState(11);
  const on = settings.full_speed_from !== settings.full_speed_until;
  const zone = zoneLabel(settings.timezone);

  const save = async (body: { full_speed_from: number; full_speed_until: number }, done: string) => {
    try {
      onChange(await api.updateSettings(body));
      setEditing(false);
      toast({ text: done });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  const select = (value: number, set: (v: number) => void, label: string) => (
    <label className="flex-1">
      <span className="mb-1 block text-[13px] text-graphite">{label}</span>
      <select
        value={value}
        onChange={(e) => set(Number(e.target.value))}
        className="w-full appearance-none rounded-xl bg-bay px-3.5 py-3 text-base outline-none ring-1 ring-line focus:ring-2 focus:ring-mark"
      >
        {Array.from({ length: 24 }, (_, h) => (
          <option key={h} value={h}>
            {hourLabel(h)}
          </option>
        ))}
      </select>
    </label>
  );
  return (
    <Row
      name="Full speed hours"
      value={
        on
          ? `${hourLabel(settings.full_speed_from)} to ${hourLabel(settings.full_speed_until)} (${zone}), when downloads ignore the limit.`
          : "None."
      }
      action={on ? "Change" : "Set some"}
      onAction={() => {
        if (on) {
          setFrom(settings.full_speed_from);
          setUntil(settings.full_speed_until);
        }
        setEditing(true);
      }}
      editing={editing}
    >
      <div className="space-y-2">
        <div className="flex gap-2">
          {select(from, setFrom, `From (${zone})`)}
          {select(until, setUntil, "Until")}
        </div>
        <div className="flex gap-2">
          {on && <Button onClick={() => save({ full_speed_from: 0, full_speed_until: 0 }, "Full speed hours off")}>Turn off</Button>}
          <Button onClick={() => setEditing(false)}>Cancel</Button>
          <Button tone="primary" disabled={from === until} onClick={() => save({ full_speed_from: from, full_speed_until: until }, "Saved.")} className="flex-1">
            Save
          </Button>
        </div>
        <p className="text-[13px] text-graphite">
          Between these hours downloads ignore the limit and use your whole connection, such as right after overnight streams
          end. A download that's running speeds up or slows down within a few seconds when they start and end.
        </p>
      </div>
    </Row>
  );
}

const AI_HINT: Record<AiProviderId, string> = {
  claude: "On your Claude plan, signed in once on the server (README). The model is opus, sonnet or a full model name.",
  codex:
    "On your ChatGPT plan, signed in once on the server with codex login --device-auth (README). Leave the model " +
    "empty for Codex's own default.",
  openrouter:
    "Paid per use from your OpenRouter credit, with CLIPPER_OPENROUTER_API_KEY. Any model whose page lists " +
    "structured outputs works; anthropic/claude-opus-5.5 costs about $0.75 for an 8-hour stream.",
  cliproxy:
    "Through a CLIProxyAPI server signed in to your plans, at CLIPPER_CLIPROXY_URL with CLIPPER_CLIPROXY_API_KEY " +
    "(README). The model is any it lists, such as claude-opus-5-5.",
};

/** The AI: the main one, and each task (picking clips, breakdowns, titles, thumbnails), which can have its own. */
function AiChoice() {
  const toast = useToast();
  const { data: setup, setData } = usePoll(api.ai, 0);
  const [editing, setEditing] = useState<string | null>(null); // "main" or a task's id
  if (!setup) return null;

  const main = setup.providers.find((p) => p.id === setup.provider)!;
  const save = async (body: Parameters<typeof api.setAi>[0]) => {
    try {
      setData(await api.setAi(body));
      setEditing(null);
      toast({ text: "Saved. It takes over from the next piece of work." });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  return (
    <Section title="The AI">
      <Row
        name="Main AI"
        hint="Every task below uses it, unless the task has an AI of its own."
        value={`${main.name} (${main.model || "its default model"})`}
        problem={setup.problem}
        editing={editing === "main"}
        onAction={() => setEditing("main")}
      >
        <AiEditor
          setup={setup}
          provider={setup.provider}
          model={main.model}
          onCancel={() => setEditing(null)}
          onSave={(provider, model) => {
            const ofIt = setup.providers.find((p) => p.id === provider)!;
            // The default stays the default, rather than being kept as this model's name.
            save({ provider: provider as AiProviderId, model: model === ofIt.default_model ? "" : model });
          }}
        />
      </Row>
      {setup.tasks.map((task) => (
        <Row
          key={task.id}
          name={task.name}
          hint={task.hint}
          value={task.provider ? task.uses : `Main AI: ${task.uses}`}
          problem={task.provider ? task.problem : ""}
          editing={editing === task.id}
          onAction={() => setEditing(task.id)}
        >
          <AiEditor
            setup={setup}
            provider={task.provider}
            model={task.model}
            withMain
            onCancel={() => setEditing(null)}
            onSave={(provider, model) => save({ task: task.id, provider, model: provider ? model : "" })}
          />
        </Row>
      ))}
    </Section>
  );
}

const COLUMNS: Record<number, string> = { 3: "grid-cols-3", 4: "grid-cols-4", 5: "grid-cols-5" }; // one row of AIs

/** Pick an AI ("" for the main one, when `withMain`) and its model. */
function AiEditor(props: {
  setup: AiSetup;
  provider: AiProviderId | "";
  model: string;
  withMain?: boolean;
  onSave: (provider: AiProviderId | "", model: string) => void;
  onCancel: () => void;
}) {
  const [provider, setProvider] = useState<AiProviderId | "">(props.provider);
  const [model, setModel] = useState(props.model);
  const ofIt = provider ? props.setup.providers.find((p) => p.id === provider)! : null;
  const options: { id: AiProviderId | ""; name: string }[] = [
    ...(props.withMain ? [{ id: "" as const, name: "Main AI" }] : []),
    ...props.setup.providers.map((p) => ({ id: p.id, name: p.name })),
  ];
  const choose = (id: AiProviderId | "") => {
    setProvider(id);
    // A task's own model starts empty: that AI's model, as set for it. The main AI shows the model it has.
    setModel(props.withMain ? "" : (props.setup.providers.find((p) => p.id === id)?.model ?? ""));
  };
  return (
    <div className="space-y-2">
      <div className={cn("grid gap-1 rounded-xl bg-bay p-1", COLUMNS[options.length] ?? "grid-cols-3")}>
        {options.map((o) => (
          <button
            key={o.id || "main"}
            type="button"
            onClick={() => choose(o.id)}
            className={cn("rounded-lg px-1 py-2 text-xs font-semibold", provider === o.id ? "bg-zinc-700 text-chalk shadow-[inset_0_-2px_0_var(--color-mark)]" : "text-zinc-400")}
          >
            {o.name}
          </button>
        ))}
      </div>
      <div className="flex gap-2">
        {ofIt && (
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={(props.withMain ? ofIt.model : ofIt.default_model) || "Its default"}
            aria-label="Model"
            className="min-w-0 flex-1 rounded-xl bg-bay px-3.5 py-3 text-base outline-none ring-1 ring-line focus:ring-2 focus:ring-mark"
          />
        )}
        <Button onClick={props.onCancel} className={ofIt ? "" : "flex-1"}>
          Cancel
        </Button>
        <Button tone="primary" onClick={() => props.onSave(provider, model.trim())} className={ofIt ? "" : "flex-1"}>
          Save
        </Button>
      </div>
      {ofIt ? (
        <p className="text-[13px] text-graphite">
          {props.withMain ? `Leave the model empty for the one ${ofIt.name} uses (${ofIt.model || "its default"}). ` : ""}
          {AI_HINT[ofIt.id]}
        </p>
      ) : (
        <p className="text-[13px] text-graphite">The task follows the main AI, whatever it's set to.</p>
      )}
    </div>
  );
}

function Notifications() {
  const toast = useToast();
  const { data } = usePoll(api.notifications, 0);
  if (!data) return null;
  const where = [data.ntfy && "ntfy", data.discord && "Discord"].filter(Boolean).join(" and ");
  const test = async () => {
    try {
      await api.testNotification();
      toast({ text: `Sent a test to ${where}.` });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };
  return (
    <Section title="Notifications">
      {where ? (
        <Row
          name="Sent to"
          value={`${where}.`}
          problem={data.links ? "" : "Set CLIPPER_APP_URL so they open the app."}
          action="Send a test"
          onAction={test}
        />
      ) : (
        <Row name="Sent to" value="Nowhere: they're off." hint="Set them up for ntfy or Discord in the server's .env (README)." />
      )}
    </Section>
  );
}

/** A number's row, which becomes a box to type the new one in. */
function NumberRow(props: {
  name: string;
  value: string;
  current: number;
  hint: string; // while it's being changed
  onSave: (value: number) => Promise<void>;
}) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");

  const save = async () => {
    try {
      await props.onSave(Math.max(0, Number(value) || 0));
      setEditing(false);
      toast({ text: "Saved." });
    } catch (e) {
      toast({ text: errorText(e) });
    }
  };

  return (
    <Row
      name={props.name}
      value={props.value}
      onAction={() => {
        setValue(String(props.current));
        setEditing(true);
      }}
      editing={editing}
    >
      <div className="space-y-2">
        <div className="flex gap-2">
          <input
            type="number"
            inputMode="decimal"
            min={0}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-label={props.name}
            className="min-w-0 flex-1 rounded-xl bg-bay px-3.5 py-3 text-base outline-none ring-1 ring-line focus:ring-2 focus:ring-mark"
          />
          <Button onClick={() => setEditing(false)}>Cancel</Button>
          <Button tone="primary" onClick={save}>
            Save
          </Button>
        </div>
        <p className="text-[13px] text-graphite">{props.hint}</p>
      </div>
    </Row>
  );
}
