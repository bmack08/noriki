"""Noriki relay — the bridge between your phone and this PC.

The private state repo is the message bus. This daemon:

    pull  ->  read inbox/*.json  ->  run headless Claude Code in the
    project's own folder  ->  write outbox/<id>.json  ->  commit  ->  push

Your PC only ever makes OUTBOUND connections to GitHub. No tunnel, no
inbound port, no auth layer of ours on the public internet. OVERSEER's
localhost-only decision stays intact.

    python noriki_relay.py --config config.json

Safety notes, in order of importance:

  * A message can only run in a directory listed in `projects` in the
    config. A prompt asking for anything else is rejected, logged, and
    answered with an error. This is the guard that matters: without it a
    leaked token would mean arbitrary code execution anywhere on disk.
  * Frozen regions stay protected — the PreToolUse gatekeeper hook runs
    inside the spawned session exactly as it does interactively.
  * `permission_mode` defaults to acceptEdits: file edits proceed, but
    Claude still asks before destructive shell commands, and those asks
    fail closed in headless mode.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("noriki.relay")

POLL_SECONDS = 15
GIT_TIMEOUT = 120


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------

def git(repo: Path, *args: str, check: bool = True, timeout: int = GIT_TIMEOUT):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc


def pull(repo: Path) -> bool:
    """Fetch and fast-forward. Returns False on a transient network failure."""
    try:
        git(repo, "fetch", "--quiet", "origin")
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        git(repo, "reset", "--hard", f"origin/{branch}", "--quiet")
        return True
    except Exception as exc:                      # offline, DNS, auth hiccup
        LOG.warning("pull failed (will retry): %s", exc)
        return False


def push(repo: Path, message: str) -> bool:
    try:
        git(repo, "add", "-A")
        status = git(repo, "status", "--porcelain").stdout.strip()
        if not status:
            return True
        git(repo, "commit", "-m", message, "--quiet")
        git(repo, "push", "--quiet", "origin", "HEAD")
        return True
    except Exception as exc:
        LOG.error("push failed: %s", exc)
        return False


# --------------------------------------------------------------------------
# claude
# --------------------------------------------------------------------------

def find_claude() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd",
    ):
        if candidate.exists():
            return str(candidate)
    raise SystemExit(
        "Could not find the `claude` executable. Install Claude Code, or set "
        "`claude_path` in the relay config."
    )


def run_claude(claude: str, cwd: Path, prompt: str, mode: str, timeout: int) -> dict:
    """Run one headless session. Returns a normalised result dict."""
    cmd = [
        claude, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", mode,
    ]
    started = time.time()
    LOG.info("running in %s: %s", cwd, prompt[:120].replace("\n", " "))

    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reply": f"Timed out after {timeout // 60} minutes. The task may be too large "
                     f"for one run — try splitting it, or raise `task_timeout` in the relay config.",
            "durationMs": int((time.time() - started) * 1000),
        }

    elapsed = int((time.time() - started) * 1000)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:800]
        return {"ok": False, "reply": f"Claude exited {proc.returncode}.\n\n{detail}",
                "durationMs": elapsed}

    # --output-format json gives us the result plus cost metadata
    try:
        payload = json.loads(proc.stdout)
        reply = payload.get("result") or payload.get("text") or proc.stdout
        return {
            "ok": not payload.get("is_error", False),
            "reply": reply.strip(),
            "durationMs": payload.get("duration_ms", elapsed),
            "costUsd": payload.get("total_cost_usd"),
            "turns": payload.get("num_turns"),
        }
    except json.JSONDecodeError:
        return {"ok": True, "reply": proc.stdout.strip(), "durationMs": elapsed}


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

class Relay:
    def __init__(self, cfg: dict):
        self.repo = Path(cfg["state_repo"]).expanduser().resolve()
        self.claude = cfg.get("claude_path") or find_claude()
        self.mode = cfg.get("permission_mode", "acceptEdits")
        self.timeout = int(cfg.get("task_timeout", 1800))
        self.poll = int(cfg.get("poll_seconds", POLL_SECONDS))

        # allowlist: message.cwd must resolve to one of these
        self.allowed = {}
        for pid, path in cfg["projects"].items():
            p = Path(path).expanduser()
            if not p.is_dir():
                LOG.warning("project %s: %s is not a directory — skipping", pid, p)
                continue
            self.allowed[pid] = p.resolve()

        if not self.allowed:
            raise SystemExit("No valid project directories in config — nothing to relay to.")

        self.inbox = self.repo / "inbox"
        self.outbox = self.repo / "outbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)

    # -- resolution & guards ------------------------------------------------

    def resolve_cwd(self, msg: dict) -> Path:
        """Map a message to a directory, refusing anything outside the allowlist."""
        pid = msg.get("project")
        if pid in self.allowed:
            return self.allowed[pid]
        raise PermissionError(
            f"Project '{pid}' is not in the relay allowlist. "
            f"Known projects: {', '.join(sorted(self.allowed))}."
        )

    # -- work ---------------------------------------------------------------

    def pending(self) -> list[Path]:
        out = []
        for f in sorted(self.inbox.glob("*.json")):
            if not (self.outbox / f.name).exists():
                out.append(f)
        return out

    def answer(self, msg_id: str, project: str, result: dict) -> None:
        payload = {
            "id": msg_id,
            "project": project,
            "completedAt": utcnow(),
            **result,
        }
        (self.outbox / f"{msg_id}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    def handle(self, path: Path) -> str:
        msg_id = path.stem
        try:
            msg = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.answer(msg_id, "?", {"ok": False, "reply": f"Unreadable message: {exc}"})
            return msg_id

        project = msg.get("project", "?")
        prompt = (msg.get("prompt") or "").strip()

        if not prompt:
            self.answer(msg_id, project, {"ok": False, "reply": "Empty prompt — nothing to do."})
            return msg_id

        try:
            cwd = self.resolve_cwd(msg)
        except PermissionError as exc:
            LOG.error("REJECTED %s: %s", msg_id, exc)
            self.answer(msg_id, project, {"ok": False, "reply": str(exc)})
            return msg_id

        result = run_claude(self.claude, cwd, prompt, self.mode, self.timeout)
        self.answer(msg_id, project, result)
        LOG.info("answered %s (%s, %sms)", msg_id,
                 "ok" if result["ok"] else "error", result.get("durationMs"))
        return msg_id

    def heartbeat(self) -> None:
        (self.repo / "relay-status.json").write_text(json.dumps({
            "alive": True,
            "at": utcnow(),
            # platform.node(), not os.uname(): the latter doesn't exist on Windows
            "host": platform.node() or "unknown",
            "projects": sorted(self.allowed),
            "permissionMode": self.mode,
        }, indent=2), encoding="utf-8")

    def tick(self) -> None:
        if not pull(self.repo):
            return

        work = self.pending()
        if not work:
            # heartbeat at most once a minute so we don't spam commits
            status = self.repo / "relay-status.json"
            stale = (not status.exists() or
                     time.time() - status.stat().st_mtime > 60)
            if stale:
                self.heartbeat()
                push(self.repo, "noriki: relay heartbeat")
            return

        LOG.info("%d message(s) waiting", len(work))
        done = []
        for path in work:
            try:
                done.append(self.handle(path))
            except Exception as exc:                      # never let one message kill the loop
                LOG.exception("handler crashed on %s", path.name)
                self.answer(path.stem, "?", {"ok": False, "reply": f"Relay error: {exc}"})
                done.append(path.stem)

        self.heartbeat()
        push(self.repo, f"noriki: {len(done)} repl{'y' if len(done) == 1 else 'ies'}")

    def run(self) -> None:
        LOG.info("relay up — repo=%s projects=%s mode=%s",
                 self.repo, ", ".join(sorted(self.allowed)), self.mode)
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                LOG.info("relay stopped")
                return
            except Exception:
                LOG.exception("tick failed — continuing")
            time.sleep(self.poll)


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Noriki phone-to-PC relay")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="process the queue once and exit")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(Path(args.config).parent / "relay.log", encoding="utf-8"),
        ],
    )

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        raise SystemExit(f"No config at {cfg_path}. Copy config.example.json and edit it.")

    # utf-8-sig: Notepad and PowerShell both write a BOM and json rejects it
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{cfg_path} is not valid JSON: {exc}") from exc

    relay = Relay(cfg)
    if args.once:
        relay.tick()
    else:
        relay.run()


if __name__ == "__main__":
    main()
