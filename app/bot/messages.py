import json
import random
from collections import deque
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "comment_templates.json"
CONVERSATION_DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "conversation_chats.json"

STAGE_ORDER = [
    "1_how_are_you",
    "2_check_in_and_warmup",
    "3_topic_openers",
    "4_conversation_builders",
    "5_deepening_and_keeping_alive",
]

EMOJIS = ["", " 🔥", " ✨", " 👍", " 💯", " ❤️", " 🙌", " 😊", " 🚀", " 👏", " ⭐", " 😄", " 🎉"]
PUNCTUATIONS = ["", "!", ".", "!!", "...", "~"]
PREFIXES = ["", "", "", "hey, ", "wow, ", "yoo, ", "so ", "lol "]

_CONVERSATIONAL_OPENERS = (
    "hey",
    "hi",
    "hello",
    "how ",
    "what",
    "where",
    "who",
    "which",
    "wait",
    "honestly",
    "actually",
    "kaise",
    "namaste",
    "kya",
    "aaj",
    "sab",
    "chalo",
    "main ",
    "good ",
    "hope ",
    "doing ",
    "just ",
    "anyone",
    "curious",
    "quick ",
    "if ",
    "love ",
    "let's",
    "speaking",
    "reading",
    "glad ",
)


@lru_cache(maxsize=1)
def _load_static_databases() -> tuple[dict, dict]:
    with open(DATA_PATH, encoding="utf-8") as f:
        templates_data = json.load(f)

    conv_data: dict = {}
    if CONVERSATION_DATA_PATH.exists():
        with open(CONVERSATION_DATA_PATH, encoding="utf-8") as f:
            conv_data = json.load(f)
    return templates_data, conv_data


def get_conversation_db_stats() -> dict:
    """Return summary stats about the conversational chat database."""
    _, conv_data = _load_static_databases()
    stages = conv_data.get("stages", {})
    stage_counts = {k: len(v) for k, v in stages.items() if isinstance(v, list)}
    arcs = conv_data.get("conversation_arcs", [])
    templates = conv_data.get("templates", [])
    return {
        "stage_counts": stage_counts,
        "total_stage_phrases": sum(stage_counts.values()),
        "conversation_arcs": len(arcs),
        "conversation_templates": len(templates),
    }


