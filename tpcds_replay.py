"""Replays, query by query, what tests/test_executor.py::TestExecutor::test_execute_tpcds does with sqlglot.

For each TPC-DS query the test parses it, sends the expression to a worker process, runs it there with sqlglot's
Python executor, transpiles the query to DuckDB SQL in the test process, runs that in DuckDB and compares the two
results. Everything sqlglot contributes to the comparison is the DuckDB SQL string and the executor's result table,
so a patched tree that reproduces both for a query cannot change the test's verdict on that query.
"""
import ast, csv, gzip, hashlib, pickle


def open_file(file_name):
    with open(file_name, "rb") as f:
        gzipped = f.read(2) == b"\x1f\x8b"
    return gzip.open(file_name, "rt", newline="") if gzipped else open(file_name, encoding="utf-8", newline="")


class Replay:
    def __init__(self, sample=1):
        """sample=k keeps every k-th row of the larger tables: the same queries on less data, for a cheaper run."""
        from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs
        self.sample = sample
        self.dir = FIXTURES_DIR + "/optimizer/tpc-ds/"
        self.schema = TPCDS_SCHEMA
        self.sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
        self.tables = {}

    def table(self, name):
        """A table built from its csv exactly as the test's setUpClass builds it."""
        if name not in self.tables:
            from sqlglot.executor.table import Table
            columns = self.schema[name]
            reader = csv.reader(open_file(f"{self.dir}{name}.csv.gz"), delimiter="|")
            rows, ctypes = [], []
            next(reader)
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
            if self.sample > 1 and len(rows) > 3000: rows = rows[::self.sample]
            self.tables[name] = Table(columns=columns, rows=rows)
        return self.tables[name]

    def run(self, i):
        """What the test's verdict on query i depends on, as a digest."""
        from sqlglot import find_tables, parse_one, transpile
        from sqlglot.executor import execute
        meta, sql, _ = self.sqls[i]
        expression = pickle.loads(pickle.dumps(parse_one(sql)))             # (the test pickles it for the worker)
        if not meta.get("execute"):
            return "parsed"
        tables = {t.name: self.table(t.name) for t in find_tables(expression)}
        schema, tables = pickle.loads(pickle.dumps((self.schema, tables)))  # (the worker's copy)
        table = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=tables)))
        duck = transpile(sql, write="duckdb")[0]
        return hashlib.sha256(repr((duck, tuple(table.columns), table.rows)).encode()).hexdigest()


def expected(out, sample=1):
    """Digest, CPU cost and tables of every query on the tree that is on sys.path (the pinned one, at build time)."""
    import json, time
    from sqlglot import find_tables, parse_one
    r = Replay(sample); digest, cost, needs, load = [], [], [], {}
    for i, (meta, sql, _) in enumerate(r.sqls):
        names = sorted({t.name for t in find_tables(parse_one(sql))}) if meta.get("execute") else []
        for n in names:
            if n not in load:
                t0 = time.process_time(); r.table(n); load[n] = round(time.process_time() - t0, 3)
        t0 = time.process_time(); digest.append(r.run(i)); cost.append(round(time.process_time() - t0, 3)); needs.append(names)
    with open(out, "w") as fh:
        json.dump({"digest": digest, "cost": cost, "tables": needs, "load": load}, fh)
    return digest


if __name__ == "__main__":
    import sys
    expected(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1)
