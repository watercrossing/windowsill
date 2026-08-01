"""Tests for windowsill.completions -- shell completion scripts and install
(the completions brief: ship Click's native runtime completion, not a
static cache or an eval-in-your-rc line).

Click's own `click.shell_completion` already knows how to render a script
per shell; this module's own job is (1) picking the right `prog_name` /
`complete_var` per entry point and (2) writing the result to whichever
directory each shell actually scans without touching a single rc file.
That's what's under test here -- not Click's script content itself.

**No test may touch the real `$HOME`.** Every filesystem-touching test
below passes an explicit `tmp_path` as `home` (module-level functions) or
monkeypatches the `HOME` env var (CLI-level tests going through
`click.testing.CliRunner`, which is how `windowsill completions install`
actually discovers where to write).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from windowsill import cli, completions

# ---------------------------------------------------------------------------
# render() / render_all() -- script generation per shell
# ---------------------------------------------------------------------------


def test_render_bash_windowsill_registers_windowsill_and_its_complete_var() -> None:
    script = completions.render("bash", "windowsill")
    assert "_WINDOWSILL_COMPLETE" in script
    assert "complete -o nosort -F _windowsill_completion windowsill" in script


def test_render_zsh_windowsill_has_compdef_line() -> None:
    script = completions.render("zsh", "windowsill")
    assert script.startswith("#compdef windowsill")


def test_render_fish_windowsill_registers_the_windowsill_command() -> None:
    script = completions.render("fish", "windowsill")
    assert "_WINDOWSILL_COMPLETE" in script
    assert "--command windowsill" in script


@pytest.mark.parametrize(
    "prog_name,var",
    [
        ("windowsill", "_WINDOWSILL_COMPLETE"),
        ("ccsub", "_CCSUB_COMPLETE"),
        ("ccstat", "_CCSTAT_COMPLETE"),
        ("ccdel", "_CCDEL_COMPLETE"),
        ("ccresume", "_CCRESUME_COMPLETE"),
    ],
)
def test_complete_var_matches_click_derivation(prog_name: str, var: str) -> None:
    """Click derives `_<PROG_NAME>_COMPLETE` from argv[0]'s basename -- the
    short verbs are separate console-script entry points (pyproject.toml),
    not subcommands of `windowsill`, so each needs its own correctly-named
    variable. Verified empirically against Click 8.4.2's own
    `Command._main_shell_completion`, not assumed."""
    assert completions.complete_var(prog_name) == var
    for shell in completions.SHELLS:
        assert var in completions.render(shell, prog_name)


def test_render_all_covers_every_entry_point() -> None:
    scripts = completions.render_all("bash")
    assert set(scripts) == set(completions.ENTRY_POINTS)
    assert scripts["ccsub"] != scripts["ccstat"]


def test_render_unsupported_shell_raises() -> None:
    with pytest.raises(ValueError):
        completions.render("powershell", "windowsill")


# ---------------------------------------------------------------------------
# target directories
# ---------------------------------------------------------------------------


def test_bash_dir_is_the_bash_completion_2x_user_directory(tmp_path: Path) -> None:
    assert completions.bash_dir(tmp_path) == tmp_path / ".local" / "share" / "bash-completion" / "completions"


def test_fish_dir(tmp_path: Path) -> None:
    assert completions.fish_dir(tmp_path) == tmp_path / ".config" / "fish" / "completions"


def test_zsh_dir(tmp_path: Path) -> None:
    assert completions.zsh_dir(tmp_path) == tmp_path / ".zsh" / "completions"


@pytest.mark.parametrize(
    "shell_value,expected",
    [
        ("/bin/bash", "bash"),
        ("/usr/bin/zsh", "zsh"),
        ("/usr/local/bin/fish", "fish"),
        ("/bin/tcsh", "bash"),  # unrecognised -> bash fallback
        ("", "bash"),  # unset -> bash fallback
    ],
)
def test_detect_shell(shell_value: str, expected: str) -> None:
    assert completions.detect_shell({"SHELL": shell_value}) == expected


def test_detect_shell_missing_key_falls_back_to_bash() -> None:
    assert completions.detect_shell({}) == "bash"


# ---------------------------------------------------------------------------
# install() -- bash: one real file + symlinks
# ---------------------------------------------------------------------------


def test_install_bash_writes_one_real_file(tmp_path: Path) -> None:
    result = completions.install("bash", tmp_path)
    directory = completions.bash_dir(tmp_path)
    assert result.written == [directory / "windowsill"]
    assert (directory / "windowsill").read_text()


def test_install_bash_symlinks_the_other_four_names_to_the_real_file(tmp_path: Path) -> None:
    completions.install("bash", tmp_path)
    directory = completions.bash_dir(tmp_path)
    for name in ("ccsub", "ccstat", "ccdel", "ccresume"):
        link = directory / name
        assert link.is_symlink()
        assert link.resolve() == (directory / "windowsill").resolve()


def test_install_bash_real_file_registers_all_five_commands(tmp_path: Path) -> None:
    """bash-completion loads whichever file it finds under the command's
    name and sources it verbatim -- so the symlinked `ccsub` file must,
    when read, still contain `ccsub`'s own registration, because it's
    reading the same concatenated real file."""
    completions.install("bash", tmp_path)
    directory = completions.bash_dir(tmp_path)
    content = (directory / "ccsub").read_text()  # reading through the symlink
    assert "_CCSUB_COMPLETE" in content
    assert "complete -o nosort -F _ccsub_completion ccsub" in content
    # And the other four are in there too -- one real file, not five.
    assert "_WINDOWSILL_COMPLETE" in content
    assert "_CCSTAT_COMPLETE" in content
    assert "_CCDEL_COMPLETE" in content
    assert "_CCRESUME_COMPLETE" in content


def test_install_bash_is_idempotent(tmp_path: Path) -> None:
    first = completions.install("bash", tmp_path)
    second = completions.install("bash", tmp_path)
    directory = completions.bash_dir(tmp_path)
    assert (directory / "windowsill").read_text() == first.written[0].read_text()
    assert second.written == first.written
    for name in ("ccsub", "ccstat", "ccdel", "ccresume"):
        assert (directory / name).is_symlink()


def test_install_bash_replaces_a_stale_plain_file_with_a_symlink(tmp_path: Path) -> None:
    """A leftover regular file at one of the symlink paths (e.g. from a
    hand-written completion someone once dropped there) must not make a
    re-install crash."""
    directory = completions.bash_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "ccsub").write_text("stale content")

    completions.install("bash", tmp_path)

    assert (directory / "ccsub").is_symlink()


# ---------------------------------------------------------------------------
# install() -- fish: one real file + symlinks (same shape as bash)
# ---------------------------------------------------------------------------


def test_install_fish_writes_windowsill_fish(tmp_path: Path) -> None:
    result = completions.install("fish", tmp_path)
    directory = completions.fish_dir(tmp_path)
    assert result.written == [directory / "windowsill.fish"]


def test_install_fish_symlinks_the_other_four(tmp_path: Path) -> None:
    completions.install("fish", tmp_path)
    directory = completions.fish_dir(tmp_path)
    for name in ("ccsub", "ccstat", "ccdel", "ccresume"):
        link = directory / f"{name}.fish"
        assert link.is_symlink()
        assert link.resolve() == (directory / "windowsill.fish").resolve()


def test_install_fish_real_file_registers_all_five_commands(tmp_path: Path) -> None:
    completions.install("fish", tmp_path)
    directory = completions.fish_dir(tmp_path)
    content = (directory / "windowsill.fish").read_text()
    for name in completions.ENTRY_POINTS:
        assert f"--command {name}" in content


# ---------------------------------------------------------------------------
# install() -- zsh: five real files, no symlinks
# ---------------------------------------------------------------------------


def test_install_zsh_writes_five_real_files(tmp_path: Path) -> None:
    result = completions.install("zsh", tmp_path)
    directory = completions.zsh_dir(tmp_path)
    assert set(result.written) == {directory / f"_{name}" for name in completions.ENTRY_POINTS}
    assert result.linked == []


def test_install_zsh_each_file_has_its_own_compdef_line(tmp_path: Path) -> None:
    """Unlike bash/fish, a zsh symlink would be wrong: `compinit` reads the
    `#compdef` line, which Click bakes with exactly one command's name, so
    a `_ccsub` symlinked to `_windowsill`'s content would still declare
    `#compdef windowsill` and register the wrong command."""
    completions.install("zsh", tmp_path)
    directory = completions.zsh_dir(tmp_path)
    for name in completions.ENTRY_POINTS:
        content = (directory / f"_{name}").read_text()
        assert content.startswith(f"#compdef {name}")
    # And no two files are byte-identical -- confirms they're not symlinks
    # of each other in disguise.
    contents = {(directory / f"_{name}").read_text() for name in completions.ENTRY_POINTS}
    assert len(contents) == len(completions.ENTRY_POINTS)


def test_install_zsh_is_idempotent(tmp_path: Path) -> None:
    completions.install("zsh", tmp_path)
    result = completions.install("zsh", tmp_path)
    directory = completions.zsh_dir(tmp_path)
    assert len(result.written) == len(completions.ENTRY_POINTS)
    assert (directory / "_windowsill").read_text().startswith("#compdef windowsill")


def test_install_unsupported_shell_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        completions.install("powershell", tmp_path)


# ---------------------------------------------------------------------------
# CLI wiring -- `windowsill completions <shell>` / `windowsill completions install`
# ---------------------------------------------------------------------------


def test_cli_completions_bash_prints_script_to_stdout() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.main, ["completions", "bash"])
    assert result.exit_code == 0, result.output
    assert "_WINDOWSILL_COMPLETE" in result.output


def test_cli_completions_zsh_prints_script_to_stdout() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.main, ["completions", "zsh"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("#compdef windowsill")


def test_cli_completions_fish_prints_script_to_stdout() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.main, ["completions", "fish"])
    assert result.exit_code == 0, result.output
    assert "--command windowsill" in result.output


def test_cli_completions_install_writes_all_five_bash_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`install` resolves the target directory from the real `$HOME` (there
    is no `--home` here -- that flag means the windowsill *state* directory
    elsewhere in this CLI, a different thing), so the fake-HOME technique is
    the only safe way to exercise this without touching the real machine."""
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli.main, ["completions", "install", "--shell", "bash"])
    assert result.exit_code == 0, result.output

    directory = tmp_path / ".local" / "share" / "bash-completion" / "completions"
    for name in completions.ENTRY_POINTS:
        assert (directory / name).exists()
    assert str(directory / "windowsill") in result.output


def test_cli_completions_install_rejects_unknown_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli.main, ["completions", "install", "--shell", "powershell"])
    assert result.exit_code != 0


def test_cli_completions_install_auto_detects_shell_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    runner = CliRunner()
    result = runner.invoke(cli.main, ["completions", "install"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".config" / "fish" / "completions" / "windowsill.fish").exists()


def test_cli_completions_install_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    first = runner.invoke(cli.main, ["completions", "install", "--shell", "bash"])
    second = runner.invoke(cli.main, ["completions", "install", "--shell", "bash"])
    assert first.exit_code == 0
    assert second.exit_code == 0, second.output
