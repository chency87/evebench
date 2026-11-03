# EveBench

EveBench evaluates SQL query-equivalence cases with a common interface for
[SQLSolver](https://github.com/SJTU-IPADS/SQLSolver),
[SPES](https://github.com/georgia-tech-db/spes),
[VeriEQL](https://github.com/VeriEQL/VeriEQL),
[XData](https://gitlab.com/xdata/xdatapublic), and
[ParSEval](https://github.com/sfu-db/ParSEval).

## What is included

| Checker ID | Backend source | Preparation | Example source dialects |
| --- | --- | --- | --- |
| `sqlsolver` | `baselines/SQLSolver` submodule | Build a fat JAR | PostgreSQL, MySQL, SQLite |
| `spes` | `baselines/spes` submodule | Build an executable fat JAR | PostgreSQL, MySQL, SQLite |
| `qed` | `baselines/qed` | Build the parser JAR and Rust prover | PostgreSQL, MySQL, SQLite |
| `verieql` | `baselines/VeriEQL` submodule | No compilation | MySQL/MariaDB, PostgreSQL, Oracle |
| `xdata` | `baselines/xdata` submodule | No compilation; needs a PostgreSQL server | PostgreSQL, MySQL, SQLite |
| `parseval` | Python wheel pinned in `pyproject.toml` | Installed by `uv sync` | `sqlite` |
| `llm` | OpenAI-compatible Chat Completions endpoint | Set environment variables | `sqlite` |

The default benchmark profile is
`configs/benchmarks/bird_baseline.toml`. It evaluates 1,432 BIRD query pairs
with all four adapters.

## Prerequisites

The supported development environment is the included VS Code dev container.
It provides Python 3.11, `uv`, Java 17, and the Gradle versions needed by the
Java adapters.

For a host installation, provide:

- Linux x86-64 (the checked-in Z3 native libraries are Linux x86-64 binaries);
- Python 3.11 or later and `uv`;
- a Java 17 JDK; and
- Git with submodule support.

macOS and Windows users should use the dev container so that SQLSolver and SPES
can load the bundled native libraries. Node.js is present in the container but
is not required to build or run EveBench.

## Initialize the repository

Clone EveBench and all adapter submodules in one command:

```sh
git clone --recurse-submodules https://github.com/sfu-db/evebench.git evebench
cd evebench
```

### Install Python dependencies

From the repository root:

```sh
uv sync --frozen
cp .env.example .env
```

`uv sync` installs EveBench, ParSEval, and the Python packages used by the
VeriEQL adapter. VeriEQL does not need a `.pth` file: EveBench adds its
submodule root to the isolated VeriEQL subprocess automatically.

## Compile the adapters

Build SQLSolver first. SPES compiles against the Z3 Java and native libraries
stored in `baselines/SQLSolver/lib`.

### 1. SQLSolver

```sh
cd baselines/SQLSolver
./gradlew fatJar
cd ../..
```

The adapter expects:

```text
baselines/SQLSolver/build/libs/sqlsolver-v1.1.0.jar
baselines/SQLSolver/lib/libz3.so
baselines/SQLSolver/lib/libz3java.so
```

EveBench loads the JAR through JPype and calls
`sqlsolver.api.entry.Verification`; it does not invoke SQLSolver's CLI.

### 2. SPES

```sh
cd baselines/spes
./gradlew clean build
cd ../..
```

The resulting executable fat JAR is:

```text
baselines/spes/build/libs/equitas-1.0-SNAPSHOT.jar
```

EveBench launches this JAR as a child JVM and points it at
`baselines/SQLSolver/lib` for Z3.

### 3. VeriEQL and ParSEval

These adapters do not have a separate compile step:

- VeriEQL runs from `baselines/VeriEQL` in an isolated Python subprocess.
- ParSEval is installed from its pinned wheel by `uv sync`.

VeriEQL uses its vendored Python Z3 wrapper with the native Z3 libraries under
`baselines/SQLSolver/lib`.

### 4. XData

XData runs unmodified from `baselines/xdata`. EveBench's runner
(`evebench.adapters.xdata.runner`) writes each pair in XData's benchmark
layout, overrides the `XData.properties` bundled in `xdata.jar` by placing a
generated copy earlier on the classpath, and starts `test.RegressionTests`
with the bundled Z3. XData generates datasets on a PostgreSQL server, so
create the database and users from `baselines/xdata/installer/xdata_db.sql`
and set:

```sh
EVEBENCH_XDATA_CONNECTION_STRING=postgresql://testuser:Testuser%40123@127.0.0.1:5432/xdatadb
```

XData kills mutants rather than proving equivalence: a confirmed kill is
`not_equivalent`, and a surviving pair is `unknown`. XData writes fixed files
such as `/tmp/dummy`, so concurrent XData runs on one host are serialized.

## Benchmark dataset format

EveBench datasets are UTF-8 CSV files with one query-equivalence case per row.
The loader requires exactly these columns (column order is unrestricted):

| Column | Description |
| --- | --- |
| `dbid` | Identifier of the database schema used by the case. |
| `index` | Case identifier within `dbid`; EveBench records the case as `dbid:index`. |
| `schema_ddl` | Complete authoritative DDL used to create the schema. Multiple statements are separated by semicolons. |
| `q1` | First SQL query in the pair. |
| `q2` | Second SQL query in the pair. |
| `dialect` | Source SQL dialect for both schema and queries, e.g. `postgresql` or `sqlite`. |
| `ground_truth` | Reference equivalence label, normally `EQ` or `NEQ`. |

The header is therefore:

```text
dbid,index,schema_ddl,q1,q2,dialect,ground_truth
```

The `(dbid, index)` pair must be unique. SQL and DDL must use normal CSV
quoting when they contain commas, quotes, or newlines. Benchmark execution
uses `schema_ddl`; each adapter then performs its own dialect-specific
compilation. Unknown columns, empty values, duplicate cases, and labels other
than `EQ`/`NEQ` are rejected. No schema discovery or legacy fallback is performed.
`ground_truth` is persisted with the case for evaluation; it is not shown to
the verifier.

## Configure EveBench

The repository-relative defaults work after the setup above. `.env.example`
documents every override; copy it to `.env` and edit only the values you need.
Use absolute paths for custom adapter installations.

| Setting | Default |
| --- | --- |
| `EVEBENCH_SQLSOLVER_JAR` | `baselines/SQLSolver/build/libs/sqlsolver-v1.1.0.jar` |
| `EVEBENCH_SQLSOLVER_LIB` | `baselines/SQLSolver/lib` |
| `EVEBENCH_SQLSOLVER_WORKDIR` | `baselines/SQLSolver` |
| `EVEBENCH_SPES_JAR` | `baselines/spes/build/libs/equitas-1.0-SNAPSHOT.jar` |
| `EVEBENCH_SPES_LIB` | `baselines/SQLSolver/lib` |
| `EVEBENCH_SPES_WORKDIR` | `baselines/spes` |
| `EVEBENCH_QED_PARSER_JAR` | `baselines/qed/parser/target/qed-parser-1.0-SNAPSHOT-jar-with-dependencies.jar` |
| `EVEBENCH_QED_PROVER` | `baselines/qed/prover/target/release/qed-prover` |
| `EVEBENCH_QED_JAVA` | `$JAVA19_HOME/bin/java`, falling back to `EVEBENCH_JAVA` or `java` |
| `EVEBENCH_VERIEQL_ROOT` | `baselines/VeriEQL` |
| `EVEBENCH_VERIEQL_Z3_LIB` | `baselines/SQLSolver/lib` |
| `EVEBENCH_VERIEQL_BOUND_SIZE` | `2` |
| `EVEBENCH_XDATA_ROOT` | `baselines/xdata` |
| `EVEBENCH_XDATA_CONNECTION_STRING` | unset; required unless passed per run |
| `EVEBENCH_RESULTS_DB` | `data/evebench.sqlite` |
| `EVEBENCH_MAX_WORKERS` | `4` |
| `EVEBENCH_MAX_TASKS_PER_WORKER` | `25` |

Additional overrides include `EVEBENCH_JAVA`,
`EVEBENCH_VERIEQL_PYTHON`, `EVEBENCH_XDATA_JAVA`, `EVEBENCH_XDATA_PYTHON`, and the logging settings described in
`.env.example`.

The optional `llm` checker is registered when `EVEBENCH_LLM_MODEL` is set.
Configure `EVEBENCH_LLM_BASE_URL` for the provider and either
`EVEBENCH_LLM_API_KEY` or `OPENAI_API_KEY` when authentication is required.
Set `EVEBENCH_LLM_REASONING_EFFORT` to use reasoning-mode request parameters;
when it is omitted, the adapter uses ordinary completion parameters. The
default counterexample prompt can be replaced per run with
`backends.llm.options.prompt`.

### LLM verifier profile

Configure the endpoint in `.env`:

```dotenv
EVEBENCH_LLM_MODEL=your-model-name
EVEBENCH_LLM_BASE_URL=https://api.openai.com/v1
EVEBENCH_LLM_API_KEY=your-api-key
EVEBENCH_LLM_REASONING_EFFORT=medium
EVEBENCH_LLM_MAX_COMPLETION_TOKENS=4096
```

For a local endpoint that does not require authentication, leave
`EVEBENCH_LLM_API_KEY` empty. Add `llm` to the benchmark profile's `checkers`
array and give it a backend table:

```toml
checkers = ["sqlsolver", "spes", "verieql", "parseval", "llm"]

[backends.llm]

[backends.llm.options]
reasoning_effort = "medium"
max_completion_tokens = 4096
```

## Run the benchmark

Start with a small end-to-end run:

```sh
uv run --env-file .env python -m evebench benchmark --limit 10
```

Provide a dataset CSV or `--config`; no dataset is selected implicitly.
The example profile is
`configs/benchmarks/bird_baseline.toml`. That profile fixes:

- the generated BIRD dataset and SQLite result paths;
- checker order and backend options;
- the timeout, semantics, seed, and worker-pool settings; and
- VeriEQL bounds and ParSEval generation options.

At completion the CLI prints a summary like:

```text
backend       evaluated      eq     neq  unknown  timeout
sqlsolver            10   ...     ...      ...      ...
spes                 10   ...     ...      ...      ...
verieql              10   ...     ...      ...      ...
parseval              10   ...     ...      ...      ...

SQLite results: /path/to/evebench/data/evebench.sqlite (run 1)
```

For a one-off CSV, pass its path positionally:

```sh
uv run --env-file .env python -m evebench benchmark path/to/cases.csv
```

Custom datasets should follow the canonical CSV format described above.

## View the results

The benchmark command always prints the four-way per-backend summary. It also
stores the complete run, cases, normalized results, diagnostics, timings,
metrics, raw backend output, and available witnesses in
`data/evebench.sqlite` by default.

Start the EveBench API with the same environment:

```sh
uv run --env-file .env python -m evebench
```

Then open:

- workbench: <http://localhost:8000/>
- benchmark leaderboard: <http://localhost:8000/leaderboard>
- OpenAPI documentation: <http://localhost:8000/docs>

In the workbench, select the source dialect once; adapters own target compilation.
ParSEval requires a matching backend URL such as
`sqlite:////tmp/evebench-case.db`. ParSEval 1.x uses an in-memory scratch
database for SQLite and a uniquely named scratch database for PostgreSQL or
MySQL, then removes that scratch database after the attempt.

The leaderboard reads the latest persisted run for each dataset from
`EVEBENCH_RESULTS_DB`. Results already committed by an active benchmark become
visible immediately after refreshing the page.
