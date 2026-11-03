from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsevalConfig:
    backend_version: str
    adapter_version: str = "4"
