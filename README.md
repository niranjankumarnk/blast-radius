# Blast Radius entry

Picks the tests a patch to sqlglot (pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`) can break.

## How it works

- `map.bin` is built from the pinned commit only: every test is run once in its own process under
  `coverage`, recording the source lines it executes (import time included) and the non-Python files it
  opens. No patch, and no kind of edit, is looked up anywhere.
- `selector.py` applies each patch to the pinned source in memory. A Python file whose AST is unchanged
  selects nothing. Otherwise each changed statement selects the tests that executed its lines; a changed
  fixture selects the tests that read it. Anything it cannot reason about selects every test.
- When the selected tests are cheap, the selector runs them inside the container against the patched tree
  (a symlink overlay of the pinned checkout in `/tmp`, `requirements.lock` installed) and schedules only the
  ones that fail. It does this only when running costs clearly less CPU than scheduling would cost; a timeout,
  crash or patch it cannot reproduce exactly keeps the static selection. A test of five seconds or more is never
  run here: it is kept unless every cheaper selected test passed and each changed spot that selected it was also
  exercised by at least three of the tests that ran.
- Selected tests are packed longest-first onto 1 to 8 workers, whichever count costs least.

## Rebuild the map

    uv venv --python 3.13 .venv && uv pip install -p .venv -r requirements.lock coverage
    git clone https://github.com/tobymao/sqlglot sqlglot-src && git -C sqlglot-src checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
    .venv/bin/python build/iso.py sqlglot-src sample/tests.json 8
    .venv/bin/python build/mkmap.py sample/tests.json map.bin

## Local score (no Docker)

    .venv/bin/python evalsel.py

## Checking recall

`gen/mutate.py` writes varied patches against the pinned checkout and `gen/label.py` finds the tests each one
really breaks by running the suite. They are used only to measure the selector; nothing from them ships in the
image and no patch or kind of edit is looked up anywhere.

## Pre-existing code

The organisers' starter kit (`score.py`, `sample/`, `requirements.lock`, `SCORING.md`), sqlglot, and
coverage.py (map building only). The selector itself uses only the Python standard library.

Written during the event with AI assistance.

## License

MIT
