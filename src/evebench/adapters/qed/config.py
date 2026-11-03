from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QEDConfig:
    parser_jar_path: str
    prover_path: str
    java_command: str = "java"
    backend_version: str = "0.1.0"
    adapter_version: str = "2"
