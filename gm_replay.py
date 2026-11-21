"""Replays tests/test_generator.py::TestGenerator::test_generator_memory one dialect at a time.

The test loops over every dialect: it builds 100 generators to warm up, then measures with tracemalloc how much
memory 1,000 more leave behind, and asserts that it is under 1,024 bytes. Each dialect is its own subtest, and one
failing subtest fails the test.
"""
import gc, tracemalloc


def names():
    from sqlglot.dialects import Dialects
    return [d.value for d in Dialects]


def run(name):
    """The number the test asserts on for one dialect: bytes left behind by 1,000 generators."""
    from sqlglot.dialects import Dialect, Dialects
    dialect = Dialect.get_or_raise(Dialects(name))
    for _ in range(100):
        dialect.generator()
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(1000):
            dialect.generator()
        gc.collect()
        after = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    return after - before


def expected(out):
    """Growth and CPU cost of every dialect on the tree that is on sys.path (the pinned one, at build time)."""
    import json, time
    growth, cost = [], []
    for n in names():
        t0 = time.process_time(); growth.append(run(n)); cost.append(round(time.process_time() - t0, 3))
    with open(out, "w") as fh:
        json.dump({"names": names(), "growth": growth, "cost": cost}, fh)


if __name__ == "__main__":
    import sys
    expected(sys.argv[1])
