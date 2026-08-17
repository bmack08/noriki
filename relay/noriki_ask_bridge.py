"""Noriki ask-bridge — puts OVERSEER's human-in-the-loop checkpoint in your pocket.

OVERSEER already has the right mechanism. When an agent needs a decision it calls
its `ask_user` MCP tool, which hits `POST /internal/ask`; OVERSEER then blocks the
session and pushes clickable option cards to its GUI. The catch is in that
endpoint's own docstring: it BLOCKS until answered "or times out", capped at 590
seconds so the MCP transport gets a clean dismissal rather than an error.

Which means: every question asked while you are away from your desk dies after
about ten minutes, and the work stalls until you come back.

This bridge changes where the question can be answered, not how long it waits:

    OVERSEER /events  ->  ask lands in your phone's manual lane
                      ->  push notification immediately
                      ->  email if still unanswered after a few minutes
                      ->  at 590s OVERSEER gives up on the LIVE answer, but the
                          question stays in your queue and a late answer is
                          delivered to the session as a follow-up prompt

So the timeout keeps doing its job — it stops a session hanging forever — while
the decision itself stops being perishable.

    python noriki_ask_bridge.py --config config.json

Runs alongside noriki_relay.py. Both are started together by run-relay.ps1.
"""

from __future__ import annotations

import argparse
import json
import logging
import smtplib
import subprocess
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

LOG = logging.getLogger("noriki.ask")

POLL_SECONDS = 3
LIVE_WINDOW = 590          # matches OVERSEER's own cap in /internal/ask
HTTP_TIMEOUT = 15


def utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# OVERSEER
# --------------------------------------------------------------------------

class Overseer:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _req(self, path: str, data: dict | None = None, method: str | None = None):
        url = f"{self.base}{path}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            url, data=body, method=method or ("POST" if body else "GET"),
            headers={"Content-Type": "application/json"} if body else {},
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else None

    def alive(self) -> bool:
        try:
            self._req("/healthz")
            return True
        except Exception:
            return False

    def asks_since(self, last_id: int) -> list[dict]:
        """New ask_user_question events, oldest first."""
        try:
            rows = self._req("/events?kind=ask_user_question&limit=50") or []
        except Exception as exc:
            LOG.debug("events poll failed: %s", exc)
            return []
        fresh = [r for r in rows if int(r.get("id", 0)) > last_id]
        return sorted(fresh, key=lambda r: int(r["id"]))

    def answer(self, session_id: str, question_id: str, answers: dict) -> str:
        """Returns 'live' | 'expired' | 'error'."""
        try:
            self._req(f"/sessions/{session_id}/answer",
                      {"question_id": question_id, "answers": answers})
            return "live"
        except urllib.error.HTTPError as exc:
            # 409 = the future already resolved or timed out; the session moved on
            return "expired" if exc.code == 409 else "error"
        except Exception:
            return "error"

    def follow_up(self, session_id: str, text: str) -> bool:
        """Deliver a late answer as a new prompt so the work can still continue."""
        try:
            self._req(f"/sessions/{session_id}/prompt", {"prompt": text})
            return True
        except Exception as exc:
            LOG.warning("follow-up prompt failed for %s: %s", session_id, exc)
            return False


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

class Notifier:
    """Push first, email second. Both optional; missing config just disables one."""

    def __init__(self, cfg: dict):
        self.topic = (cfg.get("ntfy_topic") or "").strip()
        self.server = (cfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
        self.email_to = (cfg.get("email_to") or "").strip()
        self.email_after = int(cfg.get("email_after_seconds", 180))
        self.smtp = cfg.get("smtp") or {}
        self.app_url = (cfg.get("app_url") or "").strip()

    @property
    def push_enabled(self) -> bool:
        return bool(self.topic)

    @property
    def email_enabled(self) -> bool:
        return bool(self.email_to)

    def push(self, title: str, message: str, tags: str = "question") -> bool:
        """True only if it actually went. A notifier that lies is worse than none."""
        if not self.push_enabled:
            return False
        headers = {
            "Title": title.encode("utf-8"),
            "Priority": "high",
            "Tags": tags,
        }
        if self.app_url:
            headers["Click"] = self.app_url
        try:
            req = urllib.request.Request(
                f"{self.server}/{self.topic}",
                data=message.encode("utf-8"),
                headers=headers,
            )
            urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
            LOG.info("pushed: %s", title)
            return True
        except Exception as exc:
            LOG.warning("push failed: %s", exc)
            return False

    def email(self, subject: str, body: str) -> bool:
        """True only if SMTP accepted it.

        Note: ntfy.sh's public server does NOT relay email for anonymous
        publishers — every header variant returns 400 — so there is no
        zero-setup fallback. Email requires a real SMTP host.
        """
        if not self.email_enabled:
            return False

        host = (self.smtp.get("host") or "").strip()
        if not host:
            LOG.warning("email_to is set but smtp.host is empty — no email sent. "
                        "ntfy.sh will not relay mail for anonymous senders.")
            return False
        return self._email_smtp(host, subject, body)

    def _email_smtp(self, host: str, subject: str, body: str) -> bool:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.smtp.get("from") or self.smtp.get("user") or self.email_to
        msg["To"] = self.email_to
        msg.set_content(body)
        try:
            port = int(self.smtp.get("port", 587))
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                if self.smtp.get("user"):
                    s.login(self.smtp["user"], self.smtp.get("password", ""))
                s.send_message(msg)
            LOG.info("emailed: %s", subject)
            return True
        except smtplib.SMTPAuthenticationError:
            LOG.error("SMTP rejected the login. For Gmail this must be a 16-character "
                      "App Password (2-Step Verification on), not your normal password.")
            return False
        except Exception as exc:
            LOG.warning("smtp send failed: %s", exc)
            return False


# --------------------------------------------------------------------------
# git (the state repo is the transport, same as the relay)
# --------------------------------------------------------------------------

def git(repo: Path, *args: str, check: bool = True):
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=120)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()[:200]}")
    return proc


