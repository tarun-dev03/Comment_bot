import json
import random
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "comment_templates.json"


class MessageGenerator:
    """Non-repetitive messages via shuffled fixed phrases + templated slots."""

    def __init__(self, custom_phrases: list[str] | None = None):
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)

        self.fixed: list[str] = list(data.get("fixed_phrases", []))
        self.templates: list[str] = list(data.get("templates", []))
        self.word_banks: dict[str, list[str]] = data.get("word_banks", {})

        if custom_phrases:
            self.fixed.extend(p.strip() for p in custom_phrases if p.strip())

        self._fixed_queue: list[str] = []
        self._recent: deque[str] = deque(maxlen=min(50, max(10, len(self.fixed) + len(self.templates))))

    def _refill_fixed_queue(self) -> None:
        self._fixed_queue = self.fixed.copy()
        random.shuffle(self._fixed_queue)

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
        return tpl.strip()[:200]

    def _accept(self, text: str) -> bool:
        t = text.strip().lower()
        if not t:
            return False
        return t not in self._recent

    def next_message(self) -> str:
        for _ in range(40):
            if random.random() < 0.45 and self.fixed:
                if not self._fixed_queue:
                    self._refill_fixed_queue()
                candidate = self._fixed_queue.pop()
            elif self.templates:
                candidate = self._from_template()
            elif self.fixed:
                if not self._fixed_queue:
                    self._refill_fixed_queue()
                candidate = self._fixed_queue.pop()
            else:
                candidate = "Hello live chat"

            candidate = candidate.strip()[:200]
            if self._accept(candidate):
                self._recent.append(candidate.lower())
                return candidate

        # Fallback: append random suffix to force uniqueness
        base = random.choice(self.fixed) if self.fixed else "Hello live chat"
        suffix = random.randint(1000, 9999)
        msg = f"{base} #{suffix}"[:200]
        self._recent.append(msg.lower())
        return msg
