#!/usr/bin/env python3
"""Run one plan step with omp on the local model in a sandbox and return a reviewable patch.

The repository is never modified. Its tracked and non-ignored files are copied into a
bubblewrap sandbox with no network except a relay to the local model server. The sandbox
cannot see the real home directory, omp's saved logins, or any Claude credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8080"
SANDBOX_PORT = 8080
TAIL_LINES = 40
AGENT_DIR = "/home/agent/.omp/agent"

PROMPT = """You are the execution agent for one step of a plan written by a senior engineer.

Rules:
- Do exactly this step and nothing else. Do not refactor unrelated code.
- Work only inside /workspace. There is no internet access.
- Do not run git commit. Your file changes are collected automatically.
- If the step cannot be done as written, stop and explain why instead of guessing.
- Leave existing comments unchanged and add no new comments unless this step explicitly asks for them. Claude writes any needed comments during review.
- Finish with a short summary of what you changed and what you verified.

STEP:
{task}
"""

PROPOSE_PROMPT = """You are the execution agent preparing one step of a plan written by a senior engineer.

Do not edit files or run commands. Use only the read-only tools to inspect /workspace.
Reply with a concise approach:
- the files you will change and why
- the exact functions or behavior you will add or modify
- edge cases and how the step will be verified
- anything unclear or risky in the step

STEP:
{task}
"""

RELAY_SCRIPT = f"""
socat TCP-LISTEN:{SANDBOX_PORT},bind=127.0.0.1,fork,reuseaddr UNIX-CONNECT:/run/llm/llm.sock &
for _ in $(seq 50); do
  (exec 3<>/dev/tcp/127.0.0.1/{SANDBOX_PORT}) 2>/dev/null && break
  sleep 0.1
