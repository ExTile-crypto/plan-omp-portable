#!/usr/bin/env bash
# Build the portable copy of the plan-omp skill.
#
#   install_plan_omp.sh [dest_root]      default: $HOME/.agents/skills
#
# Reads src/SKILL.md, src/planning-files.md, src/scripts/*, src/advisor.md from this repository and
# writes into <dest_root>/plan-omp-portable, plus the advisor agent when omp is
# installed. Stops without writing if
# src/SKILL.md no longer matches the rewrite contract below.
set -eu

here="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
src="$here/src"
root="${1:-$HOME/.agents/skills}"
dest="$root/plan-omp-portable"

mkdir -p "$dest/scripts" "$dest/references" "$dest/agents"
cp "$src/scripts/omp_exec.py" "$src/scripts/omp_watch.py" "$dest/scripts/"
chmod +x "$dest/scripts/omp_exec.py" "$dest/scripts/omp_watch.py"
printf 'policy:\n  allow_implicit_invocation: false\n' > "$dest/agents/openai.yaml"

python3 -I - "$src/SKILL.md" "$src/advisor.md" "$dest" <<'PY'
import re
import sys
from pathlib import Path

skill, advisor, dest = map(Path, sys.argv[1:])
text = skill.read_text()
swaps = [
    ("name: plan-omp\n", "name: plan-omp-portable\n"),
    ("argument-hint: <goal>\n", ""),
    ("Goal: $ARGUMENTS", "Goal: the task the user gave when starting this skill."),
    ("# Plan with Claude, implement with local omp, review with Claude", "# Plan here, implement with local omp, review here"),
    ("Claude requests stay in this Claude Code session. Never configure or ask omp to call Claude.", "Your own requests stay in this session. Never configure or ask the sandboxed executor to call a cloud model."),
    ("`.claude`, or `.omp` files", "`.claude`, `.codex`, `.agents`, `.cursor`, `.opencode`, `.gemini`, or `.omp` files"),
    ("${CLAUDE_SKILL_DIR}", str(dest)),
]
for old, new in swaps:
    if old not in text:
        sys.exit(f"SKILL.md has changed, cannot find: {old[:50]}")
    text = text.replace(old, new)
planning = (advisor.parent / "planning-files.md").read_text()
if text.count("## 1. Plan\n") != 1:
    sys.exit("SKILL.md has changed, cannot place the planning files section")
text = text.replace("## 1. Plan\n", planning + "## 1. Plan\n")
text, described = re.subn(
    r"^description: .*$",
    "description: Plan a coding task, check risky steps with a read-only reviewer, "
    "implement on the local model through omp in a sandbox, then review and apply the patches.",
    text, count=1, flags=re.M,
)
review = (
    "The advisor is a separate read-only reviewer. In omp, always use the `plan-omp-advisor` task agent, "
    "and never use `scout`, `task`, or another local agent for this check. If that agent is unavailable in omp, "
    "do the check yourself and tell the user. In other harnesses, if subagents exist, start one, "
    f"tell it to read `{dest}/references/advisor.md` and follow it, and limit it to read and search tools. "
    "If the harness cannot restrict its tools, tell it not to edit files or run commands that change anything. "
    "If the harness has no subagents, do the check yourself in a separate pass using those instructions, "
    "and tell the user that no independent advisor ran. Never give the advisor's job to the local executor or a local model."
)
text, advised = re.subn(r"^The advisor is the `plan-omp-advisor`.*$", lambda _: review, text, count=1, flags=re.M)
if not described or not advised:
    sys.exit("SKILL.md has changed, cannot rewrite the description or advisor rules")
(dest / "SKILL.md").write_text(text)
body = re.sub(r"^---\n.*?\n---\n+", "", advisor.read_text(), count=1, flags=re.S)
(dest / "references" / "advisor.md").write_text(body)
PY

omp_dir="${PI_CODING_AGENT_DIR:-$HOME/.omp/agent}"
if [ -d "$omp_dir" ]; then
  mkdir -p "$omp_dir/agents"
  cp "$src/omp/plan-omp-advisor.md" "$omp_dir/agents/plan-omp-advisor.md"
  echo "Installed omp advisor: $omp_dir/agents/plan-omp-advisor.md"
fi

[ -s "$dest/SKILL.md" ] && echo "Installed: $dest"
