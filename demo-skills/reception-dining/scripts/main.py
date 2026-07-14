"""Deterministic, stdlib-only mock dining recommendation Skill."""

import json
import sys


def main() -> None:
    data = json.loads(sys.stdin.buffer.read())
    guest_count = int(data["guest_count"])
    budget = float(data["budget"])
    preferences = sorted(str(value) for value in data.get("preferences", []))
    allergies = sorted(str(value) for value in data.get("allergies", []))
    per_guest = round(budget / guest_count, 2) if guest_count else 0
    result = {
        "mock": True,
        "guest_count": guest_count,
        "budget": budget,
        "preferences": preferences,
        "allergies": allergies,
        "recommendations": [
            {
                "venue": "迎宾厅·示例餐厅",
                "meal": "地方风味欢迎宴（Mock）",
                "estimated_per_guest": per_guest,
                "allergy_note": "已标记并将在演示中人工确认" if allergies else "无已知过敏项",
            },
            {
                "venue": "湖畔厅·示例餐厅",
                "meal": "清淡融合餐（Mock）",
                "estimated_per_guest": per_guest,
                "allergy_note": "仅为演示建议，不代表真实餐饮安全确认",
            },
        ],
    }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
