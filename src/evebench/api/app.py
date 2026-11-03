from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import importlib.metadata

from evebench.adapters import (
    CheckerRegistry,
    LLMConfig,
    ParsevalConfig,
    PooledChecker,
    QEDConfig,
    SPESConfig,
    SQLSolverConfig,
    VeriEQLConfig,
    XDataConfig,
    llm_descriptor,
    parseval_descriptor,
    qed_descriptor,
    spes_descriptor,
    sqlsolver_descriptor,
    verieql_descriptor,
    xdata_descriptor,
)
from evebench.api.routes import (
    benchmarks_router,
    checkers_router,
    checks_router,
)
from evebench.application import CheckService
from evebench.benchmark import ResultStore
from evebench.execution import BackendProcessPool
from evebench.observability import configure_logging, get_logger, log_context

logger = get_logger(__name__)


def default_registry(backend_pool: BackendProcessPool) -> CheckerRegistry:
    repository_root = Path(__file__).resolve().parents[3]
    default_jar = (
        repository_root
        / "baselines"
        / "SQLSolver"
        / "build"
        / "libs"
        / "sqlsolver-v1.1.0.jar"
    )
    jar_path = Path(os.environ.get("EVEBENCH_SQLSOLVER_JAR", default_jar))
    sqlsolver_root = repository_root / "baselines" / "SQLSolver"
    sqlsolver_config = SQLSolverConfig(
        jar_path=str(jar_path),
        library_path=os.environ.get(
            "EVEBENCH_SQLSOLVER_LIB",
            str(sqlsolver_root / "lib"),
        ),
        working_directory=os.environ.get(
            "EVEBENCH_SQLSOLVER_WORKDIR",
            str(sqlsolver_root),
        ),
    )
    spes_root = repository_root / "baselines" / "spes"
    spes_jar = (
        spes_root / "build" / "libs" / "equitas-1.0-SNAPSHOT.jar"
    )
    spes_config = SPESConfig(
        jar_path=os.environ.get("EVEBENCH_SPES_JAR", str(spes_jar)),
        library_path=os.environ.get(
            "EVEBENCH_SPES_LIB",
            str(sqlsolver_root / "lib"),
        ),
        working_directory=os.environ.get(
            "EVEBENCH_SPES_WORKDIR",
            str(spes_root),
        ),
        java_command=os.environ.get("EVEBENCH_JAVA", "java"),
    )
    qed_root = repository_root / "baselines" / "qed"
    java19_home = os.environ.get("JAVA19_HOME")
    qed_config = QEDConfig(
        parser_jar_path=os.environ.get(
            "EVEBENCH_QED_PARSER_JAR",
            str(
                qed_root / "parser" / "target"
                / "qed-parser-1.0-SNAPSHOT-jar-with-dependencies.jar"
            ),
        ),
        prover_path=os.environ.get(
            "EVEBENCH_QED_PROVER",
            str(qed_root / "prover/target/release/qed-prover"),
        ),
        java_command=os.environ.get(
            "EVEBENCH_QED_JAVA",
            (
                str(Path(java19_home) / "bin/java")
                if java19_home else os.environ.get("EVEBENCH_JAVA", "java")
            ),
        ),
    )
    verieql_root = repository_root / "baselines" / "VeriEQL"
    verieql_config = VeriEQLConfig(
        root_path=os.environ.get(
            "EVEBENCH_VERIEQL_ROOT",
            str(verieql_root),
        ),
        z3_library_path=os.environ.get(
            "EVEBENCH_VERIEQL_Z3_LIB",
            str(sqlsolver_root / "lib"),
        ),
        python_command=os.environ.get(
            "EVEBENCH_VERIEQL_PYTHON",
            os.environ.get("EVEBENCH_PYTHON", sys.executable),
        ),
        default_bound_size=int(
            os.environ.get("EVEBENCH_VERIEQL_BOUND_SIZE", "2")
        ),
    )
    xdata_config = XDataConfig(
        root_path=os.environ.get(
            "EVEBENCH_XDATA_ROOT",
            str(repository_root / "baselines" / "xdata"),
        ),
        default_connection_string=(
            os.environ.get("EVEBENCH_XDATA_CONNECTION_STRING") or None
        ),
        java_command=os.environ.get(
            "EVEBENCH_XDATA_JAVA",
            os.environ.get("EVEBENCH_JAVA", "java"),
        ),
        python_command=os.environ.get(
            "EVEBENCH_XDATA_PYTHON",
            os.environ.get("EVEBENCH_PYTHON", sys.executable),
        ),
    )
    parseval_config = ParsevalConfig(
        backend_version=importlib.metadata.version("parseval"),
    )
    checkers = [
        PooledChecker(qed_descriptor(qed_config), qed_config, backend_pool),
        PooledChecker(
            sqlsolver_descriptor(sqlsolver_config),
            sqlsolver_config,
            backend_pool,
        ),
        PooledChecker(
            spes_descriptor(spes_config),
            spes_config,
            backend_pool,
        ),
        PooledChecker(
            verieql_descriptor(verieql_config),
            verieql_config,
            backend_pool,
        ),
        PooledChecker(
            parseval_descriptor(parseval_config),
            parseval_config,
            backend_pool,
        ),
        PooledChecker(
            xdata_descriptor(xdata_config),
            xdata_config,
            backend_pool,
        ),
    ]
    llm_model = os.environ.get("EVEBENCH_LLM_MODEL", "").strip()
    if llm_model:
        llm_config = LLMConfig(
            model=llm_model,
            base_url=os.environ.get(
                "EVEBENCH_LLM_BASE_URL", "https://api.openai.com/v1"
            ),
            api_key=os.environ.get(
                "EVEBENCH_LLM_API_KEY", os.environ.get("OPENAI_API_KEY")
            ),
            reasoning_effort=(
                os.environ.get("EVEBENCH_LLM_REASONING_EFFORT") or None
            ),
            max_completion_tokens=int(
                os.environ.get("EVEBENCH_LLM_MAX_COMPLETION_TOKENS", "4096")
            ),
        )
        checkers.append(
            PooledChecker(
                llm_descriptor(llm_config),
                llm_config,
                backend_pool,
            )
        )
    return CheckerRegistry(checkers)


