from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from evebench.api.dependencies import get_check_service
from evebench.api.schemas import CheckRequest, CheckResponse, CheckResultResponse
from evebench.application import CheckService
from evebench.application.check_service import UnknownCheckerError
from evebench.observability import log_context

router = APIRouter(prefix="/v1/checks", tags=["checks"])


@router.post("", response_model=CheckResponse)
async def run_check(
    request: CheckRequest,
    service: Annotated[CheckService, Depends(get_check_service)],
) -> CheckResponse:
    with log_context(case_id=request.case.case_id):
        try:
            results = await service.check(
                request.case.to_domain(),
                request.options.to_domain(),
                request.checker_ids,
            )
        except UnknownCheckerError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "unknown_checker",
                    "checker_ids": exc.checker_ids,
                },
            ) from exc
    return CheckResponse(
        results=[CheckResultResponse.from_domain(result) for result in results]
    )
