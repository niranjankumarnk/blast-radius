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

## Heavy tests

Three tests take 6 to 29 seconds each, so one of them decides the cost of any commit that selects it. When a
selection carries one, and the cheaper selected tests are cheap enough to run (an estimate from measured CPU
times), the selector runs those cheaper tests in the container against the patched tree: a symlink copy of the
pinned checkout in `/tmp` with the patched files written for real, `requirements.lock` installed, pytest in a
forked child. The heavy test is left out only if every cheaper selected test passes and at least three of them
exercise each changed spot that selected the heavy test. This is a bet, not a proof: the heavy test could still be
the only one that fails.

- A test that fails on the patched tree is re-run on the unpatched tree. If it fails there too, the container
  cannot judge it: it keeps its static decision and is not counted for or against the heavy test.
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

## Pre-existing code

The organisers' starter kit (`score.py`, `sample/`, `requirements.lock`, `SCORING.md`, `Makefile`), sqlglot, and
coverage.py (map building only). The selector itself uses only the Python standard library.

Written during the event with AI assistance.

## License

MIT