def _ends_with_emoji(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    last_char = stripped[-1]
    return ord(last_char) > 0x2600 or stripped.endswith((":)", "^_^"))


class MessageGenerator:
    """Generates unique, randomized live chat comments with progressive conversation building."""

    def __init__(self, custom_phrases: list[str] | None = None):
        templates_data, conv_data = _load_static_databases()

        self.default_fixed: list[str] = list(templates_data.get("fixed_phrases", []))
        self.templates: list[str] = list(templates_data.get("templates", []))
        self.word_banks: dict[str, list[str]] = templates_data.get("word_banks", {})

        self.conv_stages: dict[str, list[str]] = {
            k: list(v)
            for k, v in conv_data.get("stages", {}).items()
            if isinstance(v, list)
        }
        self.conv_arcs: list[list[str]] = [
            list(arc)
            for arc in conv_data.get("conversation_arcs", [])
            if isinstance(arc, list) and arc
        ]
        self.conv_templates: list[str] = list(conv_data.get("templates", []))
        self.conv_word_banks: dict[str, list[str]] = conv_data.get("word_banks", {})

        self.custom_phrases: list[str] = []
        if custom_phrases:
            self.update_custom_phrases(custom_phrases)

        self._recent_set: set[str] = set()
        self._recent_deque: deque[str] = deque(maxlen=2000)

        self._turn_index: int = 0
        self._active_arc: list[str] | None = None
        self._arc_step: int = 0
        self._used_arc_indices: deque[int] = deque(maxlen=max(1, len(self.conv_arcs)))

    def update_custom_phrases(self, custom_phrases: list[str]) -> None:
        cleaned = [p.strip() for p in custom_phrases if p.strip()]
        self.custom_phrases = cleaned

    def _variate(self, text: str, allow_prefix: bool = True) -> str:
        """Add subtle random variations (prefix, punctuation, emoji) while preserving natural phrasing."""
        res = " ".join(text.strip().split())
        lower_res = res.lower()

        if allow_prefix and not lower_res.startswith(_CONVERSATIONAL_OPENERS) and random.random() < 0.20:
            prefix = random.choice([p for p in PREFIXES if p])
            if len(res) > 1 and not res.startswith(("I ", "I'")):
                res = prefix + res[0].lower() + res[1:]
            else:
                res = prefix + res

        # Preserve question marks and trailing emojis
        if not res.endswith("?") and not _ends_with_emoji(res) and random.random() < 0.45:
            punc = random.choice(PUNCTUATIONS)
            if punc:
                res = res.rstrip(".!~") + punc

        if not _ends_with_emoji(res) and random.random() < 0.38:
            emoji = random.choice([e for e in EMOJIS if e])
            res += emoji

        return " ".join(res.strip().split())

    def _fill_template(self, tpl: str, banks: dict[str, list[str]]) -> str:
        for key, words in banks.items():
            placeholder = "{" + key + "}"
            if placeholder in tpl and words:
                tpl = tpl.replace(placeholder, random.choice(words), 1)
        if "{" in tpl:
            tpl = tpl.split("{")[0].strip()
        now = datetime.now(UTC)
        tpl = tpl.replace("{time}", now.strftime("%H:%M"))
        return " ".join(tpl.strip().split())

    def _from_template(self) -> str:
        tpl = random.choice(self.templates)
        return self._fill_template(tpl, self.word_banks)

    def _from_conv_template(self) -> str:
        tpl = random.choice(self.conv_templates)
        return self._fill_template(tpl, self.conv_word_banks)

    def _stage_for_turn(self, turn_index: int) -> str:
        cycle_pos = turn_index % 20
        if cycle_pos <= 1:
            return "1_how_are_you"
        if cycle_pos <= 4:
            return "2_check_in_and_warmup"
        if cycle_pos <= 8:
            return "3_topic_openers"
        if cycle_pos <= 13:
            return "4_conversation_builders"
        return "5_deepening_and_keeping_alive"

    def _pick_new_arc(self) -> list[str] | None:
        if not self.conv_arcs:
            return None
        available = [i for i in range(len(self.conv_arcs)) if i not in self._used_arc_indices]
        if not available:
            self._used_arc_indices.clear()
            available = list(range(len(self.conv_arcs)))
        idx = random.choice(available)
        self._used_arc_indices.append(idx)
        return self.conv_arcs[idx]

    def _next_conversational_candidate(self) -> tuple[str, bool]:
        """Return (candidate_text, used_active_arc)."""
        cycle_pos = self._turn_index % 20

        if cycle_pos == 0 and self._active_arc is None and self.conv_arcs and random.random() < 0.75:
            self._active_arc = self._pick_new_arc()
            self._arc_step = 0

        if self._active_arc is not None and self._arc_step < len(self._active_arc):
            line = self._active_arc[self._arc_step]
            return self._variate(line, allow_prefix=False), True

        stage_name = self._stage_for_turn(self._turn_index)
        stage_pool = self.conv_stages.get(stage_name, [])

        if self.conv_templates and cycle_pos >= 2 and random.random() < 0.30:
            return self._variate(self._from_conv_template(), allow_prefix=False), False

        if stage_pool:
            return self._variate(random.choice(stage_pool), allow_prefix=False), False

        if self.conv_templates:
            return self._variate(self._from_conv_template(), allow_prefix=False), False

        return self._variate("Hey everyone, how are you doing today?", allow_prefix=False), False

    def _register(self, text: str, used_arc: bool = False) -> str:
        cleaned = text.strip()[:200]
        normalized = cleaned.lower()
        self._recent_set.add(normalized)
        self._recent_deque.append(normalized)
        if len(self._recent_set) > 2000:
            self._recent_set = set(self._recent_deque)
        self._turn_index += 1
        if used_arc and self._active_arc is not None:
            self._arc_step += 1
            if self._arc_step >= len(self._active_arc):
                self._active_arc = None
                self._arc_step = 0
        return cleaned

    def is_seen(self, text: str) -> bool:
        return text.strip().lower() in self._recent_set

    def next_message(self) -> str:
        has_conv_db = bool(self.conv_stages or self.conv_arcs or self.conv_templates)

        for attempt in range(60):
            used_arc = False
            in_active_arc = (
                self._active_arc is not None and self._arc_step < len(self._active_arc)
            )
            early_conv_turn = (self._turn_index % 20) < 5
            if self.custom_phrases and random.random() < 0.65:
                base = random.choice(self.custom_phrases)
                candidate = self._variate(base)
            elif has_conv_db and (early_conv_turn or in_active_arc or random.random() < 0.78):
                candidate, used_arc = self._next_conversational_candidate()
                if attempt > 5 and self._active_arc is not None:
                    self._active_arc = None
                    self._arc_step = 0
                    used_arc = False
            elif random.random() < 0.5 and self.templates:
                candidate = self._variate(self._from_template())
            elif self.custom_phrases or self.default_fixed:
                pool = self.custom_phrases + self.default_fixed
                base = random.choice(pool)
                candidate = self._variate(base)
            else:
                candidate = self._variate("Hey everyone, how are you doing today?", allow_prefix=False)

            if not self.is_seen(candidate):
                return self._register(candidate, used_arc=used_arc)

        all_conv = [p for plist in self.conv_stages.values() for p in plist]
        base_pool = self.custom_phrases or all_conv or self.default_fixed
        base = random.choice(base_pool) if base_pool else "Great stream"
        suffix_tag = random.randint(100, 9999)
        return self._register(f"{base[:190]} #{suffix_tag}", used_arc=False)
