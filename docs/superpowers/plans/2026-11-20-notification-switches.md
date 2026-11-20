# Notification Switches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Six on/off switches in Settings → Notifications, one per kind of notification, so the user can quiet the Discord channel.

**Architecture:** Every `Note` carries a hard-coded `kind`. `notify.send` looks up that kind's switch (an app setting, `notify_<kind>`, `"0"` = off) and skips it when off. The web API reads and changes the switches; the Settings screen shows them as rows.

**Tech Stack:** Python (FastAPI, SQLModel, pytest), React + TypeScript.

**Spec:** `docs/superpowers/specs/2026-11-20-notification-switches-design.md`

## Global Constraints

- All kinds are on by default: a missing setting means on.
- "Send a test" always sends (it calls `notify.deliver`, which never checks switches).
- With the digest off, the Monday summary is skipped and `stats_digest_week` is not set.
- Run backend tests from `backend/` with `CLIPPER_MIN_FREE_GB=1 .venv/bin/python -m pytest`. Known machine-caused failures: 2 in test_cleanup.py and 5 in test_editor.py (ffmpeg has no `subtitles` filter). Compare against a baseline before calling anything a regression.

## Review Focus

- A new `Note(...)` added later without a kind: the source scan in Task 1 fails, so it can't slip in unswitched.
- Notifications not set up at all (`Settings()` with no ntfy/Discord): `send` returns before touching the database, so tests and installs without a data dir don't create a stray DB.
- PATCH with an unknown kind: rejected with 422 (pydantic `extra="forbid"`), not silently stored.
- Turning the digest off then on mid-Monday: it still sends that Monday, because the week was never marked.
- Settings screen with nothing set up: the switches stay hidden, since they'd control nothing.

---

### Task 1: Kinds on every notification, and switches that stop sending

**Files:**
- Modify: `backend/clipper/notify.py`
- Modify: `backend/clipper/worker.py` (the 12 `Note(` calls; `send_digest` at ~1219)
- Modify: `backend/clipper/monitor.py:382,437`
- Test: `backend/tests/test_notify.py`

**Interfaces:**
- Produces:
  - `notify.KINDS: dict[str, str]`, in this order: `{"review": "Ready to review", "new": "New on a watched channel", "uploaded": "Uploaded", "failure": "Failures", "heads_up": "Heads-ups", "digest": "Weekly YouTube summary"}`
  - `Note.kind: str = ""`: a field after `tone`. `""` is used only by the test button.
  - `notify.switch_key(kind: str) -> str`: returns `f"notify_{kind}"`
  - `notify.enabled(settings: Settings, kind: str) -> bool`: reads `get_app_setting(s, switch_key(kind))` through `get_engine(settings)`. Returns `False` only when the stored value is `"0"`.
  - `notify.send` becomes: return if not `configured`; return if `note.kind` is set and not `enabled(...)`; otherwise start the thread as before. `deliver` is unchanged.

- [ ] **Step 1: Write the failing tests** in `test_notify.py`

```python
def test_every_notification_has_its_kind():
    """Each Note( in the app names its kind, so a new one can't go out without a switch."""
    import ast, pathlib
    found = {}
    for path in pathlib.Path(notify.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Note":
                kind = next((k.value.value for k in node.keywords if k.arg == "kind"), None)
                title = node.args[0]
                start = title.values[0].value if isinstance(title, ast.JoinedStr) else title.value
                found[start.split(":")[0].split(" {")[0].strip()] = kind
    assert found == {
        "Waiting for disk space": "failure", "Ready": "review", "Waiting for the AI": "heads_up",
        "Failed": "failure", "Render failed": "failure", "No intro on": "heads_up", "Not uploaded": "failure",
        "Only one clip kept": "heads_up", "Compilation to review": "review", "Uploaded": "uploaded",
        "Upload failed": "failure", "Last week on YouTube": "digest", "Can't check": "failure", "New for": "new",
    }


def test_a_kind_switched_off_isnt_sent(tmp_path, posts, monkeypatch):
    monkeypatch.setattr(notify.threading, "Thread", lambda target, args, **_: type("T", (), {"start": lambda self: target(*args)})())
    settings = Settings(data_dir=tmp_path, discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    with Session(get_engine(settings)) as s:
        set_app_setting(s, "notify_new", "0")
        s.commit()
    notify.send(settings, Note("New for Kino", kind="new"))
    notify.send(settings, Note("Ready: Kino", kind="review"))
    assert [p["json"]["embeds"][0]["title"] for p in posts] == ["Ready: Kino"]
    assert notify.enabled(settings, "review") and not notify.enabled(settings, "new")
```

Add these imports: `from sqlmodel import Session` and `from clipper.db import get_engine, set_app_setting`. The scan only reads `clipper/*.py`, so the test button's `notify.Note(...)` in `web/app.py` is outside it. Extract the title-prefix logic into a small helper if the scan reads awkwardly. Its job is to map each call site to its kind.

- [ ] **Step 2: Run them to make sure they fail**

