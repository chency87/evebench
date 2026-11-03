from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VeriEQLConfig:
    root_path: str
    z3_library_path: str | None = None
    python_command: str = sys.executable
    default_bound_size: int = 2
    backend_version: str = "1.0"
    adapter_version: str = "2"
