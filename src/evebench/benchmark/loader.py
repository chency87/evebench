"""Read benchmark cases, preserving SQL and optional metadata as supplied."""

from __future__ import annotations

import csv
from pathlib import Path

COLUMNS = ("dbid", "index", "schema_ddl", "q1", "q2", "dialect", "ground_truth")


def read_dataset(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        missing = set(COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"dataset is missing columns: {', '.join(sorted(missing))}")
        return list(reader)
