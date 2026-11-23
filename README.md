# plan-omp-portable

Plan a coding task with the harness's own model, implement it with a local model
through `omp` inside a `bwrap` sandbox, then review the patch before applying it.

One built copy serves omp, Codex, OpenCode and Cursor. Antigravity needs its own
folder. Marshall has no skill loader and follows the same instructions from a
prompt.

## Layout

```
src/SKILL.md                 the Claude Code skill, with ${CLAUDE_SKILL_DIR}
src/scripts/omp_exec.py      sandboxed executor, unchanged from upstream
src/scripts/omp_watch.py     live progress, unchanged from upstream
src/advisor.md               read-only reviewer instructions
install_plan_omp.sh          rewrites the copy for a non-Claude harness
```

The built copy is **not** in this repository, and it must not be: the installer
bakes absolute paths into `SKILL.md`, so a copy built on one machine breaks on
any machine with a different `$HOME`. Ship this source and rebuild there.

## Requirements

Linux (the sandbox is `bwrap`), Python ≥ 3.12, `omp`, `bwrap`, `socat`, `git`,
and a local OpenAI-compatible model server. The executor expects it at
`http://127.0.0.1:8080`; `LOCAL_LLM_URL` overrides that. Planning and review use
the harness's own model, so a cloud harness spends its own tokens on those passes.

## Install

```bash
git clone https://github.com/Zagitta/plan-omp-portable.git
cd plan-omp-portable
./install_plan_omp.sh                       # → ~/.agents/skills/plan-omp-portable
./install_plan_omp.sh ~/.gemini/config/skills   # Antigravity, its own folder
```

Then restart the harness. Antigravity also takes `/skills reload` or a new
conversation. On a machine that has no `plan-omp-watch` yet:

```bash
ln -sf ~/.agents/skills/plan-omp-portable/scripts/omp_watch.py ~/.local/bin/plan-omp-watch
```

Re-run the installer after every change to the upstream skill. The portable copy
never updates itself.

## Start it

| Harness | Folder | Start | Advisor kept read-only |
|---|---|---|---|
| omp | `~/.agents/skills` | `/skill:plan-omp-portable <goal>` | not enforced |
| Codex | `~/.agents/skills` | `$plan-omp-portable <goal>` | instructions only, no separate sandbox |
| OpenCode | `~/.agents/skills` | `@plan-omp-portable <goal>` | deny edit and shell in a subagent |
| Cursor | `~/.agents/skills` | `/plan-omp-portable <goal>` | `readonly: true` in a subagent |
| Antigravity | `~/.gemini/config/skills` | `/plan-omp-portable <goal>` | list only read tools in `tools` |
| Marshall | none, no skill loader | prompt, see below | not enforced |

Only omp is verified on the machine that built this guide; the other rows come
from each product's own docs. Marshall:

```
Read ~/.agents/skills/plan-omp-portable/SKILL.md and follow it. Goal: <your task>
```

Gemini CLI also reads `~/.agents/skills`, but Antigravity CLI replaced it on
June 18, 2026.

## Test the executor

Throwaway repository, no files touched — the executor only writes a patch.
Passed in about 16 seconds with the local model.

```bash
R=$(mktemp -d)
git -C "$R" init -q
printf 'def greet(name):\n    return "hello " + name\n' > "$R/hello.py"
git -C "$R" add hello.py
git -C "$R" -c user.name=smoke -c user.email=smoke@example.invalid commit -qm init
printf '# Step: add a farewell function\nAdd farewell(name) to hello.py. It returns "goodbye " + name. Change no other file.\n' > "$R.step.md"
python3 -I ~/.agents/skills/plan-omp-portable/scripts/omp_exec.py \
  --repo "$R" --task-file "$R.step.md" \
  --check 'python3 -c "import hello; assert hello.farewell(\"a\") == \"goodbye a\""'
```

A working install prints `"status": "ok"`, lists `hello.py` under
`files_changed`, and shows `"passed": true`. Then open that repository in the
harness and start the skill with the same goal, and confirm the harness runs the
executor, reads the patch, and applies it only after reviewing it.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| The skill does not appear | harness started before the install, or scans another folder | restart it, or check its folder in the table above; in omp run `omp skill list` |
| `SKILL.md has changed, cannot find` | upstream skill no longer matches the rewrite contract | update that line in `install_plan_omp.sh`, then run it again |
| Required tool is missing | `bwrap`, `socat` or `git` absent | install it with your system package manager |
| `omp_watch.py` SyntaxError | Python older than 3.12 | run it with Python 3.12 or newer |
| Executor cannot reach the model | server stopped or on another port | start it, or set `LOCAL_LLM_URL` |
| Check cannot find Java, Maven or `node_modules` | sandbox sees only the repository and `/usr` | omit `--check` and run the saved verify command after applying the patch |
| The advisor changed a file | harness cannot enforce read-only tools | run `git -C <repo> status` after each advisor call and discard what it changed |
