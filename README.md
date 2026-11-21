# Blast Radius entry

Picks the tests a patch to sqlglot (pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`) can break, from the
patch and the pinned source. For the few selections that carry a very slow test it also runs the cheaper selected
tests in the container (see "Heavy tests").

## How it works

`map.bin` is built from the pinned commit only. No patch, and no kind of edit, is looked up anywhere.

- Every test was run alone in its own process under `coverage`, recording the lines it executes (import time and
  child processes included), the non-Python files it opens, the functions and lambdas it enters, and the sqlglot
  classes it instantiates. Lines a test reaches only when the whole suite shares one process are added from a
  second run with one coverage context per test.

`selector.py` applies each patch to the pinned source in memory and compares the two versions statement by
statement. A test can behave differently only if, on the pinned code, it executes a statement that was changed
or removed, or passes a point where one was inserted:

- a changed or removed statement selects the tests that execute it;
- an inserted statement selects the tests that execute the next statement of its block (or the last one, at the
  end of a block);
- a changed lambda in a class-level table selects the tests that call that lambda;
- a changed signature selects the function's callers, and everyone who imports the file when the `def` statement
  itself does different work (decorators, non-constant defaults, evaluated annotations);
- a function that gains or loses a local name selects everyone who calls it;
- a changed class attribute of a dialect, parser, generator or expression class selects the tests that build an
  instance of that class family and, for a constant, execute a line that mentions its name;
- a removed or renamed function also selects the tests that execute a line referring to the old name;
- statements that run at import time select everyone who imports the file;
- comments, layout and docstrings select nothing, except an encoding declaration;
- a changed fixture selects the tests that read it; a new, deleted or renamed file selects the tests that import
  it or list its directory;
- anything it cannot reason about (a patch that does not fit the pinned file, `conftest.py`, pytest
  configuration, a new module at the repository root, an internal error) selects every test.

Selected tests are packed longest-first onto 1 to 8 workers, whichever count costs least.

## Heavy tests, exactly where possible

Two of the slow tests are loops over independent units: `test_generator_memory` checks every dialect in its own
subtest, and `test_execute_tpcds` every TPC-DS query. `gmap.bin` and `qmap.bin` are maps of the same kind as
`map.bin`, built from the pinned commit, whose "tests" are those units (plus one entry for what only the real
test executes: its own body, the class set-up, the process pool). When a selection carries one of these tests,
the same statement-level rules run against the unit map and name the units the patch can affect. If replaying
them is cheap enough, the selector does it in the container on the patched tree (`gm_replay.py`,
`tpcds_replay.py`: the test's own steps, without pytest or DuckDB):

- for a dialect, the memory left behind by 1,000 generators, which the test asserts on;
- for a query, a digest of the DuckDB SQL and of the executor's result table, which are all that sqlglot
  contributes to the test's comparison. The image build computes the pinned commit's digests.

If every affected unit behaves as on the pinned commit, the test cannot fail and is not scheduled. If one does
not, or the patch touches what only the real test executes, the test stays. This is not a bet: an unaffected unit
executes no changed code. A unit that differs is re-run on the unpatched tree; if it differs there too, the
container cannot replay it and the mechanism turns itself off. `BR_EXACT=0` turns it off.

## Heavy tests, by witnesses

Three tests take 6 to 29 seconds each, so one of them decides the cost of any commit that selects it. When a
selection carries one, and the cheaper selected tests are cheap enough to run (an estimate from measured CPU
times), the selector runs those cheaper tests in the container against the patched tree: a symlink copy of the
pinned checkout in `/tmp` with the patched files written for real, `requirements.lock` installed, pytest in a
forked child. The heavy test is left out only if every cheaper selected test passes and at least three of them
exercise each changed spot that selected the heavy test. This is a bet, not a proof: the heavy test could still be
the only one that fails.

Two of the test files (`test_optimizer.py`, `test_executor.py`) need pandas, duckdb and worker processes, which
makes their tests expensive to run here. When running every cheaper selected test would cost too much, the
selector tries the cheaper selected tests outside those two files instead, under the same rule (all pass, three
per changed spot). The tests it did not run stay scheduled.

- A test that fails on the patched tree is re-run on the unpatched tree. If it fails there too, the container
  cannot judge it: it keeps its static decision and is not counted for or against the heavy test.
- A run stops at its first failure: one failing test already keeps the static selection.
- A timeout, a crash, or a patch that cannot be reproduced exactly keeps the static selection.
- `BR_RUN=0` turns all of this off.

## Rebuild the map

    uv venv --python 3.13 .venv && uv pip install -p .venv -r requirements.lock coverage
    git clone https://github.com/tobymao/sqlglot sqlglot-src && git -C sqlglot-src checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
    .venv/bin/python build/iso.py sqlglot-src sample/tests.json 8        # lines and file reads, one process per test
    .venv/bin/python build/isofn.py sqlglot-src sample/tests.json 8      # function entries and class instances
    # build/suitecov.py (see its docstring) writes build/suite.json from one whole-suite run
    .venv/bin/python build/mkmap.py sample/tests.json map.bin

## Local score

    make sample                      # Docker, under the scorer's limits
    .venv/bin/python evalsel.py      # no Docker

## Checking recall

`gen/mutate.py` writes varied patches against the pinned checkout. `gen/label.py` finds the tests each one
really breaks by running the whole suite, and `gen/label_fast.py` by running only the tests that import or read
a changed file. `gen/evalset.py` scores the selector against those labels and re-runs any apparent miss to rule
out a flaky test. They are used only to measure the selector: nothing from them ships in the image.

`gen/label_heavy.py` gathers the evidence for the heavy-test rule (for each patch that selects a heavy test: which
cheaper selected tests fail, and does the heavy test), and `gen/heavy_table.py` tabulates how often each rule
would drop a heavy test and how often that would be wrong. `tpcds_replay.py` replays `test_execute_tpcds` query by
query without pytest or DuckDB; `gen/replay_eval.py` uses it as a faster source of that test's verdict, and
`build/mkumap.py` builds the unit maps, `gen/units_eval.py` checks the exact verdicts against labels, and
`gen/qslice_stats.py` measures how many queries a patch can affect. Apart from the two replay modules, none of
these ship in the image.

## Pre-existing code

The organisers' starter kit (`score.py`, `sample/`, `requirements.lock`, `SCORING.md`, `Makefile`), sqlglot, and
coverage.py (map building only). The selector itself uses only the Python standard library.

Written during the event with AI assistance.

## License

MIT
