# AlanGym

AlanGym measures whether a focused Alan skill helps an agent fix real, tested
bugs from merged pull requests. It mines regression tasks, runs a fresh Alan
Code session for each one, checks the result against the repository's tests,
and evolves the skill using training failures.

**Status:** early build for the OpenFire hackathon. The first tested runner is
planned for Python/pytest repositories; its demo corpus is `mahmoud/boltons`.

## Why

Teams want their coding agents to follow useful repository practices. Those
practices are easy to edit and hard to measure. AlanGym turns small real fixes
into repeatable evaluations and compares a skill against its own baseline.
Held-out tasks remain separate from skill evolution.

## How it works

```text
merged PRs → verified regression tasks → fresh Alan Code sessions
                                     ↓
             isolated pytest grading ← skill evolution on training failures
                                     ↓
                     run history and public replay
```

The planned runner uses Alan's `alan-code` CLI with DeepSeek V4.1 Flash. AlanGym
does not claim that Alan runs its grading or evolution loop. Each result will
identify the selected model and show its task, test outcome, and skill version.

## Current state

The architecture and implementation are being built in public. The README will
be updated with installation instructions, reproduced experiment results, and
the exact manual Alan import steps as those become available.

## License

AlanGym is MIT-licensed. Any task source distributed with this project retains
its original repository's license and attribution.
