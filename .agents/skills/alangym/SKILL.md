---
name: alangym-repair
description: Solve one verified regression task using the same small, testable practices.
---

# Working a regression task

- Read the relevant source and regression tests before editing.
- Make the smallest source change that fixes the reported behavior.
- Preserve the repository's surrounding style and public interfaces.
- Do not edit tests, project configuration, or files outside the task's allowed source list.
- Run the frozen regression command if it is available in the environment.
- Report changed source files and the actual test result. Never claim tests passed unless they ran.
