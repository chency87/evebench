from typing import Annotated

from fastapi import APIRouter, Depends, Query

from evebench.api.dependencies import get_result_store
from evebench.benchmark import ResultStore

router = APIRouter(prefix="/v1/benchmarks", tags=["benchmarks"])


@router.get("/summary")
async def benchmark_summary(
    store: Annotated[ResultStore, Depends(get_result_store)],
    dataset: Annotated[str | None, Query()] = None,
    run_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[dict[str, object]]:
    """Return outcome counts and ratios for the latest run of each dataset."""
    return store.summary_dicts(dataset=dataset, run_id=run_id)


@router.get("/unsupported-reasons")
async def benchmark_unsupported_reasons(
    store: Annotated[ResultStore, Depends(get_result_store)],
    run_id: Annotated[int, Query(ge=1)],
) -> list[dict[str, object]]:
    """Return unsupported reason counts for one benchmark run."""
    return store.unsupported_reasons(run_id=run_id)
