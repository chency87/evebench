from __future__ import annotations

from typing import Protocol

from .models import (
    CheckOptions,
    CheckResult,
    CheckerDescriptor,
    EquivalenceCase,
)


class EquivalenceChecker(Protocol):
    @property
    def descriptor(self) -> CheckerDescriptor: ...

    async def check(
        self,
        case: EquivalenceCase,
        options: CheckOptions,
    ) -> CheckResult: ...
