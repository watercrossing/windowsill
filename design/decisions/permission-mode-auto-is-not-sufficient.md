# `--permission-mode auto` is not enough for an unattended writer

**Settled:** 2026-07-28

## Context

Same sitting as [permission-denials-are-counted.md](permission-denials-are-counted.md): three jobs on different models, all under `--permission-mode auto`.

## Decision

**It is not enough, and the outcome is not even consistent across models.**
Sonnet's writes went through in the same directory where Haiku's were refused.
Don't take one job's success as proof that the mode is sufficient; `auto` is a coin toss.

## Consequence

At the time, the conclusion was that the configured allowlist (`safety.allowed_tools`) was the mechanism.
Three days later that turned out to be wrong: `--allowed-tools` pre-approves and does not bound anything ([allowed-tools-is-additive.md](allowed-tools-is-additive.md)).
The settled position is that the job's own `--permission-mode` is the boundary ([permission-mode-is-the-boundary.md](permission-mode-is-the-boundary.md)).
A job that writes unattended should choose that mode deliberately and not rely on `auto`.
