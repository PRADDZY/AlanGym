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

The benchmark requires exactly 10 verified tasks: 6 training and 4 held out.
The default split seed is 42. If the miner cannot verify all ten tasks, it
blocks the run instead of reducing the sample.

The tests can reveal the shape of a fix. That is part of this benchmark design:
the source change is removed while the PR's regression tests remain.

## Requirements

- Windows 10 or 11 with Python 3.13 and `uv`
- Node.js 22
- GitHub CLI (`gh`) authenticated to GitHub
- Alan Code 0.1.6 and an Alan account that can use DeepSeek V4.1 Flash

Install the pinned Alan CLI and complete its one-time device login:

```powershell
npm install --global alan-code@0.1.6
alan --login
alan --list-models
```

AlanGym pins the model to `deepseek/deepseek-v4.1-flash`. It makes no separate
model API calls and does not buy or add credits. The current CLI does not expose
per-session dollar costs, so costs remain `null` in saved results.

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

- Every task, evolver proposal, and trial starts a fresh Alan Code print-mode
  session using the pinned model.
- A repair session has a 3-minute limit. Its frozen pytest command has a
  60-second grading limit.
- Alan can change only the PR's source files. AlanGym restores other workspace
  changes before grading and records a policy violation as a failed trial.
- The evolver receives only training-task failures. A candidate is kept only
  when it raises the current training pass count. Each kept skill version is
  committed and pushed.
- Held-out tasks are scored at baseline and after the last round. They are not
  sent to the evolver.
- A completed run writes a scrubbed, read-only replay to
  `web/public/data/demo-run.json`, commits it, and pushes it. The replay keeps
  grader output and source diffs; it omits raw Alan transcripts and local file
  paths.

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

The `Publish AlanGym replay` GitHub Actions workflow builds the dashboard and
publishes it to GitHub Pages when `main` changes. Pages is read-only: it shows
the saved JSON replay and cannot start a model run. The live dashboard and its
start/cancel controls are local.

## Development checks

```powershell
New-Item -ItemType Directory -Force .alangym | Out-Null
uv run pytest --basetemp .alangym/pytest
cd web
npm run build
```

No benchmark scores are presented until Alan has actually run the tasks and
the repository tests have graded the results.

## License

AlanGym is MIT-licensed. Mined task source remains under the source
repository's license and is fetched on demand rather than copied into this
repository.
