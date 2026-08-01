"""Shell completion: generation and install (design's completions brief).

Click ships runtime completion for free once import time is cheap enough --
and it is: `import windowsill.cli` was measured at 230ms and is now ~85ms
(commit 6daf5c3), and a real TAB press through Click's runtime callback
measures 89ms, under the 100ms bar. So this module does **not** build a
static generated script, a `${XDG_CACHE_HOME}` cache, or an
`eval "$(...)"`-in-your-rc instruction as the primary path -- those were
only the fallback for a bar that was not missed. It renders the script
Click's own `click.shell_completion` machinery already knows how to
produce, once per entry point, and writes it to whichever directory each
shell actually scans, so freshness is free and no rc file is touched.

Kept out of `cli.py`'s module scope on purpose (see that module's
docstring on the TAB-press import budget): `from windowsill import
completions` happens inside the `completions` command bodies, never at
`cli.py` import time. This module itself only imports `click` (already
paid for by `cli.py`) plus stdlib; nothing here pulls in `sqlite_utils`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

BASH = "bash"
ZSH = "zsh"
FISH = "fish"
SHELLS = (BASH, ZSH, FISH)

#: The five console-script entry points (pyproject.toml `[project.scripts]`)
#: that each need their own completion registration. The short verbs are
#: separate Click commands in their own right, not subcommands of
#: `windowsill` (cli.py's `home_option` docstring: "the `main` group's
#: callback never runs for them") -- Click derives the `_<PROG>_COMPLETE`
#: env var and the shell-script function/command names from `prog_name`
#: (argv[0]'s basename), so each entry point renders a genuinely different
#: script, not five copies of the same one.
ENTRY_POINTS = ("windowsill", "ccsub", "ccstat", "ccdel", "ccresume")


def _commands() -> dict[str, object]:
    from windowsill import cli  # deferred: see module docstring

    return {
        "windowsill": cli.main,
        "ccsub": cli.sub,
        "ccstat": cli.stat,
        "ccdel": cli.del_,
        "ccresume": cli.resume,
    }


def complete_var(prog_name: str) -> str:
    """The env var Click checks for a given entry point, e.g. ``ccsub`` ->
    ``_CCSUB_COMPLETE``. Must match Click's own derivation exactly
    (verified against Click 8.4.2's ``Command._main_shell_completion``:
    ``f"_{prog_name.replace('-', '_')}_COMPLETE".upper()``), or the
    generated script would poll a variable Click never reads.
    """
    return f"_{prog_name.replace('-', '_').upper()}_COMPLETE"


def render(shell: str, prog_name: str) -> str:
    """The completion script for one entry point, one shell."""
    from click.shell_completion import get_completion_class  # deferred: see module docstring

    if shell not in SHELLS:
        raise ValueError(f"unsupported shell: {shell!r} (expected one of {SHELLS})")
    comp_cls = get_completion_class(shell)
    if comp_cls is None:  # pragma: no cover -- SHELLS mirrors Click's own registry
        raise ValueError(f"unsupported shell: {shell!r} (expected one of {SHELLS})")
    command = _commands()[prog_name]
    comp = comp_cls(command, {}, prog_name, complete_var(prog_name))
    return comp.source()


def render_all(shell: str) -> dict[str, str]:
    """``render`` for every entry point, keyed by name."""
    return {name: render(shell, name) for name in ENTRY_POINTS}


# ---------------------------------------------------------------------------
# target directories
# ---------------------------------------------------------------------------


def bash_dir(home: Path) -> Path:
    """bash-completion 2.x's per-user completions directory. Ground truth
    on the target machine: bash-completion-2.11 is auto-sourced from
    `/etc/profile.d/bash_completion.sh` and loads files from here *by
    command name* -- no rc edit needed, unlike `uv`'s `eval` line."""
    return home / ".local" / "share" / "bash-completion" / "completions"


def zsh_dir(home: Path) -> Path:
    """Not on `$fpath` by default on any zsh install this project has seen
    -- `install`'s zsh path says so in its returned note, since (unlike
    bash-completion and fish) there is no directory zsh scans for free."""
    return home / ".zsh" / "completions"


def fish_dir(home: Path) -> Path:
    """fish autoloads `*.fish` files from here by filename on every new
    shell -- no rc edit, same free ride as bash-completion's directory."""
    return home / ".config" / "fish" / "completions"


