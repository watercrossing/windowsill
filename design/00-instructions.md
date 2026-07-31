# 0. Instructions for the implementing agent

You are building this project. Read this section fully.

## 0.1 Why you are being told not to read it

Context length is the cost driver. Measured on the target account: one large prompt consumed **15% of a 5-hour window**, with `61% of your usage was at >150k context`. This is a subscription; the constraint is window percentage, not dollars. Every token you hold in the orchestrator's context is paid for again on every subsequent turn of that conversation.

So: **you orchestrate, subagents implement.** Your context should hold this section, the work breakdown, and test results. Nothing else.

## 0.2 Subagent protocol

- **Delegate by file, never by summary.** A subagent brief reads: *"Read [`04-usage-oracle.md`](04-usage-oracle.md). Implement `src/windowsill/usage.py`, tests first. Do not read other sections."* It must **never** read: *"Here's what [§4](04-usage-oracle.md) says: the usage parser should..."*
- This document is precise where it is precise on purpose. "The reset clause is the discriminator, not the percentage, not the line" survives verbatim reading and dies in paraphrase. **Paraphrasing the spec into a brief is the single most likely way to build the wrong thing.** If you find yourself explaining a section to a subagent, stop and give it the file name instead.
- **Use Sonnet for implementation subagents.** Opus for integration, design judgement, and anything where [§0.4](00-instructions.md)'s tensions apply. Most of this codebase is unremarkable Python and does not need Opus.
- **Subagents report file paths and test results, not code.** Never let a subagent paste an implementation back into your context. You read the diff only if a test fails twice.
- **One module per subagent.** The [§0.5](00-instructions.md) breakdown is designed so that modules and spec sections align, and a subagent needs exactly one file.
- Subagents cannot ask clarifying questions. If a brief is ambiguous, resolve it *before* dispatching, using [§0.4](00-instructions.md).

## 0.3 Method

**Test-driven, genuinely.** Tests first, and they are not ceremony here: [§9](09-findings.md) records four findings that were established by experiment, three of which contradicted a plausible assumption. This spec has already been wrong once about `/usage` output in a way that only a fixture would catch.

- Every parser gets fixture-based tests before it gets an implementation. Fixtures for `/usage` output are in [§4](04-usage-oracle.md) verbatim — **both the open and closed forms**. A parser tested only against live output will only ever meet the open branch, which is the branch that works.
- No network, no `claude` invocation, and no real clock in the test suite. The runner takes an injectable command; the scheduler takes an injectable `now`.
- `pytest`. Coverage is not a target; the fixtures in [§2](02-job-file.md), [§4](04-usage-oracle.md) and [§7](07-lifecycle.md) are.

**Commit as you go.** Each module lands as its own commit with its tests passing. This project's own subject matter is unattended overnight execution — you will want a clean history when you start letting it run itself.

## 0.4 Standing tensions

You will hit these. They are decided; do not relitigate:

- **Pass-through beats validation** ([§2.2](02-job-file.md)). When tempted to validate a directive, don't.
- **Window count has a ceiling** ([§1](01-motivation.md), [§5.3](05-windows.md)). When tempted to harvest more windows, don't.
- **Scoping beats resuming** ([§5.4](05-windows.md)). Resume-on-limit is a fallback for jobs that overrun, not a strategy for long work. Context is the cost.
- **The oracle is authoritative** ([§4](04-usage-oracle.md)). [§4](04-usage-oracle.md) and [§9](09-findings.md) record behaviour observed on the target machine on 2026-07-17. Where they conflict with your training data about how `/usage` or Claude Code behaves, **[§4](04-usage-oracle.md) and [§9](09-findings.md) win.** Do not "correct" them.

## 0.5 Work breakdown

Modules map 1:1 onto spec sections. Dispatch one subagent per row.

| Module | Reads | Milestone |
|---|---|---|
| `windowsill/jobfile.py` — directive parser, pass-through split | [§2](02-job-file.md) | M1 |
| `windowsill/db.py` — schema, migrations, `BEGIN IMMEDIATE` | [§6](06-state.md) | M1 |
| `windowsill/config.py` — TOML load, defaults | [§8.3](08-interface.md) | M1 |
| `windowsill/runner.py` — invoke, spawn detached, reap, timeout | [§3.2](03-execution-model.md), [§3.3](03-execution-model.md) | M1 |
| `windowsill/scheduler.py` — `tick`, dispatch, chain | [§3.1](03-execution-model.md), [§7](07-lifecycle.md) | M1 |
| `windowsill/cli.py` — click commands | [§8.1](08-interface.md) | M1 |
| `windowsill/usage.py` — `/usage` oracle, probe rule | [§4](04-usage-oracle.md) | M2 |
| `windowsill/window.py` — policies, anchors, accounting | [§5](05-windows.md) | M2/M3 |
| `windowsill/notify.py` — ntfy, sendmail | [§8.4](08-interface.md) | M2 |
| `README.md` | [§0.6](00-instructions.md), [§1](01-motivation.md), [§2](02-job-file.md), [§8.1](08-interface.md) | M1 |

Build M1 end-to-end and *use it for a night* before starting M2. [§9](09-findings.md)'s remaining open question — the rate-limit signal shape — is only answerable by observing a real limit hit, and M1 running real jobs is what produces one.

## 0.6 Deliverable shape

A proper installable package. Not a single file.

```
windowsill/
├── pyproject.toml          # hatchling; requires-python = ">=3.13"
├── README.md               # install, job file format, the 3 commands that matter
├── design/                 # this design, one file per section
├── src/windowsill/
│   ├── __init__.py, cli.py, config.py, db.py, jobfile.py,
│   ├── notify.py, runner.py, scheduler.py, usage.py, window.py
└── tests/
    ├── fixtures/usage_open.txt, usage_closed.txt, jobs/*.md
    └── test_*.py
```

- Dependencies: `click`, `sqlite-utils`. Dev: `pytest`. Nothing else unless it saves a tone of work — stdlib covers `tomllib`, `zoneinfo`, `subprocess`, `shlex`, `signal`, `smtplib`.
- Entry point: `[project.scripts] windowsill = "windowsill.cli:main"`.
- Install: `uv tool install .` — or `uvx --from . windowsill` to try it without installing.
- The `windowsill` on `PATH` must work from cron with a minimal environment ([§9](09-findings.md) confirms `claude` itself does).
