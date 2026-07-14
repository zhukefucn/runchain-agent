from __future__ import annotations

import os
import time
from typing import Literal

from mcp.server.fastmcp import FastMCP


server = FastMCP("fault-fixture")


@server.tool(structured_output=True)
def fault(mode: Literal["ok", "timeout", "crash", "business_error"]) -> dict[str, object]:
    if mode == "timeout":
        time.sleep(0.5)
    if mode == "crash":
        os._exit(91)
    if mode == "business_error":
        raise ValueError("fixture business error")
    return {"mock": True, "pid": os.getpid()}


@server.tool(structured_output=False)
def unstructured() -> str:
    return "fixture text"


if __name__ == "__main__":
    server.run(transport="stdio")
