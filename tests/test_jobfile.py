"""Tests for windowsill.jobfile — directive parsing and pass-through split.

Covers design/02-job-file.md §2 exactly: §2.1's parsing rules (shebang, comments,
trailing-``\\`` continuation, ``shlex`` tokenisation, header-terminated-
by-prose, verbatim body), and §2.2's central design rule — scheduler
directives are consumed, everything else passes through untouched, in
argv order, never validated.

Pure parsing: these tests only ever read fixture text from disk and feed
it to ``parse_jobfile``. No network, no subprocess, no clock.
"""

from __future__ import annotations

from pathlib import Path

from windowsill.jobfile import Directives, JobFile, parse_jobfile

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "jobs"


def _read(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


# ---------------------------------------------------------------------------
# The §2 example, verbatim — the canonical fixture
# ---------------------------------------------------------------------------


def test_design_doc_example_directives() -> None:
    jf = parse_jobfile(_read("basic.md"))

    assert isinstance(jf, JobFile)
    assert isinstance(jf.directives, Directives)

    d = jf.directives
    assert d.name == "refactor-auth"
    assert d.at == "2026-07-18T02:00"
    assert d.before is None
    assert d.window == "fresh"
    assert d.anchor is False
    assert d.cwd == "~/code/myapp"
    assert d.resume is None
    assert d.resume_from == "audit-auth"
    assert d.hold_jid == []
    assert d.retry == 2
    assert d.priority == 0
    assert d.isolate == "worktree"
    assert d.allow_dirty is False
    assert d.notify == "email"
    assert d.hold is False
    assert d.timeout == "4h"
    assert d.no_resume_on_limit is False


def test_design_doc_example_passthrough_is_argv_in_order() -> None:
    jf = parse_jobfile(_read("basic.md"))

    # "#$ --model claude-opus-4-8" becomes "claude --model claude-opus-4-8".
    # "#$ --max-turns 60" becomes "--max-turns 60". Order preserved, argv
    # elements (never a concatenated shell string).
    assert jf.passthrough == ["--model", "claude-opus-4-8", "--max-turns", "60"]
    assert all(isinstance(tok, str) for tok in jf.passthrough)


def test_design_doc_example_model_is_mirrored_not_intercepted() -> None:
    jf = parse_jobfile(_read("basic.md"))

    # --model is pass-through, but is *also* mirrored for jobs.model /
    # anchor defaults. Mirror, don't intercept: it must still be present
    # in passthrough.
    assert jf.model == "claude-opus-4-8"
    assert "--model" in jf.passthrough
    assert "claude-opus-4-8" in jf.passthrough


def test_design_doc_example_body_is_verbatim() -> None:
    jf = parse_jobfile(_read("basic.md"))

    expected_body = (
        "Continue the auth refactor from where the audit job left off.\n"
        "\n"
        "Done means: every call site of `legacy_verify()` migrated to "
        "`verify_token()`,\n"
        "`pytest tests/auth` green, and a single squashed commit on the "
        "branch.\n"
        "If you hit a blocker, do not stop — write what you tried and what "
        "you would\n"
        "need to `BLOCKED.md`, then continue with the next call site.\n"
    )
    assert jf.body == expected_body


# ---------------------------------------------------------------------------
# Rule 2: a leading #! line is ignored
# ---------------------------------------------------------------------------


def test_shebang_ignored_gives_identical_result_to_no_shebang() -> None:
    with_shebang = parse_jobfile(_read("basic.md"))
    without_shebang = parse_jobfile(_read("no_shebang.md"))

    assert with_shebang.directives == without_shebang.directives
    assert with_shebang.passthrough == without_shebang.passthrough
    assert with_shebang.body == without_shebang.body
    assert with_shebang.model == without_shebang.model


def test_shebang_line_itself_never_leaks_into_output() -> None:
    jf = parse_jobfile(_read("basic.md"))

    assert "#!/usr/bin/env windowsill" not in jf.body
    assert "#!/usr/bin/env windowsill" not in jf.passthrough
    assert not any("windowsill" == tok for tok in jf.passthrough)


def test_non_leading_bang_line_is_just_a_bare_comment() -> None:
    # Rule 2 only ignores a *leading* #! line. One that isn't first is
    # just another bare "#" comment line inside the header.
    text = "#$ --name x\n#!not-a-real-shebang\n#$ --retry 3\n\nbody\n"
    jf = parse_jobfile(text)

    assert jf.directives.name == "x"
    assert jf.directives.retry == 3
    assert jf.passthrough == []
    assert jf.body == "body\n"


# ---------------------------------------------------------------------------
# Rule 4: "#$ #..." and bare "#" lines inside the header are comments
# ---------------------------------------------------------------------------


def test_comments_in_header_are_skipped() -> None:
    jf = parse_jobfile(_read("comments.md"))

    d = jf.directives
    assert d.name == "comment-test"
    assert d.at == "+3h"
    assert d.window == "offpeak"
    assert d.notify == "ntfy"
    assert jf.passthrough == []
    assert jf.model is None
    assert jf.body == "Body starts here and must not contain any comment text.\n"


def test_comment_text_never_leaks_anywhere() -> None:
    jf = parse_jobfile(_read("comments.md"))

    haystacks = [jf.body, *jf.passthrough, str(jf.directives)]
    for h in haystacks:
        assert "scratch test" not in h
        assert "must be ignored" not in h
        assert "double-hash" not in h


# ---------------------------------------------------------------------------
# Rule 5: a trailing "\" continues a directive onto the next line
# ---------------------------------------------------------------------------


def test_trailing_backslash_continues_scheduler_directive() -> None:
    jf = parse_jobfile(_read("continuation.md"))

    assert jf.directives.name == "continuation-test"
    # "tomorrow" and "02:00" were split across two physical lines by a
    # trailing backslash; they belong to one logical --at value.
    assert jf.directives.at == "tomorrow 02:00"


def test_trailing_backslash_continues_passthrough_directive() -> None:
    jf = parse_jobfile(_read("continuation.md"))

    assert jf.passthrough == ["--allowedTools", "Read", "Edit", "Write", "Grep"]
    assert jf.body == "Body text for the continuation fixture.\n"


# ---------------------------------------------------------------------------
# Rule 6: shlex.split, never a bare .split()
# ---------------------------------------------------------------------------


def test_quoted_value_with_spaces_stays_one_token() -> None:
    jf = parse_jobfile(_read("flags_and_quoting.md"))

    # A bare .split() would have chopped this into '~/my', 'code/app'.
    assert jf.directives.cwd == "~/my code/app"


def test_shlex_handles_backslash_escaped_space_in_unquoted_value() -> None:
    text = "#$ --name foo\\ bar\n\nbody\n"
    jf = parse_jobfile(text)

    assert jf.directives.name == "foo bar"


# ---------------------------------------------------------------------------
# Boolean scheduler directives and comma-list / numeric coercion
# ---------------------------------------------------------------------------


def test_boolean_flags_default_false() -> None:
    jf = parse_jobfile(_read("basic.md"))

    d = jf.directives
    assert d.anchor is False
    assert d.allow_dirty is False
    assert d.hold is False
    assert d.no_resume_on_limit is False


def test_boolean_flags_set_true_when_present() -> None:
    jf = parse_jobfile(_read("flags_and_quoting.md"))

    d = jf.directives
    assert d.anchor is True
    assert d.allow_dirty is True
    assert d.hold is True
    assert d.no_resume_on_limit is True


def test_hold_jid_is_split_on_comma() -> None:
    jf = parse_jobfile(_read("flags_and_quoting.md"))
    assert jf.directives.hold_jid == ["audit-auth", "lint-fix"]


def test_retry_and_priority_are_ints_and_priority_may_be_negative() -> None:
    jf = parse_jobfile(_read("flags_and_quoting.md"))
    assert jf.directives.priority == -5
    assert isinstance(jf.directives.priority, int)

    jf2 = parse_jobfile(_read("basic.md"))
    assert jf2.directives.retry == 2
    assert isinstance(jf2.directives.retry, int)


# ---------------------------------------------------------------------------
# §2.2: --resume shadows the real claude flag; scheduler directives never
# reach passthrough
# ---------------------------------------------------------------------------


def test_resume_is_consumed_by_scheduler_never_reaches_passthrough() -> None:
    jf = parse_jobfile(_read("flags_and_quoting.md"))

    assert jf.directives.resume == "conv-abc-123"
    assert "--resume" not in jf.passthrough
    assert "conv-abc-123" not in jf.passthrough


def test_unknown_directive_passes_through_unvalidated() -> None:
    jf = parse_jobfile(_read("flags_and_quoting.md"))

    # windowsill MUST NOT validate or reject unknown directives — pass them
    # through, argv order preserved, and let claude complain.
    assert jf.passthrough == ["--some-new-flag", "value-with-dashes"]


def test_scheduler_directives_never_appear_in_passthrough() -> None:
    jf = parse_jobfile(_read("basic.md"))

    scheduler_tokens = {
        "--name",
        "--at",
        "--window",
        "--cwd",
        "--resume-from",
        "--isolate",
        "--retry",
        "--notify",
    }
    assert not (scheduler_tokens & set(jf.passthrough))


def test_log_is_consumed_as_a_scheduler_directive() -> None:
    # §2.3 --log: where the rendered transcript is *also* written. Scheduler
    # directive, so it never reaches claude's argv.
    jf = parse_jobfile("#$ --log ~/logs/nightly/\n\nbody\n")

    assert jf.directives.log == "~/logs/nightly/"
    assert jf.passthrough == []


def test_log_value_may_be_quoted_with_spaces() -> None:
    jf = parse_jobfile('#$ --log "~/my logs/run.md"\n\nbody\n')

    assert jf.directives.log == "~/my logs/run.md"


def test_log_defaults_to_none() -> None:
    assert parse_jobfile(_read("basic.md")).directives.log is None


# ---------------------------------------------------------------------------
# §5.5 --warm / --warm-lead
# ---------------------------------------------------------------------------


def test_warm_is_a_boolean_scheduler_directive() -> None:
    jf = parse_jobfile("#$ --warm\n\nbody\n")

    assert jf.directives.warm is True
    # A scheduler directive, so `claude` never sees it -- there is no such
    # claude flag, and passing it through would fail the run outright.
    assert jf.passthrough == []


def test_warm_lead_is_a_value_directive_overriding_the_config_default() -> None:
    jf = parse_jobfile("#$ --warm\n#$ --warm-lead 45m\n\nbody\n")

    assert jf.directives.warm is True
    assert jf.directives.warm_lead == "45m"
    assert jf.passthrough == []


def test_warming_directives_default_to_off() -> None:
    """§5.5 is opt-in: a job file that says nothing about warming is not a
    warm job, and carries no lead of its own for the scheduler to prefer
    over `[warm].lead`."""
    d = parse_jobfile(_read("basic.md")).directives

    assert d.warm is False
    assert d.warm_lead is None


def test_warm_lead_without_warm_is_inert_not_an_error() -> None:
    """The parser validates nothing (§2.2), and the pair is deliberately not
    coupled: `--warm-lead` alone records a lead on a job that is not warm,
    exactly as `--retry 3` records a retry count on a job that may never run."""
    d = parse_jobfile("#$ --warm-lead 90m\n\nbody\n").directives

    assert d.warm is False
    assert d.warm_lead == "90m"


def test_resume_and_resume_from_both_present_is_not_rejected() -> None:
    # Pass-through beats validation (§0.4): jobfile.py must not reject
    # this even though §2.3 documents them as mutually exclusive — that
    # is the scheduler's job, at dispatch time, not the parser's.
    text = "#$ --resume conv-1\n#$ --resume-from other-job\n\nbody\n"
    jf = parse_jobfile(text)

    assert jf.directives.resume == "conv-1"
    assert jf.directives.resume_from == "other-job"


# ---------------------------------------------------------------------------
# Rule 1 / header-terminated-by-prose
# ---------------------------------------------------------------------------


def test_header_terminated_by_prose_cannot_resume() -> None:
    jf = parse_jobfile(_read("prose_header.md"))

    d = jf.directives
    assert d.name == "prose-test"
    assert d.priority == 5
    # The "#$ --retry 9" line appears *after* prose, so it is never
    # parsed as a directive — it is inert text inside the body.
    assert d.retry == 0


def test_header_terminated_by_prose_body_is_verbatim_including_hash_lines() -> None:
    jf = parse_jobfile(_read("prose_header.md"))

    expected_body = (
        "This line is prose, not a directive or a blank line, so the "
        "header ends here.\n"
        "#$ --retry 9\n"
        "This directive-looking line must be treated as literal body "
        "text, verbatim.\n"
    )
    assert jf.body == expected_body
    assert jf.passthrough == []


def test_no_directives_at_all_is_pure_body() -> None:
    original = _read("no_directives.md")
    jf = parse_jobfile(original)

    assert jf.directives == Directives()
    assert jf.passthrough == []
    assert jf.model is None
    assert jf.body == original


# ---------------------------------------------------------------------------
# Rule 3: body starts at the first blank line after the header, verbatim,
# to EOF — including the no-body edge cases
# ---------------------------------------------------------------------------


def test_no_blank_line_at_all_means_empty_body() -> None:
    jf = parse_jobfile(_read("no_body_no_blank.md"))

    assert jf.directives.name == "headless"
    assert jf.directives.retry == 1
    assert jf.body == ""


def test_header_then_blank_then_eof_gives_empty_body() -> None:
    jf = parse_jobfile(_read("empty_body.md"))

    assert jf.directives.name == "empty-body"
    assert jf.body == ""


def test_body_preserves_blank_lines_and_markdown_formatting() -> None:
    jf = parse_jobfile(_read("basic.md"))

    # Verbatim means verbatim: internal blank line and backticked code
    # spans must survive untouched.
    assert "\n\n" in jf.body
    assert "`legacy_verify()`" in jf.body
    assert "`BLOCKED.md`" in jf.body


# ---------------------------------------------------------------------------
# Defaults when no header is present at all
# ---------------------------------------------------------------------------


def test_defaults_with_no_header() -> None:
    jf = parse_jobfile("Just do the thing.\n")

    d = jf.directives
    assert d.name is None
    assert d.at is None
    assert d.before is None
    assert d.window == "share"
    assert d.anchor is False
    assert d.cwd is None
    assert d.resume is None
    assert d.resume_from is None
    assert d.hold_jid == []
    assert d.retry == 0
    assert d.priority == 0
    assert d.isolate is None
    assert d.allow_dirty is False
    assert d.notify is None
    assert d.hold is False
    assert d.timeout == "4h"
    assert d.no_resume_on_limit is False

    assert jf.passthrough == []
    assert jf.model is None
    assert jf.body == "Just do the thing.\n"
