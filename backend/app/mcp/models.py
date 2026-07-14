from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class McpTool:
    name: str
    description: str | None
    input_schema: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
