# ccq — a local batch queue for Claude Code

**One-liner:** SGE-style job files that queue prompts and run them unattended in 5-hour windows you would otherwise sleep through.

---

The full design is split into one file per section in this directory.
Section numbers are stable: a reference to *§N* lives in `NN-<slug>.md`
(e.g. §4 → [04-usage-oracle.md](04-usage-oracle.md)).

## Contents

- [§0. Instructions for the implementing agent](00-instructions.md)
- [§1. Motivation and objective](01-motivation.md)
- [§2. The job file](02-job-file.md)
- [§3. Execution model](03-execution-model.md)
- [§4. `/usage` as the window oracle](04-usage-oracle.md)
- [§5. Windows, anchors, accounting, limits](05-windows.md)
- [§6. State: SQLite](06-state.md)
- [§7. Job lifecycle](07-lifecycle.md)
- [§8. Interface](08-interface.md)
- [§9. Findings and open questions](09-findings.md)
- [§10. Milestones](10-milestones.md)
- [§11. Acceptance criteria](11-acceptance.md)
- [§12. Prior art](12-prior-art.md)
