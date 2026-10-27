# AlanGym

AlanGym measures whether a focused Alan skill helps a coding agent fix real
bugs from merged pull requests. Each task is checked against the repository's
own regression tests. A skill change is kept only when it improves the
training score; held-out tasks are measured at the beginning and end.

## What runs where

```text
public GitHub PRs
      │
      ▼
verified parent + regression tests ──► Alan Code / DeepSeek V4.1 Flash
                                                │
                                                ▼
                                         repository pytest
                                                │
                                  training failures only
                                                ▼
                                         Alan skill edit
                                                │
                          training score improves? ──► keep and commit
                                                │
                                                ▼
                                   final held-out score
```

AlanGym runs the task miner, runner, grader, and skill evolver locally. Alan
Code runs each model session. The skill can be imported into Alan from this
repository after a run; skill import and any future PR-merged automation remain
manual Alan steps.

## Demo benchmark

The supported demo repository is `mahmoud/boltons`. AlanGym reads public
repository history through the authenticated GitHub CLI (`gh`), checks each
candidate's first parent and complete changed-file list, and admits small PRs
that change Python source and tests. It builds a frozen pytest environment,
then checks that each proposed test fails on the PR parent with its test changes
overlaid and passes on the merged commit. An import error for a missing symbol
from the changed module counts as a pre-fix regression; unrelated collection
errors do not.

The benchmark requires exactly 14 verified tasks: 6 training and 8 held out.
Four held-out tasks are retained from the previous completed run as a stable
validation cohort; four additional held-out tasks are mined fresh. The fresh
cohort is split into 6 training tasks and 4 new held-out tasks. The default
split seed is 42. AlanGym verifies retained PRs by commit identity and blocks
instead of relabeling tasks when it cannot recover the exact cohort.

The tests can reveal the shape of a fix. That is part of this benchmark design:
the source change is removed while the PR's regression tests remain.

The previous completed 10-task run had 6 training and 4 held-out tasks.
Baseline training passed 4/6; the round-two current score was 5/6. Held-out
results were 4/4 at baseline and 3/4 at the end. Both proposed skill candidates
were rejected, so the initial skill remained in use. These results do not
establish that a skill change caused the score differences. The 14-task
benchmark and a real model ladder have not yet been run, and costs are
unverified.

## Requirements

- Windows 10 or 11 with Python 3.13 and `uv`
- Node.js 22
- GitHub CLI (`gh`) authenticated to GitHub
- Alan Code 0.1.6 and an Alan account that can use DeepSeek V4.1 Flash and V4 Pro

Install the pinned Alan CLI and complete its one-time device login:

```powershell
npm install --global alan-code@0.1.6
alan --login
alan --list-models
```

The experiment pins its repair model to `deepseek/deepseek-v4.1-flash`. The
optional ladder compares Flash without a skill, Flash with the current skill,
and V4 Pro without a skill on every held-out task. It uses the configured Alan
models and existing credits; AlanGym never buys or adds credits. Token counts
appear when Alan's local session database provides them. Dollar costs remain
hidden unless billing is verified. AlanGym does not enforce a dollar spending
cap; the session time limit is not a spending limit.

## Run locally

Install the application dependencies:

```powershell
uv sync --locked --extra dev
cd web
npm ci
cd ..
```

Start the local API in one terminal:

```powershell
uv run alangym serve
```

Start the dashboard in another:

```powershell
cd web
npm run dev
```

Open the Vite address shown in the terminal, normally
`http://127.0.0.1:5173`. Start a run in the dashboard, or use the CLI:

```powershell
uv run alangym run --repo mahmoud/boltons
uv run alangym status RUN_ID
uv run alangym cancel RUN_ID
```

The local API binds to `127.0.0.1:8765`. Run data, cloned repositories,
temporary workspaces, and the per-run dependency lock stay under `.alangym/`,
which Git ignores.

## Experiment rules

- Every repair, ladder trial, and skill proposal starts a fresh Alan Code
  print-mode session. Repair sessions use Flash; the optional ladder uses its
  three pinned configurations.
- Repair and ladder sessions have a 10-minute limit. Frozen pytest commands
  have a 60-second grading limit.
- Alan can change only the PR's source files. AlanGym restores other workspace
  changes before grading and records a policy violation as a failed trial.
- The evolver receives only training-task failures. A candidate is kept only
  when it improves training without regressing any prior pass. A regression
  triggers one retry; the candidate is still rejected and inconsistent retries
  are marked flaky.
- Held-out tasks are scored at baseline and after the last round. They are not
  sent to the evolver. The model ladder runs only when started from the local
  evidence panel.
- A completed run saves a scrubbed replay under `.alangym/exports/` for review.
  Export it again after saved ladder trials to include those trials. The replay keeps
  grader output, source diffs, token counts, and safe action labels. It omits
  raw Alan transcripts, prompts, tool arguments, and local file paths. Raw
  session histories stay in the local database.

## Evidence views and limits

The dashboard includes a task scrubber, pass/fail heatmap, skill lineage,
version-level PR provenance, model ladder, task-level X-ray fallback, and a
controlled Flash battle replay when both skill configurations have saved data.
The fallback compares complete task outcomes and does not claim that one skill
line caused a specific agent decision. Battle replay shows action labels only.
Line-level counterfactual probes, skill compilation, adversarial twins, and
exact conversation/workspace time-travel forks remain unavailable until their
required evidence and restoration data exist.

Boltons' `conftest.py` uses a pytest hook signature removed in pytest 9. For
that demo repository, AlanGym applies a local pytest 8 compatibility pin to
its disposable clone and saves the generated `pyproject.toml` and `uv.lock`
with the run. The task snapshots still use the repository's original project
files.

AlanGym currently runs natively on Windows because this machine has no
container or WSL sandbox. Its subprocess environments omit common API and Git
credentials, and task workspaces are temporary. This does not provide an OS
security boundary: model tools and repository tests execute as the current
Windows user, so use repositories you trust.

## Import the skill into Alan

After reviewing the measured version, open Alan's Skills area and choose
Import, then From GitHub. Paste the link to
`PRADDZY/AlanGym/blob/main/.agents/skills/alangym/SKILL.md`, review the imported
skill, and add it to the team. AlanGym does not claim that it created an Alan
skill version or configured an automation; those actions happen in Alan.

## Replay site

The public replay is deployed to Cloudflare Pages at
https://alangym-13q.pages.dev. It displays the saved replay and evidence report;
starting or canceling live model runs requires the local AlanGym API.

To review a completed run and include any saved ladder trials, export it from
the local database and inspect the JSON before publishing. The default output is
`.alangym/exports/RUN_ID.json`; use `--output PATH` to choose another file.
Export does not run models, commit, push, or deploy.

```powershell
uv run alangym export RUN_ID
Get-Content .alangym/exports/RUN_ID.json
# After review, copy the approved replay into the site:
Copy-Item .alangym/exports/RUN_ID.json web/public/data/demo-run.json
```

To build and publish the production site:

```powershell
cd web
npm run build:cloudflare
npx wrangler pages deploy dist --project-name=alangym --branch=main
```

## Development checks

```powershell
New-Item -ItemType Directory -Force .alangym | Out-Null
uv run pytest --basetemp .alangym/pytest
cd web
npm run build
```

Benchmark scores represent tasks Alan actually ran and the repository tests
graded; demo data does not establish results for an unrun benchmark.

## License

AlanGym is MIT-licensed. Mined task source remains under the source
repository's license and is fetched on demand rather than copied into this
repository.