def pull(repo: Path) -> bool:
    try:
        git(repo, "fetch", "--quiet", "origin")
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        git(repo, "reset", "--hard", f"origin/{branch}", "--quiet")
        return True
    except Exception as exc:
        LOG.debug("pull failed: %s", exc)
        return False


def push_repo(repo: Path, message: str) -> bool:
    try:
        git(repo, "add", "-A")
        if not git(repo, "status", "--porcelain").stdout.strip():
            return True
        git(repo, "commit", "-m", message, "--quiet")
        git(repo, "push", "--quiet", "origin", "HEAD")
        return True
    except Exception as exc:
        LOG.error("push failed: %s", exc)
        return False


# --------------------------------------------------------------------------

def summarise(questions: list) -> str:
    """One human line describing what is being asked."""
    if not questions:
        return "A session needs a decision."
    q = questions[0]
    text = q.get("question") or q.get("header") or "A session needs a decision."
    extra = len(questions) - 1
    return f"{text}{f' (+{extra} more)' if extra > 0 else ''}"


class Bridge:
    def __init__(self, cfg: dict):
        self.repo = Path(cfg["state_repo"]).expanduser().resolve()
        self.ov = Overseer(cfg.get("overseer_url", "http://127.0.0.1:7777/api"))
        self.notify = Notifier(cfg.get("notify") or {})
        self.poll = int(cfg.get("ask_poll_seconds", POLL_SECONDS))

        self.asks = self.repo / "asks"
        self.answers = self.repo / "answers"
        self.asks.mkdir(parents=True, exist_ok=True)
        self.answers.mkdir(parents=True, exist_ok=True)

        self.cursor_file = self.repo / ".ask-cursor"
        self.last_id = int(self.cursor_file.read_text().strip()) if self.cursor_file.exists() else 0
        self.pending: dict[str, dict] = {}      # question_id -> local tracking
        self.warned_down = False

    # -- inbound: OVERSEER asked something ---------------------------------

    def record_ask(self, ev: dict) -> None:
        payload = ev.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        payload = payload or {}

        qid = payload.get("question_id")
        if not qid or (self.asks / f"{qid}.json").exists():
            return

        questions = payload.get("questions") or []
        session_id = ev.get("session_id") or ""
        headline = summarise(questions)

        (self.asks / f"{qid}.json").write_text(json.dumps({
            "question_id": qid,
            "session_id": session_id,
            "questions": questions,
            "headline": headline,
            "askedAt": utcnow(),
            "state": "waiting",
        }, indent=2), encoding="utf-8")

        self.pending[qid] = {"at": time.time(), "session": session_id,
                             "emailed": False, "expired": False,
                             "headline": headline}

        LOG.info("ask %s: %s", qid, headline)
        self.notify.push("Noriki needs a decision", headline)

    # -- state transitions --------------------------------------------------

    def escalate(self) -> bool:
        """Email after a delay; mark expired once OVERSEER's live window closes."""
        changed = False
        for qid, p in list(self.pending.items()):
            age = time.time() - p["at"]
            path = self.asks / f"{qid}.json"

            if not p["emailed"] and age >= self.notify.email_after and self.notify.email_enabled:
                self.notify.email(
                    "Noriki needs a decision",
                    f"{p['headline']}\n\n"
                    f"Asked {int(age // 60)} minute(s) ago and still unanswered.\n"
                    f"{self.notify.app_url or 'Open Noriki to answer.'}\n\n"
                    f"If you answer after ~10 minutes the session will have stopped waiting, "
                    f"but your answer is still delivered and the work picks up from there."
                )
                p["emailed"] = True

            if not p["expired"] and age >= LIVE_WINDOW:
                p["expired"] = True
                if path.exists():
                    try:
                        doc = json.loads(path.read_text(encoding="utf-8"))
                        doc["state"] = "expired"
                        doc["note"] = ("The session stopped waiting, but this is still "
                                       "worth answering — your answer is delivered as a "
                                       "follow-up and the work resumes.")
                        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                        changed = True
                    except Exception:
                        pass
                LOG.info("ask %s passed the live window; still answerable", qid)
        return changed

    # -- outbound: the phone answered --------------------------------------

    def deliver_answers(self) -> bool:
        changed = False
        for path in sorted(self.answers.glob("*.json")):
            qid = path.stem
            ask_path = self.asks / f"{qid}.json"
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if doc.get("delivered"):
                continue

            answers = doc.get("answers") or {}
            session_id = doc.get("session_id") or self.pending.get(qid, {}).get("session", "")
            if not session_id:
                continue

            result = self.ov.answer(session_id, qid, answers)

            if result == "expired":
                summary = "; ".join(f"{k}: {v}" for k, v in answers.items())
                ok = self.ov.follow_up(
                    session_id,
                    f"Answering your earlier question (you had stopped waiting): {summary}. "
                    f"Continue from there.")
                result = "resumed" if ok else "undeliverable"

            doc["delivered"] = True
            doc["result"] = result
            doc["deliveredAt"] = utcnow()
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

            if ask_path.exists():
                try:
                    a = json.loads(ask_path.read_text(encoding="utf-8"))
                    a["state"] = "answered"
                    a["result"] = result
                    a["answeredAt"] = utcnow()
                    ask_path.write_text(json.dumps(a, indent=2), encoding="utf-8")
                except Exception:
                    pass

            self.pending.pop(qid, None)
            changed = True
            LOG.info("delivered answer for %s (%s)", qid, result)
        return changed

    # -- loop ---------------------------------------------------------------

    def tick(self) -> None:
        if not self.ov.alive():
            if not self.warned_down:
                LOG.warning("OVERSEER is not responding at %s — is the daemon running?", self.ov.base)
                self.warned_down = True
            return
        if self.warned_down:
            LOG.info("OVERSEER is back")
            self.warned_down = False

        new_asks = self.ov.asks_since(self.last_id)
        dirty = False

        for ev in new_asks:
            self.record_ask(ev)
            self.last_id = max(self.last_id, int(ev["id"]))
            dirty = True
        if new_asks:
            self.cursor_file.write_text(str(self.last_id), encoding="utf-8")

        if self.escalate():
            dirty = True

        if dirty:
            push_repo(self.repo, f"noriki: {len(new_asks)} question(s) for you")

        if pull(self.repo) and self.deliver_answers():
            push_repo(self.repo, "noriki: answers delivered")

    def run(self) -> None:
        LOG.info("ask-bridge up — overseer=%s push=%s email=%s",
                 self.ov.base,
                 "on" if self.notify.push_enabled else "off",
                 "on" if self.notify.email_enabled else "off")
        if not self.notify.push_enabled:
            LOG.warning("No ntfy topic configured — questions will reach your phone "
                        "only when you open the app.")
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                LOG.info("ask-bridge stopped")
                return
            except Exception:
                LOG.exception("tick failed — continuing")
            time.sleep(self.poll)


