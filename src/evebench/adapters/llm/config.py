from __future__ import annotations

from .prompts import DEFAULT_PROMPT
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class LLMConfig:
    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = field(default=None, repr=False)
    prompt_template: str = DEFAULT_PROMPT
    adapter_version: str = "2"
    reasoning_effort: str | None = None
    max_completion_tokens: int = 4_096
    temperature: float | None = None