def create_app(
    registry: CheckerRegistry | None = None,
    *,
    backend_pool: BackendProcessPool | None = None,
    result_store: ResultStore | None = None,
) -> FastAPI:
    owns_pool = registry is None and backend_pool is None
    if registry is None:
        backend_pool = backend_pool or BackendProcessPool(
            max_workers=int(
                os.environ.get(
                    "EVEBENCH_MAX_WORKERS",
                    max(1, min(4, os.cpu_count() or 1)),
                )
            ),
            max_tasks=int(os.environ.get("EVEBENCH_MAX_TASKS_PER_WORKER", "25")),
        )
        registry = default_registry(backend_pool)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        configure_logging()
        logger.info(
            "EveBench API started",
            extra={
                "event": "api.started",
                "checker_ids": list(registry.ids()),
            },
        )
        try:
            yield
        finally:
            if owns_pool and backend_pool is not None:
                await asyncio.to_thread(backend_pool.stop)
            logger.info("EveBench API stopped", extra={"event": "api.stopped"})

    app = FastAPI(
        title="EveBench",
        version="0.1.0",
        description="Run SQL query-equivalence checkers through a common API.",
        lifespan=lifespan,
    )
    app.state.backend_pool = backend_pool
    app.state.check_service = CheckService(registry)
    app.state.result_store = result_store or ResultStore(
        os.environ.get("EVEBENCH_RESULTS_DB", "data/evebench.sqlite")
    )

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        supplied_id = request.headers.get("x-request-id", "")
        request_id = (
            supplied_id
            if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied_id)
            else uuid.uuid4().hex
        )
        started = time.perf_counter()
        with log_context(request_id=request_id):
            logger.debug(
                "HTTP request started",
                extra={
                    "event": "http.request_started",
                    "http_method": request.method,
                    "http_path": request.url.path,
                },
            )
            try:
                response = await call_next(request)
            except Exception:
                logger.exception(
                    "HTTP request failed",
                    extra={
                        "event": "http.request_failed",
                        "http_method": request.method,
                        "http_path": request.url.path,
                        "duration_ms": round(
                            (time.perf_counter() - started) * 1000,
                            3,
                        ),
                    },
                )
                raise
            response.headers["x-request-id"] = request_id
            logger.info(
                "HTTP request completed",
                extra={
                    "event": "http.request_completed",
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": response.status_code,
                    "duration_ms": round(
                        (time.perf_counter() - started) * 1000,
                        3,
                    ),
                },
            )
            return response

    app.include_router(benchmarks_router)
    app.include_router(checkers_router)
    app.include_router(checks_router)

    web_root = Path(__file__).resolve().parents[1] / "web"
    app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def workbench() -> FileResponse:
        return FileResponse(web_root / "index.html")

    @app.get("/leaderboard", include_in_schema=False)
    async def leaderboard() -> FileResponse:
        return FileResponse(web_root / "leaderboard.html")

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
