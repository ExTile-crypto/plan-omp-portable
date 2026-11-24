#!/usr/bin/env python3
"""Follow /plan-omp live: the current step, files Coder-L reads or edits, commands, speed and results.

  plan-omp-watch            follow the newest step and any steps after it (Ctrl+C to stop)
  plan-omp-watch --once     print the newest step and exit
  plan-omp-watch --repo .   only follow steps for one repository
  plan-omp-watch --tokens   print Claude and local token use and exit
  plan-omp-watch --reset    start the token counters from zero for this repository
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_RUNS = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omp-exec" / "runs"
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]+")
HEARTBEAT_S = 20
CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
OMP_SESSIONS = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".omp" / "agent")) / "sessions"
OMP_SKILL = re.compile(r'invoked the "plan-omp[\w-]*" skill')
RESET_FILE = DEFAULT_RUNS.parent / "usage-reset.json"
TOKEN_FIELDS = (("input", "input_tokens"), ("write", "cache_creation_input_tokens"), ("read", "cache_read_input_tokens"), ("output", "output_tokens"))
TOKEN_REFRESH_S = 5
TOKEN_PRINT_S = 30


def clean(value, limit: int = 120) -> str:
    """Model output is untrusted, so never let it send terminal control sequences."""
    text = re.sub(r" +", " ", CONTROL.sub(" ", str(value))).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clock(stamp) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(stamp)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "--:--:--"


def span(seconds) -> str:
    try:
        whole = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "?"
    return f"{whole // 60}m {whole % 60:02d}s" if whole >= 60 else f"{whole}s"


def compact(number) -> str:
    try:
        value = float(number)
    except (TypeError, ValueError):
        return "?"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    return f"{value / 1000:.1f}K" if value >= 1000 else f"{value:.0f}"


class View:
    STATUS = {
        "ok": "Patch is ready for the Claude advisor and review.",
        "proposed": "Approach is ready for the Claude advisor.",
        "unexpected_changes": "The approach check changed files, so its result should be rejected.",
        "no_approach": "Coder-L returned no approach.",
        "check_failed": "The check failed. Claude will review it or retry.",
        "no_changes": "Coder-L made no file changes.",
        "timeout": "The step hit its time limit.",
        "executor_failed": "omp stopped with an error.",
    }

    def __init__(self, color: bool):
        self.color = color
        self.reset()

    def reset(self):
        self.tokens = 0
        self.generating = 0.0

    def paint(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def out(self, text: str = ""):
        print(text, flush=True)

    def row(self, r: dict):
        kind, at = r.get("kind"), self.paint("2", clock(r.get("t")))
        if kind == "start":
            self.reset()
            self.out()
            self.out(self.paint("1;36", ("━━ Approach check: " if r.get("phase") == "propose" else "━━ Step: ") + clean(r.get("title"), 100)))
            self.out(self.paint("2", f"   {clean(r.get('repo_name'), 40)} · {clean(r.get('model'), 60)} · run {clean(r.get('run'), 40)}"))
        elif kind == "activity":
            self.out(f"{at} {self.paint('2', clean(r.get('state'), 40) + '…')}")
        elif kind == "tool_start":
            self.out(f"{at} {self.paint('34', '→')} {self.paint('1', f'{clean(r.get('tool'), 18):<6}')} {clean(r.get('target'))}")
        elif kind == "tool_end":
            if not r.get("ok"):
                self.out(f"{at} {self.paint('31', '✗ ' + clean(r.get('tool'), 18) + ' failed')}")
            elif (r.get("seconds") or 0) >= 2:
                self.out(f"{at} {self.paint('32', '✓')} {clean(r.get('tool'), 18)} finished in {span(r.get('seconds'))}")
        elif kind == "turn":
            output = r.get("output") or 0
            tps = r.get("tps") or 0
            self.tokens += output
            if tps > 0:
                self.generating += output / tps
            self.out(f"{at} {self.paint('2', f'{tps:.0f} tok/s · {compact(output)} tokens · context {compact(r.get('context'))}')}")
            if r.get("error"):
                self.out(f"{at} {self.paint('31', 'Model error: ' + clean(r.get('error'), 160))}")
        elif kind == "check_start":
            self.out(f"{at} {self.paint('34', '→')} {self.paint('1', 'check ')} {clean(r.get('command'))}")
        elif kind == "check_end":
            ok = bool(r.get("passed"))
            self.out(f"{at} {self.paint('32', '✓ check passed') if ok else self.paint('31', '✗ check failed (exit ' + clean(r.get('exit_code'), 8) + ')')}")
        elif kind == "end":
            status = clean(r.get("status"), 24)
            files = r.get("files") if isinstance(r.get("files"), list) else []
            speed = f", {self.tokens / self.generating:.0f} tok/s" if self.generating > 0 else ""
            mark = self.paint("1;32", "✓ Approach ready") if status == "proposed" else self.paint("1;32", "✓ Step finished") if status == "ok" else self.paint("1;31", f"✗ Step ended: {status}")
            self.out(f"{at} {mark} in {span(r.get('seconds'))} · {len(files)} file(s) · {compact(self.tokens)} tokens{speed}")
            for name in files[:10]:
                self.out(f"           {clean(name)}")
            if len(files) > 10:
                self.out(f"           and {len(files) - 10} more")
            self.out(f"           {self.STATUS.get(status, 'Check the run log.')}")


def first_row(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            row = json.loads(f.readline())
            return row if isinstance(row, dict) else {}
    except (OSError, ValueError):
        return {}


def newest(runs: Path, repo: str | None) -> Path | None:
    for path in sorted(runs.glob("*/progress.jsonl"), key=lambda p: p.parent.name, reverse=True):
        if repo is None or first_row(path).get("repo") == repo:
            return path
    return None


def repo_root(value: str) -> str:
    r = subprocess.run(["git", "-C", value, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else str(Path(value).resolve())


def model_name(model: str) -> str:
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)-(\d+)(?:-.*)?", model)
    return f"{m[1].title()} {m[2]}.{m[3]}" if m else clean(model, 30)


def new_lines(path: Path, state: list) -> list:
    """Return complete lines added since the last read. state is [offset, partial line, ...]."""
    try:
        with path.open("rb") as f:
            if os.fstat(f.fileno()).st_size < state[0]:
                state[0], state[1] = 0, b""
            f.seek(state[0])
            data = f.read()
            state[0] = f.tell()
    except OSError:
        return []
    lines = (state[1] + data).split(b"\n")
    state[1] = lines.pop()
    return lines


def iso_epoch(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def local_name(model) -> str:
    text = str(model).lower()
    for key, name in (("coder-l", "Coder-L"), ("gyro-m", "Gyro-M"), ("inkling", "Inkling")):
        if key in text:
            return name
    return clean(model, 24)


def amount(value) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < float("inf") else 0


def read_resets() -> dict:
    try:
        data = json.loads(RESET_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def reset_time(repo):
    return amount(read_resets().get(repo)) or None


def save_reset(repo: str) -> float:
    now = time.time()
    data = read_resets()
    data[repo] = now
    RESET_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = RESET_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data))
    temp.replace(RESET_FILE)
    return now


def is_plan_command(entry) -> bool:
    message = entry.get("message") if isinstance(entry, dict) and entry.get("type") == "user" else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = next((b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"), None)
    return isinstance(content, str) and content.lstrip().startswith("<command-") and "<command-name>/plan-omp</command-name>" in content


class ClaudeUsage:
    """Per-model totals for the Claude session driving /plan-omp, including its advisor subagents.

    Only usage numbers and step launch commands are read from the local transcripts.
    """

    def __init__(self, projects: Path):
        self.projects, self.repo, self.session = projects, None, None
        self.reset, self.after = None, None
        self.launches: dict = {}
        self.clear()

    def clear(self):
        self.offsets: dict = {}
        self.messages: dict = {}
        self.context, self.context_model, self.started = 0, None, None

    def set_repo(self, repo):
        if repo and repo != self.repo:
            self.repo, self.session = repo, None
            self.launches = {}
            self.clear()

    @property
    def folder(self) -> Path:
        return self.projects / re.sub(r"[^A-Za-z0-9]", "-", self.repo)

    def find_session(self, after=None):
        """The Claude session with the newest /plan-omp command, unless an omp-driven run started after it."""
        for path in self.folder.glob("*.jsonl"):
            state = self.launches.setdefault(path, [0, b"", "", ""])
            for raw in new_lines(path, state):
                if b"<command-name>/plan-omp</command-name>" not in raw:
                    continue
                try:
                    entry = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if is_plan_command(entry):
                    stamp = str(entry.get("timestamp") or "")
                    state[2] = max(state[2], stamp)
                    state[3] = min(state[3] or stamp, stamp)
        found = [(state[2], path.stem) for path, state in self.launches.items() if state[2]]
        if not found:
            return None
        stamp, session = max(found)
        return None if after is not None and (iso_epoch(stamp) or 0) < after else session

    def add(self, raw: bytes, is_main: bool):
        if b'"usage"' not in raw:
            return
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        message = entry.get("message") if isinstance(entry, dict) and entry.get("type") == "assistant" else None
        if not isinstance(message, dict):
            return
        if self.reset is not None and (iso_epoch(entry.get("timestamp")) or 0) < self.reset:
            return
        model, usage = message.get("model"), message.get("usage")
        if not isinstance(model, str) or model.startswith("<") or not isinstance(usage, dict):
            return
        # A streamed message repeats the same usage on each content block, so its id counts once.
        self.messages[(model, message.get("id") or entry.get("uuid"))] = usage
        if is_main and not entry.get("isSidechain"):
            self.context = sum(self.number(usage, f) for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            self.context_model = model

    @staticmethod
    def number(usage: dict, field: str) -> int:
        value = usage.get(field)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    def totals(self) -> dict:
        result: dict = {}
        for (model, _), usage in self.messages.items():
            row = result.setdefault(model, {"requests": 0, "input": 0, "write": 0, "read": 0, "output": 0})
            row["requests"] += 1
            for name, field in TOKEN_FIELDS:
                row[name] += self.number(usage, field)
        return result

    def refresh(self, after=None) -> bool:
        """Read new transcript lines and return True if the totals changed."""
        if not self.repo:
            return False
        before = self.totals()
        self.after = after
        reset = reset_time(self.repo)
        if reset != self.reset:
            self.reset = reset
            self.clear()
        session = self.find_session(after)
        if session != self.session:
            self.session = session
            self.clear()
        if session is None:
            return bool(before)
        main = self.folder / f"{session}.jsonl"
        self.started = iso_epoch(self.launches[main][3]) if main in self.launches else None
        for path in [main, *sorted((self.folder / session).rglob("*.jsonl"))]:
            state = self.offsets.setdefault(path, [0, b""])
            for raw in new_lines(path, state):
                self.add(raw, path == main)
        return self.totals() != before

    def ordered(self) -> list:
        return sorted(self.totals().items(), key=lambda item: (item[0] != self.context_model, item[0]))

    def line(self) -> str:
        parts = []
        for model, t in self.ordered():
            context = f", context {compact(self.context)}" if model == self.context_model else ""
            parts.append(f"{model_name(model)} {compact(t['output'])} out, {compact(t['write'])} write, {compact(t['read'])} reread{context}")
        return f"{clock(time.time())} Claude · " + " · ".join(parts)

    def report(self) -> list:
        rows = self.ordered()
        if not rows:
            if self.after is not None and self.session is None:
                return ["No Claude Code session in this run. omp is driving it."]
            return ["No Claude /plan-omp usage found for this repository."]
        lines = [f"Claude usage · session {clean(self.session, 40)[:8]}"]
        def text(name, t, extra=""):
            return (f"  {name:<11}{t['requests']:>4} requests · {compact(t['input'])} input · {compact(t['write'])} cache write · "
                    f"{compact(t['read'])} cache reread · {compact(t['output'])} output{extra}")
        for model, t in rows:
            lines.append(text(model_name(model), t, f" · context {compact(self.context)}" if model == self.context_model else ""))
        if len(rows) > 1:
            lines.append(text("Total", {k: sum(t[k] for _, t in rows) for k in rows[0][1]}))
        return lines


class LocalUsage:
    """Local-model totals from /plan-omp step logs and omp sessions that started the skill."""

    def __init__(self, runs: Path, sessions: Path = OMP_SESSIONS):
        self.runs, self.sessions, self.repo, self.since = runs, sessions, None, None
        self.files: dict = {}
        self.omp: dict = {}

    @staticmethod
    def blank() -> list:
        return [0, b"", {"repo": None, "model": None, "t": None, "turns": 0, "output": 0, "prompt": 0, "read": 0, "write": 0, "split": 0, "seconds": 0.0, "peak": 0}]

    def read(self):
        for path in self.runs.glob("*/progress.jsonl"):
            state = self.files.setdefault(path, self.blank())
            try:
                if path.stat().st_size < state[0]:
                    state = self.files[path] = self.blank()
            except OSError:
                continue
            run = state[2]
            for raw in new_lines(path, state):
                try:
                    row = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("kind") == "start":
                    run["repo"], run["model"], run["t"] = row.get("repo"), row.get("model"), row.get("t")
                elif row.get("kind") == "turn":
                    output, context, tps = amount(row.get("output")), amount(row.get("context")), amount(row.get("tps"))
                    run["turns"] += 1
                    run["output"] += output
                    run["prompt"] += context
                    if "cache_read" in row:
                        run["read"] += amount(row.get("cache_read"))
                        run["split"] += context
                    run["peak"] = max(run["peak"], context)
                    if tps:
                        run["seconds"] += output / tps
        for path in self.sessions.glob("*/*.jsonl"):
            self.scan_omp(path, path)
            for child in path.with_suffix("").rglob("*.jsonl"):
                if child.name != "changes.jsonl":
                    self.scan_omp(child, path)

    def scan_omp(self, path: Path, owner: Path):
        blank = lambda: [0, b"", {"owner": owner, "child": path != owner, "cwd": None, "marks": [], "turns": []}]
        state = self.omp.setdefault(path, blank())
        try:
            if path.stat().st_size < state[0]:
                state = self.omp[path] = blank()
        except OSError:
            return
        info = state[2]
        for raw in new_lines(path, state):
            if not any(key in raw for key in (b'"type":"session"', b"skill-prompt", b'"role":"assistant"')):
                continue
            try:
                row = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            stamp = iso_epoch(row.get("timestamp"))
            if row.get("type") == "session":
                info["cwd"] = row.get("cwd")
            elif row.get("type") == "custom_message" and row.get("customType") == "skill-prompt":
                if stamp and OMP_SKILL.search(str(row.get("content"))):
                    info["marks"].append(stamp)
            elif row.get("type") == "message" and isinstance(row.get("message"), dict) and row["message"].get("role") == "assistant":
                message, usage = row["message"], row["message"].get("usage")
                if stamp and isinstance(usage, dict):
                    read, write = amount(usage.get("cacheRead")), amount(usage.get("cacheWrite"))
                    prompt = amount(usage.get("input")) + read + write
                    info["turns"].append((stamp, str(message.get("provider")), message.get("model"), amount(usage.get("output")), prompt, read, write))

    def driver(self, repo):
        """When the newest omp session for this repository started the skill."""
        marks = [mark for state in self.omp.values() if state[2]["cwd"] == repo for mark in state[2]["marks"]]
        return max(marks) if marks else None

    def totals(self) -> dict:
        result: dict = {}
        for state in self.files.values():
            run = state[2]
            if self.repo is None or run["repo"] != self.repo or not amount(run["t"]):
                continue
            if self.since is not None and run["t"] < self.since - 1:
                continue
            row = result.setdefault(local_name(run["model"]), self.new_row(""))
            row["runs"] += 1
            for key in ("turns", "output", "prompt", "read", "write", "split", "seconds"):
                row[key] += run[key]
            row["peak"] = max(row["peak"], run["peak"])
        owners: dict = {}
        for state in self.omp.values():
            info = state[2]
            parent = self.omp.get(info["owner"])
            if self.repo is None or parent is None or parent[2]["cwd"] != self.repo or not parent[2]["marks"]:
                continue
            start = min(parent[2]["marks"])
            for stamp, provider, model, output, prompt, read, write in info["turns"]:
                if stamp < start or (self.since is not None and stamp < self.since - 1):
                    continue
                is_local = provider == "llama.cpp"
                name = f"{local_name(model)} {'subagent' if info['child'] else 'planner'}" if is_local else model_name(str(model)) if provider == "anthropic" else f"{clean(provider, 12)}/{clean(model, 24)}"
                row = result.setdefault(name, self.new_row("" if is_local else provider))
                if info["owner"] not in owners.setdefault(name, set()):
                    owners[name].add(info["owner"])
                    row["runs"] += 1
                row["turns"] += 1
                row["output"] += output
                row["prompt"] += prompt
                row["read"] += read
                row["write"] += write
                row["split"] += prompt
                row["peak"] = max(row["peak"], prompt)
        return result

    @staticmethod
    def new_row(provider: str) -> dict:
        return {"provider": provider, "runs": 0, "turns": 0, "output": 0, "prompt": 0, "read": 0, "write": 0, "split": 0, "seconds": 0.0, "peak": 0}

    def sections(self) -> list:
        rows = sorted(self.totals().items())
        local = [(name, t) for name, t in rows if not t["provider"]]
        cloud = [(name, t) for name, t in rows if t["provider"]]
        result = [("36", "Local", local)] if local else []
        if cloud:
            title = "Anthropic via omp" if all(t["provider"] == "anthropic" for _, t in cloud) else "Cloud via omp"
            result.append(("35", title, cloud))
        return result

    @staticmethod
    def reread(t: dict) -> str:
        return ("≥" if t["split"] < t["prompt"] else "") + compact(t["read"])

    def refresh(self, usage: ClaudeUsage) -> tuple:
        """Update both counters and return whether the Claude and local totals changed."""
        before = self.totals()
        self.read()
        driver = self.driver(usage.repo)
        claude_changed = usage.refresh(driver)
        self.repo = usage.repo
        starts = [usage.started or driver, reset_time(usage.repo)]
        self.since = max((s for s in starts if s), default=None)
        return claude_changed, self.totals() != before

    @staticmethod
    def speed(t: dict) -> str:
        return f"{t['output'] / t['seconds']:.0f}" if t["seconds"] > 0 else "?"

    @staticmethod
    def count(runs: int) -> str:
        return f"{runs} run" if runs == 1 else f"{runs} runs"

    def lines(self) -> list:
        result = []
        for code, title, rows in self.sections():
            parts = []
            for name, t in rows:
                write = f"{compact(t['write'])} write, " if t["write"] else ""
                speed = f"{self.speed(t)} tok/s, " if t["seconds"] > 0 else ""
                parts.append(f"{name} {compact(t['output'])} out, {compact(t['prompt'])} prompt, {self.reread(t)} reread, {write}{speed}{self.count(t['runs'])}")
            result.append((code, f"{clock(time.time())} {title} · " + " · ".join(parts)))
        return result

    def report(self) -> list:
        def text(name, t):
            write = f"{compact(t['write'])} cache write · " if t["write"] else ""
            speed = f"{self.speed(t)} tok/s · {span(t['seconds'])} generating · " if t["seconds"] > 0 else ""
            return (f"  {name:<16}{self.count(t['runs']):>9} · {t['turns']} turns · {compact(t['prompt'])} prompt · {self.reread(t)} cache reread · {write}"
                    f"{compact(t['output'])} output · {speed}peak context {compact(t['peak'])}")
        result = []
        for code, title, rows in self.sections():
            result.append((code, f"{title} usage"))
            result += [(code, text(name, t)) for name, t in rows]
            if len(rows) > 1:
                total = {k: sum(t[k] for _, t in rows) for k in ("runs", "turns", "output", "prompt", "read", "write", "split", "seconds")}
                total["peak"] = max(t["peak"] for _, t in rows)
                if any(t["output"] and not t["seconds"] for _, t in rows):
                    total["seconds"] = 0
                result.append((code, text("Total", total)))
        return result


class Follower:
    def __init__(self, path: Path, view: View):
        self.path, self.view, self.pos, self.partial = path, view, 0, b""
        self.ended, self.pid, self.last = False, None, time.time()

    def drain(self):
        try:
            with self.path.open("rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            return
        if not data:
            return
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        for raw in lines:
            try:
                row = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("kind") == "start":
                self.pid = row.get("pid")
            elif row.get("kind") == "end":
                self.ended = True
            self.view.row(row)
            self.last = time.time()

    def lost(self) -> bool:
        return not self.ended and isinstance(self.pid, int) and not Path(f"/proc/{self.pid}").exists()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="print the newest step and exit")
    p.add_argument("--repo", help="only follow steps for this repository")
    p.add_argument("--runs-dir", default=str(DEFAULT_RUNS), help=argparse.SUPPRESS)
    p.add_argument("--no-color", action="store_true", help="plain text output")
    p.add_argument("--tokens", action="store_true", help="print Claude and local token use and exit")
    p.add_argument("--reset", action="store_true", help="start the token counters from zero for this repository; logs are kept")
    p.add_argument("--claude-dir", default=str(CLAUDE_PROJECTS), help=argparse.SUPPRESS)
    a = p.parse_args()
    runs = Path(a.runs_dir).expanduser()
    repo = repo_root(a.repo) if a.repo else None
    color = sys.stdout.isatty() and not a.no_color and "NO_COLOR" not in os.environ
    view = View(color)
    usage = ClaudeUsage(Path(a.claude_dir).expanduser())
    local = LocalUsage(runs)

    if a.reset:
        target = repo or repo_root(".")
        at = save_reset(target)
        view.out(f"Token counters for {clean(target, 80)} now start at {clock(at)}. Logs were kept.")
        return 0

    if a.tokens:
        path = newest(runs, repo)
        usage.set_repo(repo or (first_row(path).get("repo") if path else None))
        local.refresh(usage)
        for text in usage.report():
            view.out(view.paint("35", text))
        for code, text in local.report():
            view.out(view.paint(code, text))
        return 0

    if a.once:
        path = newest(runs, repo)
        if path is None:
            view.out("No /plan-omp steps with progress yet.")
            return 0
        follower = Follower(path, view)
        follower.drain()
        if follower.lost():
            view.out(view.paint("31", "The step stopped without a final result."))
        elif not follower.ended:
            view.out(view.paint("2", "Still running."))
        usage.set_repo(repo or first_row(path).get("repo"))
        local.refresh(usage)
        for text in usage.report():
            view.out(view.paint("35", text))
        for code, text in local.report():
            view.out(view.paint(code, text))
        return 0

    view.out(view.paint("1", "Watching /plan-omp. Press Ctrl+C to stop watching; this does not stop the run."))
    follower, waiting = None, False
    usage_at, printed_at, usage_changed, local_changed = 0.0, 0.0, False, False
    try:
        while True:
            path = newest(runs, repo)
            if path is not None and (follower is None or path != follower.path):
                if follower is not None:
                    follower.drain()
                follower, waiting = Follower(path, view), False
            if follower is not None:
                follower.drain()
                if follower.lost():
                    view.out(view.paint("31", "The step stopped without a final result."))
                    follower.ended = True
                if not follower.ended and time.time() - follower.last >= HEARTBEAT_S:
                    view.out(view.paint("2", f"{clock(time.time())} still working, {span(time.time() - follower.last)} since the last update"))
                    follower.last = time.time()
            if (follower is None or follower.ended) and not waiting:
                view.out(view.paint("2", "Waiting for the next /plan-omp step…"))
                waiting = True
            now = time.time()
            if now - usage_at >= TOKEN_REFRESH_S:
                usage_at = now
                usage.set_repo(repo or (first_row(follower.path).get("repo") if follower else None))
                claude_now, local_now = local.refresh(usage)
                usage_changed, local_changed = usage_changed or claude_now, local_changed or local_now
                if (usage_changed or local_changed) and now - printed_at >= TOKEN_PRINT_S:
                    if usage_changed and usage.messages:
                        view.out(view.paint("35", usage.line()))
                    if local_changed:
                        for code, text in local.lines():
                            view.out(view.paint(code, text))
                    printed_at, usage_changed, local_changed = now, False, False
            time.sleep(0.4)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
