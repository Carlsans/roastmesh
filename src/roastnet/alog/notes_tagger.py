"""Lightweight, rule-based keyword tagging of roastingnotes/cuppingnotes text.

Deliberately simple (stdlib `re`, no NLP/ML dependency) -- these tags are an
extra filterable/searchable signal alongside full-text search, not a
text-classification product.

Includes a basic negation guard (checks the few words immediately before a
match for "no"/"not"/"never"/"without"/"n't") -- real notes like "No
tipping, no scorching" (an explicitly GOOD outcome) would otherwise be
tagged "scorched, tipped". Still a known limitation: only catches negation
within a few words of the match.

Also normalizes literal two-character "\\n"/"\\r" sequences (backslash
followed by the letter n/r) to spaces before matching -- some tools export
notes into .alog files with a literal backslash+n embedded in the text
(not a parser bug -- confirmed via hex dump of a real file), which can
silently merge two words with no real whitespace between them and break
word-boundary-based negation detection right at that point.
"""
from __future__ import annotations

import re

_LITERAL_ESCAPE_RE = re.compile(r"\\[nr]")

TAG_KEYWORDS: dict[str, list[str]] = {
    "baked": [r"\bbaked\b", r"\bflat(?:\s|$)"],
    "tipped": [r"\btip(?:ped|ping)?\b"],
    "scorched": [r"\bscorch(?:ed|ing)?\b"],
    "stalled": [r"\bstall(?:ed|ing)?\b", r"\bflick\b"],
    "great": [r"\bgreat\b", r"\bexcellent\b", r"\bbest\b", r"\bdelicious\b"],
    "underdeveloped": [r"\bunder-?developed\b", r"\bsour\b", r"\bgrassy\b"],
    "smoky": [r"\bsmok(?:y|e|ey)\b", r"\bashy\b"],
    "cracked_early": [r"\bearly\s+(?:first\s+)?crack\b", r"\bfast\s+fc\b"],
    "uneven": [r"\buneven\b", r"\bquaker"],
}

_COMPILED = {
    tag: [re.compile(p, re.IGNORECASE) for p in patterns]
    for tag, patterns in TAG_KEYWORDS.items()
}

_NEGATION_RE = re.compile(r"\b(?:no|not|never|without|n't)\b", re.IGNORECASE)
_NEGATION_WINDOW_WORDS = 4


def _is_negated(text: str, match_start: int) -> bool:
    preceding_words = re.findall(r"\S+", text[:match_start])[-_NEGATION_WINDOW_WORDS:]
    return bool(_NEGATION_RE.search(" ".join(preceding_words)))


def tag_notes(*texts: str | None) -> list[str]:
    combined = " ".join(t for t in texts if t)
    if not combined:
        return []
    combined = _LITERAL_ESCAPE_RE.sub(" ", combined)
    tags = []
    for tag, patterns in _COMPILED.items():
        for pattern in patterns:
            match = pattern.search(combined)
            if match and not _is_negated(combined, match.start()):
                tags.append(tag)
                break
    return sorted(tags)
