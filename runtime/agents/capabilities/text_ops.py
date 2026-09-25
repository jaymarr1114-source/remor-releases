"""
swarm_engine/agents/capabilities/text_ops.py

Real text analysis. Nothing here is faked or hardcoded — output depends
entirely on the input text.
"""
import re
from collections import Counter

_WORD_RE = re.compile(r"[A-Za-z']+")


def analyze(text: str) -> dict:
    words = _WORD_RE.findall(text.lower())
    counts = Counter(words)
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    return {
        "char_count": len(text),
        "word_count": len(words),
        "sentence_count": len(sentences),
        "unique_words": len(counts),
        "top_words": counts.most_common(5),
        "avg_word_length": round(sum(len(w) for w in words) / len(words), 2) if words else 0.0,
    }
