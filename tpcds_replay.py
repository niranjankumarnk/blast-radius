"""Replays, query by query, what tests/test_executor.py::TestExecutor::test_execute_tpcds does with sqlglot.

For each TPC-DS query the test parses it, sends the expression to a worker process, runs it there with sqlglot's
Python executor, transpiles the query to DuckDB SQL in the test process, runs that in DuckDB and compares the two
results. Everything sqlglot contributes to the comparison is the DuckDB SQL string and the executor's result table,
so a patched tree that reproduces both for a query cannot change the test's verdict on that query.
"""
import ast, csv, enum, gzip, hashlib, operator, pickle


def open_file(file_name):
    with open(file_name, "rb") as f:
        gzipped = f.read(2) == b"\x1f\x8b"
    return gzip.open(file_name, "rt", newline="") if gzipped else open(file_name, encoding="utf-8", newline="")


class _Stop(BaseException):
    pass


class Replay:
    def __init__(self, sample=1, pickled=None):
        """sample=k keeps every k-th row of the larger tables: the same queries on less data, for a cheaper run."""
        from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs
        self.sample = sample
        self.pickled = pickle.load(open(pickled, "rb")) if pickled else None      # {table: pickle of the Table}, see expected()
        self.dir = FIXTURES_DIR + "/optimizer/tpc-ds/"
        self.schema = TPCDS_SCHEMA
        self.sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
        self.tables, self.worker, self.snapshot, self._schema = {}, {}, {}, None

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

    def worker_table(self, name):
        """The worker process's copy of a table (the test pickles every table to its workers once)."""
        if name not in self.worker:
            if self.pickled and name in self.pickled:                   # the same pickle, made when the image was built
                self.worker[name] = pickle.loads(self.pickled[name])
            else:
                self.worker[name] = pickle.loads(pickle.dumps(self.table(name)))
            self.snapshot[name] = list(self.worker[name].rows)
        return self.worker[name]

    def run(self, i):
        """What the test's verdict on query i depends on, as a digest."""
        from sqlglot import find_tables, parse_one, transpile
        from sqlglot.executor import execute
        meta, sql, _ = self.sqls[i]
        expression = pickle.loads(pickle.dumps(parse_one(sql)))             # (the test pickles it for the worker)
        if not meta.get("execute"):
            return "parsed"
        tables = {n: self.worker_table(n) for n in sorted({t.name for t in find_tables(expression)})}      # (the test's own order is arbitrary)
        schema = self.worker_schema()
        table = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=tables)))
        duck = transpile(sql, write="duckdb")[0]
        return hashlib.sha256(repr((duck, tuple(table.columns), table.rows)).encode()).hexdigest()

    def worker_schema(self):
        if self._schema is None: self._schema = pickle.loads(pickle.dumps(self.schema))
        return self._schema

    def compile(self, i):
        """A digest of everything query i's data phase is given, without running it: the DuckDB SQL, and the plan and
        executor that sqlglot.executor.execute hands to PythonExecutor.execute. A patch that changes no code of the
        data phase and reproduces this digest cannot change the query's result."""
        from sqlglot import find_tables, parse_one, transpile
        from sqlglot.executor import execute
        from sqlglot.executor.python import PythonExecutor
        meta, sql, _ = self.sqls[i]
        expression = pickle.loads(pickle.dumps(parse_one(sql)))
        if not meta.get("execute"):
            return "parsed"
        tables = {n: self.worker_table(n) for n in sorted({t.name for t in find_tables(expression)})}      # (the test's own order is arbitrary)
        seen = []
        def construct(executor, tables=None, env=None):                     # (building the executor counts as data phase)
            seen.append(self.canon((tables, env)))
        def capture(executor, plan, outer_scope=None):
            seen.append(self.canon((plan, outer_scope)))
            raise _Stop()
        original = PythonExecutor.__init__, PythonExecutor.execute
        PythonExecutor.__init__, PythonExecutor.execute = construct, capture
        try:
            try:
                execute(expression, schema=self.worker_schema(), tables=tables)
                seen.append("returned")                                     # (the data phase was never entered)
            except _Stop:
                pass
        finally:
            PythonExecutor.__init__, PythonExecutor.execute = original
        self.last = seen
        duck = transpile(sql, write="duckdb")[0]
        return hashlib.sha256(repr((duck, seen)).encode()).hexdigest()

    def canon(self, o, refs=None):
        """A canonical text of an object graph that uses no sqlglot method: class names, attributes and containers."""
        refs = {} if refs is None else refs
        out = []; add = out.append; plain = (bool, int, float, str, bytes); skip = _SKIP; slots = _slots
        def walk(o):
            if o is None or type(o) in plain: add(repr(o)); return
            t = type(o)
            if t is list or t is tuple:
                add("[")
                for x in o: walk(x); add(",")
                add("]"); return
            if t is dict:
                add("{")
                for k, v in o.items(): walk(k); add(":"); walk(v); add(",")
                add("}"); return
            if isinstance(o, plain): add(repr(o)); return
            if isinstance(o, enum.Enum): add(t.__name__ + "." + o.name); return
            if isinstance(o, type): add("<class " + o.__module__ + "." + o.__qualname__ + ">"); return
            if isinstance(o, (list, tuple, dict)): walk(list(o.items()) if isinstance(o, dict) else list(o)); return
            if isinstance(o, (set, frozenset)): add("{" + ",".join(sorted(self.canon(x, dict(refs)) for x in o)) + "}"); return
            cls = t.__module__ + "." + t.__qualname__
            if callable(o) and not hasattr(o, "args"):
                owner = getattr(o, "__self__", None)
                add("<callable " + str(getattr(o, "__module__", "")) + "." + getattr(o, "__qualname__", cls) + (" of " + type(owner).__qualname__ if owner is not None and not isinstance(owner, type(csv)) else "") + ">"); return
            i = id(o)
            if i in refs: add("<seen " + str(refs[i]) + ">"); return
            refs[i] = len(refs)
            if cls == "sqlglot.executor.table.Table":                       # rows: the very tuples that were loaded, in order
                name = next((n for n, tb in self.worker.items() if tb is o), None)
                snap = self.snapshot.get(name)
                same = snap is not None and len(o.rows) == len(snap) and all(map(operator.is_, o.rows, snap))
                add("<Table " + str(name) + " "); walk(o.columns); walk(o.column_range)
                add(" rows=" + ("loaded" if same else hashlib.sha256(repr(o.rows).encode()).hexdigest()) + ">"); return
            add("<" + cls + " ")
            done = None
            for k in slots(t):
                if k not in skip and hasattr(o, k): add(k + "="); walk(getattr(o, k)); add(",")
            d = getattr(o, "__dict__", None)
            if d:
                for k, v in d.items():
                    if k not in skip: add(k + "="); walk(v); add(",")
            add(">")
        walk(o)
        return "".join(out)


