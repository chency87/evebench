from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SQLSolverConfig:
    jar_path: str
    library_path: str
    working_directory: str
    backend_version: str = "1.1.0"
    adapter_version: str = "2"
