from typing import Annotated

from fastapi import APIRouter, Depends

from evebench.api.dependencies import get_check_service
from evebench.api.schemas import CheckerDescriptorResponse
from evebench.application import CheckService

router = APIRouter(prefix="/v1/checkers", tags=["checkers"])


@router.get("", response_model=list[CheckerDescriptorResponse])
async def list_checkers(
    service: Annotated[CheckService, Depends(get_check_service)],
) -> list[CheckerDescriptorResponse]:
    return [
        CheckerDescriptorResponse.from_domain(checker.descriptor)
        for checker in service.registry.all()
    ]