_SKIP = frozenset(("parent", "arg_key", "index", "_hash", "dependents", "_dag", "__weakref__", "__dict__"))
_SLOTS = {}


def _slots(t):
    if t not in _SLOTS:
        names = []
        for klass in t.__mro__:
            sl = klass.__dict__.get("__slots__", ())
            for k in (sl,) if isinstance(sl, str) else sl:
                if k not in names: names.append(k)
        _SLOTS[t] = tuple(names)
    return _SLOTS[t]


def expected(out, sample=1):
    """Digest, CPU cost and tables of every query on the tree that is on sys.path (the pinned one, at build time)."""
    import json, time
    from sqlglot import find_tables, parse_one
    r = Replay(sample); digest, cost, cdigest, ccost, needs, load = [], [], [], [], [], {}
    for i, (meta, sql, _) in enumerate(r.sqls):
        names = sorted({t.name for t in find_tables(parse_one(sql))}) if meta.get("execute") else []
        for n in names:
            if n not in load:
                t0 = time.process_time(); r.worker_table(n); load[n] = round(time.process_time() - t0, 3)
        t0 = time.process_time(); cdigest.append(r.compile(i)); ccost.append(round(time.process_time() - t0, 3))
        t0 = time.process_time(); digest.append(r.run(i)); cost.append(round(time.process_time() - t0, 3)); needs.append(names)
    with open(out, "w") as fh:
        json.dump({"digest": digest, "cost": cost, "cdigest": cdigest, "ccost": ccost, "tables": needs, "load": load}, fh)
    with open(out + ".tables", "wb") as fh:                                  # what the test's workers are sent
        pickle.dump({n: pickle.dumps(r.table(n)) for n in load}, fh)
    return digest


if __name__ == "__main__":
    import sys
    expected(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1)