def main() -> None:
    ap = argparse.ArgumentParser(description="Bridge OVERSEER's ask_user to your phone")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-notify", action="store_true",
                    help="send a test push and email, then exit")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(Path(args.config).parent / "ask-bridge.log",
                                      encoding="utf-8")],
    )

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        raise SystemExit(f"No config at {cfg_path}. Copy config.example.json first.")
    # utf-8-sig, not utf-8: Notepad and PowerShell both write a BOM, and json
    # rejects it outright. This file gets hand-edited, so tolerate both.
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{cfg_path} is not valid JSON: {exc}") from exc

    if args.test_notify:
        n = Notifier(cfg.get("notify") or {})

        if not n.push_enabled:
            print("PUSH   not configured (no ntfy_topic)")
        else:
            ok = n.push("Noriki test", "If you can read this on your phone, push works.")
            print(f"PUSH   {'DELIVERED' if ok else 'FAILED — see the warning above'}")

        if not n.email_enabled:
            print("EMAIL  not configured (no email_to)")
        elif not (n.smtp.get("host") or "").strip():
            print("EMAIL  not configured — email_to is set but smtp.host is empty.")
            print("       ntfy.sh will not relay mail for anonymous senders, so a real")
            print("       SMTP host is required. For Gmail: smtp.gmail.com:587 with a")
            print("       16-character App Password.")
        else:
            ok = n.email("Noriki test", "If you can read this, email works.")
            print(f"EMAIL  {'DELIVERED' if ok else 'FAILED — see the warning above'}")
        return

    bridge = Bridge(cfg)
    if args.once:
        bridge.tick()
    else:
        bridge.run()


if __name__ == "__main__":
    main()
