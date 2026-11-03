from __future__ import annotations


GENERAL_WORDS = ["MERIDIAN", "PUZZLE", "ZEBRA", "PRINT", "CLUE", "PAPER", "HINT", "SOLVE"]

THEMES: dict[str, list[str]] = {
    "general": GENERAL_WORDS,
    # Kept as a compatibility alias for existing Daily Stripe API clients.
    "daily_stripe": GENERAL_WORDS,
    "home": ["PORCH", "PANTRY", "LAUNDRY", "GARAGE", "KITCHEN", "GARDEN", "SHELF"],
    "weather": ["CLOUD", "RAIN", "SUNNY", "STORM", "BREEZE", "HUMID", "RADAR"],
    "space": ["ORBIT", "COMET", "LUNAR", "ROCKET", "PLANET", "STAR", "ECLIPSE"],
    "technology": ["SERVER", "DOCKER", "PYTHON", "ROUTER", "SCRIPT", "SENSOR", "PRINT"],
}


def words_for_theme(theme: str, difficulty: str) -> list[str]:
    words = THEMES.get(theme, THEMES["general"])
    count = {"easy": 5, "medium": 7, "hard": 8}.get(difficulty, 5)
    return words[:count]
