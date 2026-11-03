from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SPESConfig:
    jar_path: str
    library_path: str
    working_directory: str
    java_command: str = "java"
    backend_version: str = "1.0"
    adapter_version: str = "2"
