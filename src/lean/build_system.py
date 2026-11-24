#!/usr/bin/env python3
"""Build the lean Claude Code system prompt and advisor from the skill source."""
import re
import sys
from pathlib import Path

HEADER = """You are Claude Code running the plan-omp flow. You plan and review. A local model writes the code through omp in a sandbox.

Goal: the user's first message. If it asks to continue, resume from `run.md`. If it is only a question, answer it without starting a plan.

Spend your effort on a precise plan and a careful review, and leave routine editing to the executor. Your own requests stay in this session. Never configure or ask the sandboxed executor to call a cloud model.

Use the dedicated file tools when they fit. Keep updates short and report outcomes faithfully, including anything unverified. Confirm before destructive or outward-facing actions. Do not print secrets. Do not commit or push unless the user asked.

"""
ADVISOR = "The advisor is the `plan-omp-advisor` subagent. Start it with the Agent tool. It is read-only and runs on its own model. Never give its job to the local executor. If it is unavailable, do the check yourself and tell the user that no independent advisor ran."
RULES = """## Keep Claude requests low

Every request sends this whole conversation again, so a few full requests cost far less than many small ones.

- Prefer fewer, larger steps. Combine routine changes into one step when one executor run can handle them, and keep risky changes in their own step.
- Put independent tool calls in one message, such as all planning files and step files.
- The executor's JSON includes `patch_text`. Review the patch there. Read the patch file only if `patch_truncated` is true.
- Accept a step with one command that applies the patch, runs the verify command, and starts the next step's executor only if both succeed. Update `run.md` in the same message.
- Fix problems in one pass: find everything wrong with a patch first, then make all corrections in one message and verify once.
- Finish with one command that runs the full tests and any smoke checks, and update `run.md` in the same message.

"""
GIT = '$(git -C "<repo>" rev-parse --absolute-git-dir)'


def swap(text: str, old: str, new: str) -> str:
    if old not in text:
        raise SystemExit(f"The skill source has changed, cannot find: {old[:60]}")
    return text.replace(old, new)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: build_system.py <src> <dest>")
    src, dest = Path(sys.argv[1]), Path(sys.argv[2])
    skill = (src / "SKILL.md").read_text().split("---\n", 2)[2]
    planning = (src / "planning-files.md").read_text()
    body = HEADER + planning + RULES + skill[skill.index("## 1. Plan\n"):]
    body, count = re.subn(r"^The advisor is the `plan-omp-advisor`.*$", lambda _: ADVISOR, body, flags=re.M)
    if count != 1:
        raise SystemExit("The skill source has changed, cannot rewrite the advisor rule")
    body = swap(body, f"`{GIT}/plan-omp/`", "`{{PLAN_DIR}}/`")
    body = swap(body, "Never store secrets in them.", "Never store secrets in them. Never write inside `.git`. If older notes mention `.git/plan-omp/`, those files were copied to this folder, so use this folder.")
    body = swap(body, "Add only rules confirmed by the code or the user.", "Add only rules confirmed by the code or the user. If `brief.md` exists, read it first and keep every existing rule unless the code or the user proves it wrong.")
    body = swap(body, "`plan-omp/steps/`", "`{{PLAN_DIR}}/steps/`")
    body = swap(body, f"`{GIT}/plan-omp-verify.sh`", "`{{PLAN_DIR}}/verify.sh`")
    body = swap(body, "${CLAUDE_SKILL_DIR}/scripts/omp_exec.py", str(dest / "omp_exec.py"))
    body = swap(body, '--task-file "<step-file>"', '--task-file "<step-file>" --brief-file "{{PLAN_DIR}}/brief.md"')
    if GIT in body or "${CLAUDE_SKILL_DIR}" in body:
        raise SystemExit("The skill source still contains a path this build cannot use")
    (dest / "system.md").write_text(body)
    advisor = re.sub(r"^---\n.*?\n---\n+", "", (src / "advisor.md").read_text(), count=1, flags=re.S)
    (dest / "advisor.md").write_text(advisor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
