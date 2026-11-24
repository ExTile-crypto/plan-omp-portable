# Claude Code and CC GUI guide

This guide sets up the lean Claude Code launcher, its CC GUI mode, and the
Implementer panel for IntelliJ.

The lean launcher starts Claude Code with only the instructions and tools the
plan flow needs. In testing with Claude Code 2.1.331, a session started at about
**11.7K tokens instead of 35.4K**.

## How the flow works

1. **Plan (Claude):** reads the code, splits the goal into steps, and marks each
   step risky or routine. For a risky step, it writes the approach into the step.
2. **Implement (local model):** omp changes a copy of the repository in a sandbox
   and returns a patch.
3. **Patch check (Claude advisor, risky steps only):** a read-only subagent on
   its own model looks for bugs.
4. **Review (Claude):** reads the patch, fixes small problems, applies it, and
   runs the checks.

Only one step runs at a time, and each patch is applied before the next step
starts. A patch with looping repeated text gets the status `repeated_text`.

## Requirements

- Linux, Python 3.12 or newer, `git`, `bwrap`, `socat`, and `omp`
- Claude Code, signed in
- A local OpenAI-compatible model server at `http://127.0.0.1:8080`
- For the panel: IntelliJ with its embedded browser (JCEF). Tested on 2026.2.

## Install

```bash
git clone https://github.com/ExTile-crypto/plan-omp-portable.git
cd plan-omp-portable
./install_lean.sh
```

This installs to `~/plan-omp-lean` and links three commands in `~/.local/bin`:
`plan-omp-lean`, `plan-omp-lean-cc`, and `plan-omp-lean-watch`. Re-running it
keeps your saved plans.

Don't install it under `~/.local` or `~/.config`. CC GUI blocks tool calls that
mention those folders, so the executor and plan files would stop working.

## Use it in a terminal

Run it from inside the project:

```bash
plan-omp-lean "your goal"
```

It uses Opus to plan and Sonnet as the advisor. It asks before shell commands and
edits like normal Claude Code. To follow the local model from a second terminal:

```bash
plan-omp-lean-watch --repo .
```

## Use it in CC GUI

> CC GUI is a third-party plugin. It runs the official Claude Code program with
> your login, but Anthropic restricts third-party products from using subscription
> logins. Check Anthropic's current terms before using a subscription this way.
> The terminal launcher above avoids the question.

1. Enable lean mode for the project:

   ```bash
   cd /path/to/project
   plan-omp-lean --gui on
   ```

2. In CC GUI, open **Settings > Basic** and set the custom Claude CLI path to the
   full path this prints:

   ```bash
   echo "$HOME/.local/bin/plan-omp-lean-cc"
   ```

3. Restart IntelliJ, open a new CC GUI chat, and select **Opus**.
4. Send your goal as a normal message, with no slash command.

Only enabled projects change. Other projects, and CC GUI's commit-message and
rewind features, go to Claude Code unchanged. While a project is enabled, every
new CC GUI chat there uses the plan flow, and plain questions are answered without
starting a plan.

To check that a running chat is using it:

```bash
pgrep -fc -- '--strict-mcp-config.*plan-omp-lean'
```

A number above 0 means a lean session is running. To turn it off for a project:

```bash
plan-omp-lean --gui off
```

## Continue an existing omp plan

Stop the omp chat first and make sure its current step has finished and been
applied. The first lean launch or `--gui on` copies `.git/plan-omp` once. After
that, the two plans no longer sync, so don't run the same plan in both.

Then send:

```text
Continue the existing plan from run.md. Check which steps are really applied, then run the remaining steps one at a time.
```

## Implementer panel

The panel shows the local model's live thinking and tool calls in an IntelliJ tab.
It reads local run files only and uses no Claude tokens.

Build it against your IntelliJ installation:

```bash
IDE=/path/to/idea ./plugin/build.sh
```

Then open **Settings > Plugins**, click the gear icon, choose **Install Plugin from
Disk**, select `plugin/dist/plan-omp-implementer-0.1.1.zip`, and restart IntelliJ.
Rebuild it after an IntelliJ major version update.

The **Implementer** panel sits on the right, under CC GUI, and is set to open when
a new step starts. It shows:

- thinking as it streams, collapsed once the model moves on
- grouped tool cards with status dots, and each tool's input and result on click
- elapsed time, tokens, speed, context, turns, errors, and changed files

Tool details are stored in `~/.cache/omp-exec/runs/<run>/` with owner-only
permissions. They can contain code the model read from your project.

## Where files live

| Path | Contents |
|---|---|
| `~/plan-omp-lean/` | launcher, system prompt, executor, and watcher |
| `~/plan-omp-lean/state/<project>-<id>/` | `run.md`, `brief.md`, `map.md`, steps, patches, and `verify.sh` |
| `~/.cache/omp-exec/runs/<run>/` | live progress, tool details, and the step's patch |

To uninstall, turn off lean mode, clear the CC GUI path, remove the plugin, then
delete `~/plan-omp-lean` and the three links in `~/.local/bin`. This also deletes
saved lean plans.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `Access to a sensitive path is not allowed` | files are under a folder CC GUI blocks | reinstall to the default `~/plan-omp-lean` |
| CC GUI still starts at about 35K | project not enabled, path not set, or chat opened earlier | run `plan-omp-lean --gui status`, check the path, and open a new chat |
| A tool row shows only its target | the step started before tool details were recorded | run a new step |
| No thinking appears | the step started before this version, or the model sent none | run a new step |
| The panel doesn't open | no step has started since IntelliJ opened | open **Implementer** from the right tool bar |
| The plugin is incompatible | IntelliJ's major version changed | rebuild it with `plugin/build.sh` |
| `No javac` during the build | the IDE's runtime has no compiler | set `JDK` to a JDK matching that runtime |
| Status `repeated_text` | the local model looped while writing | fix the listed lines, or retry with the exact text in the step |
| `verify.sh` runs in the wrong folder | it was copied from an older plan | make its `cd` use the project root |

## What has been tested

The launcher, CC GUI wrapper, plan-file copy, executor, and panel page have
automated or scripted tests. The panel has been installed and opened in IntelliJ
2026.2. Its automatic opening and full runs on other machines have not been
verified.