def detect_shell(env: Mapping[str, str] | None = None) -> str:
    """``$SHELL``'s basename, or ``bash`` if unset or unrecognised."""
    source = os.environ if env is None else env
    name = Path(source.get("SHELL", "")).name
    return name if name in SHELLS else BASH


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


@dataclass
class InstallResult:
    shell: str
    #: Real files actually written (script content lives here).
    written: list[Path] = field(default_factory=list)
    #: (symlink path, real file it points at) pairs.
    linked: list[tuple[Path, Path]] = field(default_factory=list)
    #: Whether a new shell is needed, and why -- printed by the CLI.
    note: str = ""


def _symlink(link_path: Path, target_name: str) -> None:
    """(Re-)point ``link_path`` at ``target_name`` (a relative name in the
    same directory). Idempotent: clears whatever was there first, whether
    that's a stale symlink or a leftover plain file."""
    if link_path.is_symlink() or link_path.exists():
        link_path.unlink()
    link_path.symlink_to(target_name)


def _install_fanout(directory: Path, shell: str, real_name: str, suffix: str) -> InstallResult:
    """bash and fish share a loader shape: whichever file the shell finds
    under a command's name gets *sourced* (bash) or *autoloaded*
    (fish) verbatim, and it's the file's *content* -- not its name -- that
    decides which command(s) actually get registered (bash via
    ``complete -F ... <cmd>``, fish via ``complete --command <cmd>``). So
    one real file holding all five entry points' scripts, plus a symlink
    per remaining name, is both correct and avoids writing five
    near-identical files by hand.
    """
    directory.mkdir(parents=True, exist_ok=True)
    scripts = render_all(shell)
    content = "\n".join(scripts[name] for name in ENTRY_POINTS) + "\n"
    real_path = directory / f"{real_name}{suffix}"
    real_path.write_text(content)
    result = InstallResult(shell=shell, written=[real_path])
    for name in ENTRY_POINTS:
        if name == real_name:
            continue
        link_path = directory / f"{name}{suffix}"
        _symlink(link_path, f"{real_name}{suffix}")
        result.linked.append((link_path, real_path))
    return result


def _install_bash(home: Path) -> InstallResult:
    result = _install_fanout(bash_dir(home), BASH, "windowsill", "")
    result.note = (
        "new bash shells pick this up automatically (bash-completion scans this "
        "directory by command name); `source` the file to use it in this shell "
        "without restarting."
    )
    return result


def _install_fish(home: Path) -> InstallResult:
    result = _install_fanout(fish_dir(home), FISH, "windowsill", ".fish")
    result.note = "new fish shells pick this up automatically (fish autoloads *.fish files here by filename)."
    return result


def _install_zsh(home: Path) -> InstallResult:
    """zsh's loader is the opposite shape from bash/fish: `compinit` binds
    a command name to a completion function by reading each file's
    `#compdef` line, and Click bakes exactly one command's name into that
    line (and into the generated function body). A symlink here would
    register the *wrong* command -- `_ccsub` symlinked to `_windowsill`'s
    content would still declare `#compdef windowsill`. So zsh gets five
    real files, not a real file plus symlinks.
    """
    directory = zsh_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name in ENTRY_POINTS:
        path = directory / f"_{name}"
        path.write_text(render(ZSH, name))
        written.append(path)
    return InstallResult(
        shell=ZSH,
        written=written,
        note=(
            f"if {directory} is not already on your $fpath, add `fpath+=({directory})` "
            "before `compinit` in .zshrc and start a new shell (unlike bash-completion "
            "and fish, zsh does not scan a fixed user directory automatically)."
        ),
    )


_INSTALLERS = {BASH: _install_bash, ZSH: _install_zsh, FISH: _install_fish}


def install(shell: str, home: Path) -> InstallResult:
    """Write completion files for all five entry points, for one shell,
    under ``home`` (the real ``Path.home()`` in production; a ``tmp_path``
    in every test -- see tests/test_completions.py's module docstring)."""
    if shell not in SHELLS:
        raise ValueError(f"unsupported shell: {shell!r} (expected one of {SHELLS})")
    return _INSTALLERS[shell](home)
