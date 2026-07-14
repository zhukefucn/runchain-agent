"""Deterministic, stdlib-only private authorization demo Skill."""

import json
import sys


def main() -> None:
    data = json.loads(sys.stdin.buffer.read())
    result = {
        "mock": True,
        "capability": "private-demo",
        "message": str(data.get("message", "专属技能调用成功")),
    }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
