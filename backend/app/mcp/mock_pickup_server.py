from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field


server = FastMCP("pickup-planner")


@server.tool(structured_output=True)
def plan_pickup(
    arrival_time: Annotated[
        str,
        Field(
            min_length=20,
            max_length=40,
            pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
        ),
    ],
    station: Literal[
        "南京南站",
        "南京站",
        "南京禄口国际机场",
    ],
    guest_count: Annotated[int, Field(ge=1, le=50)],
) -> dict[str, object]:
    """Return a deterministic mock pickup plan for a supported Nanjing station."""
    try:
        parsed_arrival = datetime.fromisoformat(arrival_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("arrival_time must be a valid ISO timestamp") from exc
    if parsed_arrival.tzinfo is None or parsed_arrival.utcoffset() is None:
        raise ValueError("arrival_time must include a timezone offset")
    return {
        "mock": True,
        "arrival_time": arrival_time,
        "station": station,
        "guest_count": guest_count,
        "vehicle": "商务中巴（Mock）" if guest_count > 6 else "七座商务车（Mock）",
        "driver": {"name": "王师傅（Mock）", "phone": "仅演示，不可拨打"},
        "meeting_point": f"{station}到达层指定接待点（Mock）",
        "timeline": [
            {"offset_minutes": -30, "event": "车辆到达接待点"},
            {"offset_minutes": -10, "event": "接待人员举牌等候"},
            {"offset_minutes": 20, "event": "预计完成集合并出发"},
        ],
    }


if __name__ == "__main__":
    server.run(transport="stdio")
