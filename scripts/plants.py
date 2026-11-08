#!/usr/bin/env python3
"""Plant receipts: prove each safety guard by deleting it and watching its test go red.

A guard that no test turns red when removed is not a guard; it is a comment.
Every entry below names a guard (one exact source edit that disables it) and
the test that must FAIL once it is gone. The script copies the tree, applies
the edit, runs the test, and reports RED (proven) or GREEN (not proven).

    python scripts/plants.py            # run every plant, exit 1 on any GREEN
    python scripts/plants.py --list     # print the table only

Edits are exact strings, asserted to occur exactly once, so a plant that no
longer matches the source fails loudly instead of silently testing nothing
(tests/test_plants_table.py checks the anchors on every run).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COPY_DIRS = ("src", "tests", "scripts")
COPY_FILES = ("pyproject.toml",)


@dataclass(frozen=True)
class Plant:
    name: str
    file: str
    test: str
    #: (exact text in the source, its replacement) -- the guard, disabled
    edits: tuple[tuple[str, str], ...] = field(default_factory=tuple)


PLANTS: tuple[Plant, ...] = (
    # -- the memory scope -----------------------------------------------------
    Plant(
        "memory-kept-by-session",
        "src/neurailyzer/config.py",
        "tests/test_memory_scope.py::test_session_wipe_keeps_memory",
        (("        if scope != MEMORY_SCOPE:\n            return self.keep\n", ""),),
    ),
    Plant(
        "mcp-memory-refusal",
        "src/neurailyzer/mcp_server.py",
        "tests/test_memory_scope.py::test_mcp_plans_memory_but_refuses_to_commit_it",
        (("if commit and MEMORY_SCOPE in core.expand_scopes(scopes):", "if False:"),),
    ),
    Plant(
        "all-includes-memory",
        "src/neurailyzer/config.py",
        "tests/test_memory_scope.py::test_wipe_all_forgets_everything_and_restore_brings_it_back",
        (
            (
                'FILE_SCOPES: tuple[str, ...] = ("session", "sandbox", "memory")',
                'FILE_SCOPES: tuple[str, ...] = ("session", "sandbox")',
            ),
        ),
    ),
    Plant(
        "restore-keep-by-snapshot-scope",
        "src/neurailyzer/cli.py",
        "tests/test_memory_scope.py::test_session_restore_never_rolls_memory_back",
        (
            (
                "keep = cfg.keep_for(MEMORY_SCOPE) if MEMORY_SCOPE in snap.scopes else cfg.keep",
                "keep = cfg.keep_for(MEMORY_SCOPE)",
            ),
        ),
    ),
    Plant(
        "restore-memory-from-memory-snapshot",
        "src/neurailyzer/cli.py",
        "tests/test_memory_scope.py::test_wipe_all_forgets_everything_and_restore_brings_it_back",
        (
            (
                "keep = cfg.keep_for(MEMORY_SCOPE) if MEMORY_SCOPE in snap.scopes else cfg.keep",
                "keep = cfg.keep",
            ),
        ),
    ),
    Plant(
        "mcp-restore-full-keep",
        "src/neurailyzer/mcp_server.py",
        "tests/test_memory_scope.py::test_mcp_plans_memory_but_refuses_to_commit_it",
        (("plan = store.restore(snap, cfg.keep)", "plan = store.restore(snap, cfg.hard_keep)"),),
    ),
    Plant(
        "memory-symlink-redirect",
        "src/neurailyzer/config.py",
        "tests/test_memory_scope.py::test_symlinked_memory_dir_is_skipped_not_followed",
        (("        if cand.is_symlink():\n            return True\n", ""),),
    ),
    Plant(
        "stow-root-all-spellings",
        "src/neurailyzer/config.py",
        "tests/test_memory_scope.py::test_stow_managed_root_does_not_false_skip_memory",
        (
            (
                "detect_literals = {p for p in presets_mod.expand_all(preset.detect)"
                " if p.exists()}",
                "detect_literals = set(presets_mod.expand_existing(preset.detect))",
            ),
        ),
    ),
    Plant(
        "library-hard-keep-fallback",
        "src/neurailyzer/config.py",
        "tests/test_memory_scope.py::test_library_config_without_hard_keep_keeps_everything_for_memory",
        (
            (
                "return self.hard_keep if self.hard_keep is not None else self.keep",
                "return self.hard_keep if self.hard_keep is not None else KeepList()",
            ),
        ),
    ),
    Plant(
        "glob-escapes-expanded-home",
        "src/neurailyzer/presets.py",
        "tests/test_memory_scope.py::test_home_with_glob_characters_never_selects_a_sibling_tree",
        (
            (
                'template = globmod.escape(os.path.expanduser("~")) + template[1:]',
                'template = os.path.expanduser("~") + template[1:]',
            ),
        ),
    ),
    Plant(
        "non-symlink-spelling-wins-dedup",
        "src/neurailyzer/presets.py",
        "tests/test_memory_scope.py::test_symlink_alias_does_not_shadow_the_real_memory_dir",
        (
            (
                "if prior is None or (prior.is_symlink() and not c.is_symlink()):",
                "if prior is None:",
            ),
        ),
    ),
    # -- time windows ---------------------------------------------------------
    Plant(
        "window-filter",
        "src/neurailyzer/wipers/local.py",
        "tests/test_time_window.py::test_after_takes_only_what_was_modified_since",
        (("return self.window.contains(os.lstat(p).st_mtime)", "return True"),),
    ),
    Plant(
        "undatable-is-error",
        "src/neurailyzer/wipers/local.py",
        "tests/test_time_window.py::test_an_undatable_entry_is_an_error_not_a_skip",
        (('return f"could not date {p}: {exc.strerror or exc}"', "return False"),),
    ),
    Plant(
        "window-dir-prune",
        "src/neurailyzer/wipers/local.py",
        "tests/test_time_window.py::test_after_takes_only_what_was_modified_since",
        (("elif prunable is not None and d not in prunable:", "elif False:"),),
    ),
    Plant(
        "remote-window-refusal",
        "src/neurailyzer/core.py",
        "tests/test_time_window.py::test_core_refuses_a_window_on_the_remote_scope",
        (
            (
                "if window is not None and not window.is_open and REMOTE_SCOPE in scopes:",
                "if False:",
            ),
        ),
    ),
    Plant(
        "list-state-remote-window",
        "src/neurailyzer/cli.py",
        "tests/test_time_window.py::test_cli_span_form_and_refusals",
        (("    if window is not None and Scope.remote in scope:", "    if False:"),),
    ),
    Plant(
        "list-state-default-excludes-remote-under-window",
        "src/neurailyzer/cli.py",
        "tests/test_time_window.py::test_cli_span_form_and_refusals",
        (
            (
                "if s is not Scope.all and not (window is not None and s is Scope.remote)",
                "if s is not Scope.all",
            ),
        ),
    ),
    Plant(
        "mcp-empty-bound",
        "src/neurailyzer/mcp_server.py",
        "tests/test_time_window.py::test_mcp_empty_bound_is_refused_not_unwindowed",
        (
            (
                "after=parse_when(after) if after is not None else None,",
                "after=parse_when(after) if after else None,",
            ),
            ("if (after is not None or before is not None)", "if (after or before)"),
        ),
    ),
    # -- honest status --------------------------------------------------------
    Plant(
        "status-kept-counts-keep-skips",
        "src/neurailyzer/core.py",
        "tests/test_time_window.py::test_scope_status_counts_keep_skips_only_and_carries_completeness",
        (
            (
                'kept_count=sum(1 for n in plan.notes if n.startswith("keep-list skip:")),',
                "kept_count=len(plan.notes),",
            ),
        ),
    ),
    Plant(
        "status-complete-flag",
        "src/neurailyzer/core.py",
        "tests/test_time_window.py::test_scope_status_counts_keep_skips_only_and_carries_completeness",
        (("        complete=plan.complete,", "        complete=True,"),),
    ),
    Plant(
        "cli-incomplete-render",
        "src/neurailyzer/cli.py",
        "tests/test_time_window.py::test_cli_list_state_shows_incomplete_when_a_file_cannot_be_read",
        (("        elif not st.complete:", "        elif False:"),),
    ),
    Plant(
        "mcp-incomplete-status",
        "src/neurailyzer/mcp_server.py",
        "tests/test_time_window.py::test_mcp_list_state_reports_an_incomplete_survey",
        (
            (
                '                        else "incomplete"\n'
                "                        if not st.complete\n",
                "",
            ),
        ),
    ),
    Plant(
        "mcp-incomplete-field",
        "src/neurailyzer/mcp_server.py",
        "tests/test_time_window.py::test_mcp_list_state_reports_an_incomplete_survey",
        (
            (
                '                    "complete": st.complete,',
                '                    "complete": True,',
            ),
        ),
    ),
)


def apply(text: str, edits: tuple[tuple[str, str], ...]) -> str:
    """Apply each edit exactly once; a missing or ambiguous anchor is an error."""
    for old, new in edits:
        n = text.count(old)
        if n != 1:
            raise LookupError(f"anchor occurs {n} times, expected 1: {old!r}")
        text = text.replace(old, new, 1)
    return text


def _copy_tree(dest: Path) -> None:
    for d in COPY_DIRS:
        shutil.copytree(
            ROOT / d,
            dest / d,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
    for f in COPY_FILES:
        shutil.copy2(ROOT / f, dest / f)


#: pytest exit codes that mean "tests ran and some FAILED" -- the only red
#: that proves a guard. 2 (interrupted / collection error), 3 (internal
#: error), 4 (usage error) and 5 (no tests collected) prove nothing.
_TESTS_FAILED = 1


def _pytest(copy: Path, test: str) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603 -- fixed argv, our own test suite
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", test],
        cwd=copy,
        env={"PYTHONPATH": str(copy / "src"), **_passthrough_env()},
        capture_output=True,
        text=True,
        timeout=600,
    )
    last = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.strip()), "")
    if not last and proc.stderr.strip():
        last = proc.stderr.strip().splitlines()[-1]
    return proc.returncode, last


def run_plant(plant: Plant) -> tuple[bool, str]:
    """(proven, detail): proven means the named test PASSED on the untouched
    copy and FAILED (exit 1, "failed" in the summary) once the guard was gone.

    A missing interpreter, an import error or a collection error is not a
    red: it is "no reading", and it never counts as proof.
    """
    with tempfile.TemporaryDirectory(prefix="plant-") as tmp:
        copy = Path(tmp)
        _copy_tree(copy)
        base_rc, base_last = _pytest(copy, plant.test)
        ok, why = baseline_verdict(base_rc, base_last)
        if not ok:
            return False, why
        target = copy / plant.file
        try:
            target.write_text(apply(target.read_text(), plant.edits))
        except LookupError as exc:
            return False, f"anchor mismatch: {exc}"
        return mutant_verdict(*_pytest(copy, plant.test))


def baseline_verdict(rc: int, last: str) -> tuple[bool, str]:
    """The untouched copy must PASS the named test, or nothing is measured."""
    if rc != 0 or "passed" not in last:
        return False, f"no baseline: the test does not pass untouched (exit {rc}: {last})"
    return True, last


def mutant_verdict(rc: int, last: str) -> tuple[bool, str]:
    """Only exit 1 with a 'failed' summary is a red that proves the guard."""
    if rc == 0:
        return False, f"GREEN with the guard removed -- not proven ({last})"
    if rc != _TESTS_FAILED or "failed" not in last:
        return False, f"no reading: pytest exit {rc}, not a test failure ({last})"
    return True, last


def _passthrough_env() -> dict[str, str]:
    import os

    keep = ("HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "PATH", "LANG", "LC_ALL")
    return {k: os.environ[k] for k in keep if k in os.environ}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="print the plant table and exit")
    ap.add_argument("names", nargs="*", help="run only these plants (default: all)")
    args = ap.parse_args(argv)
    chosen = [p for p in PLANTS if not args.names or p.name in args.names]
    if args.list:
        for p in chosen:
            print(f"{p.name:36} {p.file:36} {p.test}")
        return 0
    failures = 0
    started = time.monotonic()
    print(f"plants: {len(chosen)} guard(s) to disprove", flush=True)
    for i, p in enumerate(chosen, 1):
        print(f"[{i}/{len(chosen)}] {p.name} ... ", end="", flush=True)
        proven, detail = run_plant(p)
        print(("RED  " if proven else "GREEN") + f"  {detail}", flush=True)
        failures += 0 if proven else 1
    elapsed = int(time.monotonic() - started)
    print(f"plants: {len(chosen) - failures}/{len(chosen)} proven red in {elapsed}s", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