done
exec "$@"
"""


def fail(message: str) -> None:
    print(json.dumps({"status": "error", "error": message}, indent=2))
    sys.exit(1)


def discover_model(url: str) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url}/v1/models", timeout=5) as response:
            models = json.load(response).get("data") or []
    except Exception as exc:
        fail(f"Local model server is not reachable at {url}: {exc}. Start a model with strata-menu.")
    if not models:
        fail(f"Local model server at {url} reports no models.")
    return models[0]["id"]


def find_omp() -> Path:
    found = shutil.which("omp") or str(Path.home() / ".local/bin/omp")
    path = Path(found).resolve()
    if not path.is_file():
        fail("omp is not installed.")
    return path


def repo_root(path: str) -> Path:
    result = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"Not a git repository: {path}")
    return Path(result.stdout.strip())


def snapshot(repo: Path, dest: Path) -> None:
    """Copy tracked and untracked non-ignored files, including uncommitted edits."""
    listing = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-co", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    for raw in set(listing.split(b"\0")) - {b""}:
        relative = os.fsdecode(raw)
        source = repo / relative
        if not (source.is_symlink() or source.is_file()):
            continue
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def shadow_git(run: Path, *args: str) -> bytes:
    """Git against a private repo the sandbox cannot reach, so its config is trusted."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(run),
        "GIT_DIR": str(run / "shadow.git"),
        "GIT_WORK_TREE": str(run / "work"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    command = [
        "git",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "core.autocrlf=false",
        "-c", "user.name=omp-exec",
        "-c", "user.email=omp-exec@localhost",
        *args,
    ]
    return subprocess.run(command, env=env, check=True, capture_output=True).stdout


def init_shadow(run: Path) -> None:
    shadow_git(run, "init", "--quiet")
    info = run / "shadow.git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text("* -text -filter -ident\n")
    (info / "exclude").write_text(".omp/\n__pycache__/\n*.pyc\n")
    shadow_git(run, "add", "-A", "-f", ".")
    shadow_git(run, "commit", "--quiet", "--allow-empty", "-m", "baseline")


def make_writable(path: Path) -> None:
    subprocess.run(["chmod", "-R", "u+rwX", str(path)], stderr=subprocess.DEVNULL)


def remove_nested_git(work: Path) -> None:
    for root, dirs, _files in os.walk(work, followlinks=False):
        if ".git" in dirs:
            dirs.remove(".git")
        candidate = Path(root) / ".git"
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
        elif candidate.is_dir():
            shutil.rmtree(candidate)


def collect_patch(run: Path) -> tuple[Path, list[str], list[str]]:
    work = run / "work"
    make_writable(work)
    remove_nested_git(work)
    shadow_git(run, "add", "-A", ".")
    patch = run / "changes.patch"
    patch.write_bytes(
        shadow_git(run, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames")
    )
    raw = shadow_git(run, "diff", "--cached", "--raw", "-z", "--no-renames").split(b"\0")
    files: list[str] = []
    symlinks: list[str] = []
    for meta, name in zip(raw[0::2], raw[1::2]):
        path = os.fsdecode(name)
        files.append(path)
        modes = meta.decode().split()[:2]
        if any(mode.lstrip(":") == "120000" for mode in modes):
            symlinks.append(path)
    return patch, sorted(files), sorted(symlinks)


def write_sandbox_config(run: Path, model: str) -> Path:
    """A local-only config. The real omp config and saved logins never enter the sandbox."""
    selector = json.dumps(f"llama.cpp/{model}")
    roles = "".join(f"  {role}: {selector}\n" for role in ("default", "smol", "slow", "plan", "task", "commit"))
    path = run / "omp-config.yml"
    path.write_text("modelRoles:\n" + roles + "tools:\n  approvalMode: yolo\nadvisor:\n  enabled: false\nupdate:\n  auto: false\n")
    path.chmod(0o600)
    return path


def start_relay(url: str, socket_path: Path) -> subprocess.Popen:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    relay = subprocess.Popen(
        ["socat", f"UNIX-LISTEN:{socket_path},fork,mode=600", f"TCP:{host}:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        if socket_path.exists():
            return relay
        time.sleep(0.05)
    relay.terminate()
    fail("Could not start the relay to the local model server.")
    raise AssertionError


CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]+")


def clean(value, limit: int = 120) -> str:
    text = re.sub(r" +", " ", CONTROL.sub(" ", str(value))).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def step_title(task: str) -> str:
    for line in task.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return clean(line, 90)
    return "Untitled step"


def tool_target(args) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("path", "file_path", "command", "cmd", "pattern", "query", "url"):
        if args.get(key):
            return clean(args[key])
    paths = args.get("paths")
    return clean(", ".join(map(str, paths[:4]))) if isinstance(paths, list) and paths else ""


def assistant_text(message) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    return "".join(x.get("text", "") for x in message.get("content") or [] if isinstance(x, dict) and x.get("type") == "text").strip()


class Progress:
    """Turn omp's JSON event stream into a small timeline for plan-omp-watch."""

    STATES = {"thinking_start": "thinking", "toolcall_start": "preparing a tool call", "text_start": "writing its summary"}

    def __init__(self, run: Path, log: Path):
        self.path, self.log = run / "progress.jsonl", log
        self.file = self.path.open("a", buffering=1, encoding="utf-8")
        self.lock = threading.Lock()
        self.state, self.final, self.raw, self.tools = None, "", [], {}

    def emit(self, kind: str, **data) -> None:
        row = {"t": round(time.time(), 2), "kind": kind, **data}
        with self.lock:
            if not self.file.closed:
                self.file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def line(self, raw: bytes) -> None:
        try:
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                return
            try:
                event = json.loads(text)
            except ValueError:
                self.raw = (self.raw + [text[:1000]])[-TAIL_LINES:]
                return
            if isinstance(event, dict):
                self.handle(event)
        except Exception as exc:  # A display problem must never stall or fail the step.
            self.raw = (self.raw + [f"progress error: {exc}"])[-TAIL_LINES:]

    def handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "message_update":
            state = self.STATES.get((event.get("assistantMessageEvent") or {}).get("type"))
            if state and state != self.state:
                self.state = state
                self.emit("activity", state=state)
        elif kind == "tool_execution_start":
            self.state = None
            self.tools[event.get("toolCallId")] = time.time()
            self.emit("tool_start", tool=clean(event.get("toolName"), 30), target=tool_target(event.get("args")))
        elif kind == "tool_execution_end":
            started = self.tools.pop(event.get("toolCallId"), None)
            self.emit("tool_end", tool=clean(event.get("toolName"), 30), ok=not event.get("isError"), seconds=round(time.time() - started, 1) if started else None)
        elif kind == "turn_end":
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            output = usage.get("output") or 0
            generating = max((message.get("duration") or 0) - (message.get("ttft") or 0), 1) / 1000
            error = message.get("errorMessage")
            self.emit("turn", output=output, context=(usage.get("input") or 0) + (usage.get("cacheRead") or 0), cache_read=usage.get("cacheRead") or 0, tps=round(output / generating, 1), error=clean(error, 160) if error else None)
            self.final = assistant_text(message) or self.final
        elif kind == "agent_end":
            for message in reversed(event.get("messages") or []):
                text = assistant_text(message)
                if text:
                    self.final = text
                    break

    def write_output(self) -> None:
        parts = self.raw + ([self.final] if self.final else [])
        self.log.write_text("\n".join(parts) + ("\n" if parts else ""), encoding="utf-8", errors="replace")

    def close(self) -> None:
        with self.lock:
            self.file.close()


def run_sandboxed(command: list[str], work: Path, omp: Path, socket_dir: Path | None, timeout: int, log: Path, config: Path | None = None, on_line=None) -> int | None:
    args = [
        "bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/home/agent",
        "--setenv", "TERM", "dumb",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "NO_COLOR", "1",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PI_CODING_AGENT_DIR", AGENT_DIR,
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/sbin", "/sbin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
        "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
        "--ro-bind-try", "/etc/ssl", "/etc/ssl",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/home",
        "--dir", AGENT_DIR,
        "--ro-bind", str(omp), "/opt/omp/omp",
        "--bind", str(work), "/workspace",
    ]
    if socket_dir is not None:
        args += ["--bind", str(socket_dir), "/run/llm"]
    if config is not None:
        args += ["--ro-bind", str(config), "/opt/omp/config.yml"]
    args += ["--chdir", "/workspace", *command]

    if on_line is not None:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)

        def pump() -> None:
            for raw in process.stdout:
                on_line(raw)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            code = None
        reader.join(5)
        return code

    with log.open("ab") as output:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return None


def tail(path: Path, lines: int = TAIL_LINES) -> str:
    if not path.exists():
        return ""
    content = path.read_text(errors="replace").splitlines()
    return "\n".join(content[-lines:])


def parse_args() -> argparse.Namespace:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Git repository to work on")
    parser.add_argument("--task-file", required=True, help="File containing the step")
    parser.add_argument("--check", help="Command to verify the step inside the sandbox")
    parser.add_argument("--phase", choices=("implement", "propose"), default="implement", help="propose returns a read-only approach for the advisor")
    parser.add_argument("--url", default=os.environ.get("LOCAL_LLM_URL", DEFAULT_URL))
    parser.add_argument("--model", help="Model id (default: the model on the local server)")
    parser.add_argument("--timeout", type=int, default=1800, help="Executor timeout in seconds")
    parser.add_argument("--check-timeout", type=int, default=300)
    parser.add_argument("--runs-dir", default=str(cache / "omp-exec" / "runs"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for tool in ("bwrap", "socat", "git"):
        if shutil.which(tool) is None:
            fail(f"Required tool is missing: {tool}")
    omp = find_omp()
    repo = repo_root(args.repo)
    task = Path(args.task_file).read_text()
    url = args.url.rstrip("/")
    model = args.model or discover_model(url)

    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run = Path(args.runs_dir).expanduser().resolve() / stamp
    work = run / "work"
    work.mkdir(parents=True)
    log = run / "executor.log"
    progress = Progress(run, log)
    progress.emit("start", repo=str(repo), repo_name=repo.name, title=step_title(task), model=model, run=run.name, pid=os.getpid(), phase=args.phase)

    # Unix socket paths are limited to about 108 bytes, so keep this one short.
    socket_dir = Path(tempfile.mkdtemp(prefix="ox-", dir=os.environ.get("XDG_RUNTIME_DIR") or "/tmp"))
    relay = None
    started = time.time()
    try:
        snapshot(repo, work)
        init_shadow(run)
        config = write_sandbox_config(run, model)
        relay = start_relay(url, socket_dir / "llm.sock")
        phase_args = ["--tools", "read,grep,glob"] if args.phase == "propose" else []
        prompt = (PROPOSE_PROMPT if args.phase == "propose" else PROMPT).format(task=task)
        command = [
            "bash", "-c", RELAY_SCRIPT, "relay",
            "/opt/omp/omp", "-p", "--mode", "json",
            "--config", "/opt/omp/config.yml",
            "--model", f"llama.cpp/{model}",
            "--approval-mode", "yolo",
            "--no-session", "--offline", "--no-title", "--no-extensions", "--no-skills", "--no-lsp",
            *phase_args,
            prompt,
        ]
        exit_code = run_sandboxed(command, work, omp, socket_dir, args.timeout, log, config, progress.line)
    finally:
        if relay is not None:
            relay.terminate()
            relay.wait()
        shutil.rmtree(socket_dir, ignore_errors=True)
        progress.write_output()
    seconds = round(time.time() - started, 1)

    patch, files, symlinks = collect_patch(run)

    check = None
    if args.check and args.phase == "implement" and exit_code == 0 and files:
        check_log = run / "check.log"
        progress.emit("check_start", command=clean(args.check))
        check_exit = run_sandboxed(["bash", "-c", args.check], work, omp, None, args.check_timeout, check_log)
        check = {"passed": check_exit == 0, "exit_code": check_exit, "output": tail(check_log)}
        progress.emit("check_end", passed=check["passed"], exit_code=check_exit)

    if exit_code is None:
        status = "timeout"
    elif exit_code != 0:
        status = "executor_failed"
    elif args.phase == "propose":
        status = "unexpected_changes" if files else "proposed" if progress.final.strip() else "no_approach"
    elif not files:
        status = "no_changes"
    elif check is not None and not check["passed"]:
        status = "check_failed"
    else:
        status = "ok"

    progress.emit("end", status=status, seconds=seconds, files=files, check=check["passed"] if check else None)
    progress.close()

    make_writable(work)
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(run / "shadow.git", ignore_errors=True)
    (run / "omp-config.yml").unlink(missing_ok=True)

    print(json.dumps({
        "status": status,
        "phase": args.phase,
        "approach": progress.final[:12000] if args.phase == "propose" else None,
        "model": model,
        "seconds": seconds,
        "executor_exit_code": exit_code,
        "files_changed": files,
        "symlinks_changed": symlinks,
        "patch": str(patch),
        "log": str(log),
        "progress": str(progress.path),
        "check": check,
        "executor_output_tail": tail(log),
    }, indent=2))
    return 0 if status in ("ok", "proposed") else 2


if __name__ == "__main__":
    sys.exit(main())