Run: `CLIPPER_MIN_FREE_GB=1 .venv/bin/python -m pytest tests/test_notify.py -v`
Expected: both new tests FAIL (kinds are `None`; `Note` has no `kind`).

- [ ] **Step 3: Implement the Interfaces above in `notify.py`.** Add `kind=` to each call site, using the mapping in the test. In `worker.send_digest`, add `or not notify.enabled(self.settings, "digest")` to the early-return condition, after `configured`.

- [ ] **Step 4: Add a digest test** next to the existing digest tests (`grep -rn send_digest backend/tests`). With `notify_digest` set to `"0"` on a Monday after `DIGEST_HOUR`, `send_digest` sends nothing and `get_app_setting(s, "stats_digest_week")` stays `None`. Copy the existing digest test's setup.

- [ ] **Step 5: Run the whole backend suite**

Run: `CLIPPER_MIN_FREE_GB=1 .venv/bin/python -m pytest -q`
Expected: new tests PASS. The only failures are the 7 known machine-caused ones.

- [ ] **Step 6: Commit**: `git commit -am "Each notification has a kind, and a kind can be switched off"`

### Task 2: API for the switches

**Files:**
- Modify: `backend/clipper/web/app.py` (`notification_setup` ~1246; models near `PausesPatch` ~495)
- Test: `backend/tests/test_notify.py::test_the_apps_test_button`, plus a new test

**Interfaces:**
- Consumes: `notify.KINDS`, `notify.switch_key`, `get_app_setting`/`set_app_setting`
- Produces:
  - `GET /api/notifications` → `{"ntfy": bool, "discord": bool, "links": bool, "kinds": [{"id": str, "name": str, "on": bool}, ...]}`, in `KINDS` order
  - `PATCH /api/notifications`: body `NotificationsPatch`, one optional `bool` per kind, `model_config = ConfigDict(extra="forbid")`. Stores `"1"`/`"0"` and returns the same shape as GET.

- [ ] **Step 1: Write the failing test**, and update the existing `== {...}` assert in `test_the_apps_test_button` to include `kinds`

```python
def test_the_switches_save_and_load(tmp_path):
    client = TestClient(web_app.create_app(Settings(data_dir=tmp_path, web_dir=tmp_path / "web")))
    kinds = client.get("/api/notifications").json()["kinds"]
    assert [k["id"] for k in kinds] == list(notify.KINDS) and all(k["on"] for k in kinds)
    after = client.patch("/api/notifications", json={"new": False, "digest": False}).json()["kinds"]
    assert {k["id"] for k in after if not k["on"]} == {"new", "digest"}
    assert client.patch("/api/notifications", json={"new": True}).json()["kinds"][1]["on"] is True
    assert client.patch("/api/notifications", json={"spam": False}).status_code == 422
```

- [ ] **Step 2: Run it**: `... pytest tests/test_notify.py -v`. Expected: FAIL (no `kinds`, PATCH gives 405).
- [ ] **Step 3: Implement.** `notification_setup` takes `s: Session = Depends(db)`, like `get_pauses`.
- [ ] **Step 4: Run** `... pytest tests/test_notify.py tests/test_api.py -q`. Expected: PASS.
- [ ] **Step 5: Commit**: `git commit -am "Notification switches can be read and changed through the API"`

### Task 3: The switches in Settings

**Files:**
- Modify: `frontend/src/api.ts:519`
- Modify: `frontend/src/screens/Settings.tsx` (`Notifications`, ~343)

**Interfaces:**
- Consumes: Task 2's endpoints.
- Produces:
  - `export type NotificationSetup = { ntfy: boolean; discord: boolean; links: boolean; kinds: { id: string; name: string; on: boolean }[] }`
  - `api.notifications` returns it
  - `api.setNotifications(body: Record<string, boolean>)` → `patch<NotificationSetup>("/api/notifications", body)`

- [ ] **Step 1: Implement.** Use `usePoll`'s `setData` to show the PATCH result. When `where` is set, add one `Row` per kind under "Sent to": `name={k.name}`, `value={k.on ? "On" : "Off"}`, `action={k.on ? "Turn off" : "Turn on"}`, and `onAction` calls `setNotifications({[k.id]: !k.on})`. Errors go through `toast({ text: errorText(e) })`, as `test` does. When nothing is set up, show no switches.
- [ ] **Step 2: Type-check and build**: `cd frontend && npm run build`. Expected: no errors.
- [ ] **Step 3: Check it in a browser** with the `local-testing` skill: Settings at phone and desktop widths, with `CLIPPER_DISCORD_WEBHOOK_URL` set to a dummy value. Turn one kind off, reload, and check that it stays off.
- [ ] **Step 4: Commit**: `git commit -am "Settings has a switch for each kind of notification"`

### Task 4: Docs

- [ ] **Step 1:** Add a sentence to README's Notifications section (~line 170): each kind can be switched off in Settings → Notifications. Commit: `git commit -am "README: notification switches"`
