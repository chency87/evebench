from __future__ import annotations

from collections.abc import Iterable

from evebench.domain.ports import EquivalenceChecker


class CheckerRegistry:
    def __init__(self, checkers: Iterable[EquivalenceChecker] = ()) -> None:
        self._checkers: dict[str, EquivalenceChecker] = {}
        for checker in checkers:
            self.register(checker)

    def register(self, checker: EquivalenceChecker) -> None:
        checker_id = checker.descriptor.checker.checker_id
        if checker_id in self._checkers:
            raise ValueError(f"checker already registered: {checker_id}")
        self._checkers[checker_id] = checker

    def get(self, checker_id: str) -> EquivalenceChecker:
        return self._checkers[checker_id]

    def all(self) -> tuple[EquivalenceChecker, ...]:
        return tuple(self._checkers.values())

    def ids(self) -> tuple[str, ...]:
        return tuple(self._checkers)
