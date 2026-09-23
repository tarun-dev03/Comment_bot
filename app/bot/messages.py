import json
import random
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "comment_templates.json"

EMOJIS = ["", " 🔥", " ✨", " 👍", " 💯", " ❤️", " 🙌", " 😊", " 🚀", " 👏", " ⭐", " 😄", " 🎉"]
PUNCTUATIONS = ["", "!", ".", "!!", "...", "~"]
PREFIXES = ["", "", "", "hey, ", "wow, ", "yoo, ", "so ", "lol "]


class MessageGenerator:
    """Generates unique, highly randomized live chat comments prioritized from user-added phrases."""

    def __init__(self, custom_phrases: list[str] | None = None):
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)

        self.default_fixed: list[str] = list(data.get("fixed_phrases", []))
        self.templates: list[str] = list(data.get("templates", []))
        self.word_banks: dict[str, list[str]] = data.get("word_banks", {})

        self.custom_phrases: list[str] = []
        if custom_phrases:
            self.update_custom_phrases(custom_phrases)

        self._recent_set: set[str] = set()
        self._recent_deque: deque[str] = deque(maxlen=2000)

    def update_custom_phrases(self, custom_phrases: list[str]) -> None:
        cleaned = [p.strip() for p in custom_phrases if p.strip()]
        self.custom_phrases = cleaned

    def _variate(self, text: str) -> str:
        """Add subtle random variations (prefix, punctuation, emoji) to ensure uniqueness and randomness."""
        res = text.strip()

        # Optionally add a soft prefix (20% chance)
        if random.random() < 0.20:
            prefix = random.choice([p for p in PREFIXES if p])
            if len(res) > 1:
                res = prefix + res[0].lower() + res[1:]
            else:
                res = prefix + res

        # Strip existing trailing punctuation and apply random punctuation (50% chance)
        if random.random() < 0.50:
            punc = random.choice(PUNCTUATIONS)
            if punc:
                res = res.rstrip(".!~") + punc

        # Optionally add a random emoji (45% chance)
        if random.random() < 0.45:
            emoji = random.choice([e for e in EMOJIS if e])
            res += emoji

        return res.strip()

    def _from_template(self) -> str:
        tpl = random.choice(self.templates)
        for key, words in self.word_banks.items():
            placeholder = "{" + key + "}"
            if placeholder in tpl:
                tpl = tpl.replace(placeholder, random.choice(words), 1)
        if "{" in tpl:
            tpl = tpl.split("{")[0].strip()
        now = datetime.now(UTC)
        tpl = tpl.replace("{time}", now.strftime("%H:%M"))
        return tpl.strip()

    def _register(self, text: str) -> str:
        cleaned = text.strip()[:200]
        normalized = cleaned.lower()
        self._recent_set.add(normalized)
        self._recent_deque.append(normalized)
        if len(self._recent_set) > 2000:
            self._recent_set = set(self._recent_deque)
        return cleaned

    def is_seen(self, text: str) -> bool:
        return text.strip().lower() in self._recent_set

    def next_message(self) -> str:
        # Try up to 50 randomized candidates to guarantee uniqueness
        for _ in range(50):
            # Prioritize custom (added) phrases if available (75% chance)
            if self.custom_phrases and random.random() < 0.75:
                base = random.choice(self.custom_phrases)
                candidate = self._variate(base)
            elif random.random() < 0.5 and self.templates:
                candidate = self._variate(self._from_template())
            elif self.custom_phrases or self.default_fixed:
                pool = self.custom_phrases + self.default_fixed
                base = random.choice(pool)
                candidate = self._variate(base)
            else:
                candidate = self._variate("Great live stream")

            if not self.is_seen(candidate):
                return self._register(candidate)

        # Guaranteed unique fallback if all candidates were previously seen
        base_pool = self.custom_phrases if self.custom_phrases else self.default_fixed
        base = random.choice(base_pool) if base_pool else "Great stream"
        suffix_tag = random.randint(100, 9999)
        return self._register(f"{base} #{suffix_tag}")

