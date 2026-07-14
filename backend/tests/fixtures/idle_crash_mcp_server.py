from __future__ import annotations

import os
from threading import Timer

from mcp.server.fastmcp import FastMCP


server = FastMCP("idle-crash-fixture")


@server.tool(structured_output=True)
def alive() -> dict[str, object]:
    return {"alive": True, "pid": os.getpid()}


if __name__ == "__main__":
    Timer(1.0, lambda: os._exit(92)).start()
    server.run(transport="stdio")
