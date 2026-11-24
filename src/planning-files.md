## Planning files

Keep these files in `$(git -C "<repo>" rev-parse --absolute-git-dir)/plan-omp/`. They stay with this clone and never enter a patch or commit. Create the folder when needed. Never store secrets in them.

Before planning, read `run.md`, `brief.md`, and `map.md` if they exist. They are hints, so the current code and the user's request win. Correct anything you find to be wrong.

**`run.md` is the checkpoint.** Record the goal, starting `HEAD`, each step with its risk and status (`pending`, `approved`, `patched`, `applied`, or `failed`), its step file, its patch path, and decisions that affect later steps. Keep step files in `plan-omp/steps/`. Update `run.md` after every status change.

- If `run.md` is unfinished and has the same goal, resume at the first step that is not `applied`. Before trusting `applied`, confirm the patch is present with `git -C "<repo>" apply --reverse --check "<patch>"`. If it is not, treat the step as not applied.
- Never apply a saved patch without reviewing it in this session under section 3.
- If `run.md` is unfinished and has a different goal, ask the user before replacing it. If it is finished, rename it to `run.previous.md` and start a new one.
- Mark the run `finished` after the final review.
- If this session's context passes about 100K tokens between steps, tell the user once that they can start a new chat with the same goal to resume from `run.md`, then continue.

**`brief.md` is the executor's standing brief.** Keep it under 40 lines: stable project rules, conventions, and things that must not change. Add only rules confirmed by the code or the user. The executor receives this file automatically on every step, so do not repeat its rules in step files. Each step still needs its own files, behavior, constraints, and verification.

**`map.md` is the code map.** Keep it under 40 lines of pointers: feature or flow, file path, symbol, and a few words on its role. Do not include code or behavior summaries. Use it to choose what to search, and confirm each pointer before relying on it. Add a pointer only after you have used it.

