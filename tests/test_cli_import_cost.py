"""Regression test for `windowsill.cli`'s import-time budget.

`import windowsill.cli` runs on *every* invocation of the CLI, and will soon
run on every TAB press too: Click's shell completion is a stub that
re-invokes the program per keystroke to list completions, so whatever this
import costs is paid constantly, not once per session. Measured on the
reference machine, a naive `import windowsill.cli` costs ~230ms versus ~20ms
for a bare interpreter and ~70ms for `import click` alone -- `-X importtime`
attributes the excess to `sqlite_utils` and to `windowsill.notify` pulling in
`urllib.request` at module scope, neither of which "what are the
subcommands" needs.

Checked via `sys.modules` in a fresh subprocess rather than a timing
threshold: faster to run, and not flaky under machine load the way a
wall-clock budget would be.
"""

from __future__ import annotations

import subprocess
import sys

#: Modules that `windowsill.cli` must not load just by being imported --
#: each is only needed by specific commands (`sqlite_utils` by anything that
#: touches the DB, `urllib.request` by `windowsill.notify`'s mail path) and
#: must be imported inside those commands' bodies, not at module scope.
_HEAVY_MODULES = ("sqlite_utils", "urllib.request")


def test_import_windowsill_cli_does_not_load_heavy_modules() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys, windowsill.cli; print('\\n'.join(sorted(sys.modules)))"],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = set(result.stdout.split())
    culprits = [m for m in _HEAVY_MODULES if m in loaded]

    assert not culprits, (
        f"import windowsill.cli pulled in {culprits!r}. windowsill.cli is imported on "
        "*every* CLI invocation, and will be on every TAB press once shell completion "
        "ships (Click re-invokes the program per keystroke) -- a top-level import here "
        "is not free, it's dozens of ms paid on a key someone holds down. Whatever "
        "innocent-looking `import` you just added at module scope, move it into the "
        "body of the command function that actually uses it instead."
    )
