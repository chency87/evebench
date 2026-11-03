from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class XDataConfig:
    root_path: str
    default_connection_string: str | None = None
    java_command: str = "java"
    python_command: str = sys.executable
    # XData writes fixed scratch files such as /tmp/dummy, so concurrent runs on
    # one host must serialize their JVMs through this lock.
    lock_path: str = os.path.join(tempfile.gettempdir(), "evebench-xdata.lock")
    backend_version: str = "a860311"
    adapter_version: str = "1"
