import { useState, type FormEvent } from "react";
import { api } from "../api";
import { Button } from "../ui";

export function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(password);
      onDone();
    } catch {
      setError("That password didn't work.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid min-h-dvh place-items-center px-6">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 text-center">
        <img src="/icon-192.png" alt="" className="mx-auto size-16 rounded-2xl" />
        <h1 className="text-2xl font-bold tracking-tight">Stream Clipper</h1>
        <input
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          autoFocus
          className="w-full rounded-xl bg-zinc-900 px-4 py-3 text-base outline-none ring-1 ring-white/10 focus:ring-2 focus:ring-violet-500"
        />
        {error && <p className="text-sm text-red-300">{error}</p>}
        <Button type="submit" tone="primary" disabled={!password || busy} className="w-full">
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </div>
  );
}
