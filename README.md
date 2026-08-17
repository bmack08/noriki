# Noriki

A phone console for four ventures and the machine that works on them.

Every task splits into two lanes. **Yours** is what only you can clear — taste,
judgement, authority, or a body doing pushups. **Running** is what a machine can
finish without you. The app's whole job is to keep the first lane short.

There is no server. Git is both the database and the message bus.

```
  PHONE (PWA on GitHub Pages)
        │  GitHub API
        ▼
  ┌──────────────────────┐
  │  private state repo  │   state.json · inbox/ · outbox/
  └──────────────────────┘
        ▲
        │  git pull / push
  RELAY (this PC) ──spawns──▶ claude -p, in the project's own folder
```

Your PC only ever makes **outbound** connections. No tunnel, no inbound port, no
auth layer of ours facing the internet — and OVERSEER stays localhost-only, exactly
as its frozen decision requires.

## Layout

| Path | What it is |
|---|---|
| `static/` | PWA source — edit here |
| `docs/` | Built output; this is what GitHub Pages serves. Never hand-edit. |
| `relay/` | The phone-to-PC bridge |
| `build_single.py` | `static/` → `docs/` + a one-file `dist/Noriki.html` |

## Setup

### 1. Two repos

- **`noriki`** (public) — this repo. Holds code only, no data. Public so Pages is free.
- **`noriki-state`** (**private**) — holds `state.json`, `inbox/`, `outbox/`. Never public.

In `noriki` → Settings → Pages → **Deploy from a branch** → `main` → **`/docs`**.

The folder must be `docs/`. Branch-based Pages only ever offers the repo root
or `/docs` — an arbitrary folder like `/deploy` is not selectable, which is why
the build output lives where it does.

### 2. A token for the phone

GitHub → Settings → Developer settings → **Fine-grained** tokens:

- Repository access: **only `noriki-state`**
- Permissions: **Contents → Read and write**
- Expiry: set one. 90 days is sensible.

Open the Pages URL on your phone, Add to Home Screen, paste the repo name and token.
The token stays in that device's local storage and is never committed.

### 3. The relay

```powershell
cd relay
copy config.example.json config.json     # edit the paths
git clone https://github.com/<you>/noriki-state ..\.state
.\run-relay.ps1                          # or -Install to start it at logon
```

## The ask-bridge — the part that actually unblocks things

OVERSEER already had the right idea. When an agent hits a decision it can't make,
it calls `ask_user`, which hits `POST /internal/ask`; OVERSEER blocks the session
and pushes option cards to its GUI. But read that endpoint's own docstring: it
blocks **"until the user answers or times out"**, capped at **590 seconds**.

So every question asked while you're away from your desk died after ten minutes,
and the work stalled until you came back. That timeout was your bandwidth
constraint, written as a number.

`relay/noriki_ask_bridge.py` doesn't change how long it waits. It changes where
you can answer:

| When | What happens |
|---|---|
| immediately | Question lands in your **Yours** lane · push notification |
| after ~3 min | Still unanswered → email, for when your phone isn't to hand |
| at 590s | OVERSEER stops waiting — but the question **stays in your queue** |
| whenever you answer | Inside the window it resumes live; after it, your answer is delivered as a follow-up prompt and the work picks up |

The timeout still does its real job — stopping a session hanging forever. The
*decision* just stops being perishable.

### Notifications

Both channels are optional and configured under `notify` in `relay/config.json`.

**Push — [ntfy](https://ntfy.sh).** Free, no account. Pick a long unguessable
topic name (anyone who knows it can read your notifications), put it in
`ntfy_topic`, install the ntfy app and subscribe to the same topic.

**Email.** Set `email_to`. Leave `smtp.host` empty and ntfy forwards it for you —
no SMTP setup at all. Fill in `smtp` only if you'd rather send it yourself.

Test both without waiting for a real question:

```powershell
python noriki_ask_bridge.py --config config.json --test-notify
```

## How a message travels

1. You type into a project's chat on your phone.
2. The app writes `inbox/<id>.json` to the private repo.
3. The relay pulls, sees it, and runs `claude -p` **with the working directory set to
   that project's folder** — so it reads and edits the real files.
4. The reply lands in `outbox/<id>.json`; the app pulls it into the thread.

Replies arrive in roughly 15–30 seconds plus however long the work takes. This is
texting, not live chat, and the work is the slow part anyway. If the PC is asleep the
message simply queues.

## Security posture

Being able to run Claude Code on this PC from a phone is a real capability. Three
things bound it:

1. **Path allowlist.** The relay will only run inside a directory named in
   `config.json`. A message pointing anywhere else is rejected and logged. Without
   this, a leaked token would mean arbitrary code execution on this machine.
2. **The gatekeeper still applies.** Frozen regions are enforced by the PreToolUse
   hook inside the spawned session, the same as interactively.
3. **`permission_mode: acceptEdits`.** File edits proceed; destructive shell commands
   still require approval, and an approval prompt in headless mode fails closed.

Keep the state repo private, scope the token to it alone, and give it an expiry.

## Write ownership

The two sides never touch the same files, so there is nothing to merge:

| Writer | Files |
|---|---|
| Phone | `state.json`, `inbox/*.json`, `answers/*.json` |
| Relay + bridge | `outbox/*.json`, `asks/*.json`, `relay-status.json` |

## Build

```powershell
python build_single.py
git add -A && git commit -m "build" && git push
```

Pages redeploys on push.
