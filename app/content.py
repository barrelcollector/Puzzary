from __future__ import annotations


THEMES: dict[str, list[str]] = {
    "daily_stripe": ["MERIDIAN", "PUZZLE", "ZEBRA", "PRINT", "CLUE", "PAPER", "HINT", "SOLVE"],
    "home": ["PORCH", "PANTRY", "LAUNDRY", "GARAGE", "KITCHEN", "GARDEN", "SHELF"],
    "weather": ["CLOUD", "RAIN", "SUNNY", "STORM", "BREEZE", "HUMID", "RADAR"],
    "space": ["ORBIT", "COMET", "LUNAR", "ROCKET", "PLANET", "STAR", "ECLIPSE"],
    "technology": ["SERVER", "DOCKER", "PYTHON", "ROUTER", "SCRIPT", "SENSOR", "PRINT"],
}


def words_for_theme(theme: str, difficulty: str) -> list[str]:
    words = THEMES.get(theme, THEMES["daily_stripe"])
    count = {"easy": 5, "medium": 7, "hard": 8}.get(difficulty, 5)
    return words[:count]
