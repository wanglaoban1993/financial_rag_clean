# ============================================================
#  rag/history.py — keep unreliable earlier answers out of the next turn
#
#  The Orchestrator hands the last few turns to the specialists as context,
#  and also counts them as "evidence" for the grounding check. That is right
#  for a normal follow-up ("and what about SAP?"), but it also feeds back
#  whatever the assistant got wrong: an answer with invented figures becomes
#  both context the model repeats and evidence that vouches for those figures,
#  and a wrong "I can't read PDFs" is repeated as a fact.
#
#  So an earlier answer that failed (error) or that the grounding check
#  flagged is replaced, in the context only, by a short placeholder. The
#  user's own question is kept so the thread still makes sense. What the user
#  sees in the chat window is not touched.
# ============================================================

from __future__ import annotations

import re
from collections import deque

from langchain_core.messages import AIMessage, BaseMessage

# The dashboard may store the answer with the grounding footer appended.
_GROUNDING_FOOTER_RE = re.compile(r"⚠️\s*Not found in the retrieved sources", re.IGNORECASE)
_ERROR_RE = re.compile(r"^\s*\[\w+ error\]")

_KEY_CHARS = 300
_MAX_REMEMBERED = 200
_MIN_PREFIX_CHARS = 40

WITHHELD_ERROR = "[Earlier answer withheld: that request failed.]"
WITHHELD_UNVERIFIED = (
    "[Earlier answer withheld: it contained figures or details that could not be verified "
    "against the documents. Do not rely on or repeat it; look the information up again.]"
)


def _key(text: str) -> str:
    """Stable identity of an answer: whitespace-collapsed start of the text,
    cut before any grounding footer, so a client that appends a footer or a
    source list still matches the answer we generated."""
    text = str(text or "")
    footer = _GROUNDING_FOOTER_RE.search(text)
    if footer:
        text = text[:footer.start()]
    return re.sub(r"\s+", " ", text).strip()[:_KEY_CHARS]


def is_error_answer(text: str) -> bool:
    return bool(_ERROR_RE.match(str(text or "")))


class UntrustedAnswers:
    """Bounded memory of answers this process produced that should not be reused."""

    def __init__(self, limit: int = _MAX_REMEMBERED):
        self._keys: deque[str] = deque(maxlen=limit)

    def remember(self, answer: str) -> None:
        key = _key(answer)
        if key and key not in self._keys:
            self._keys.append(key)

    def __contains__(self, answer: str) -> bool:
        key = _key(answer)
        if not key:
            return False
        # Prefix match: the client may append a source list to a short answer.
        # Very short remembered answers must match exactly ("No." is not a prefix of everything).
        return any(key == k or (len(k) >= _MIN_PREFIX_CHARS and key.startswith(k)) for k in self._keys)


def sanitize_history(
    messages: list[BaseMessage], untrusted: UntrustedAnswers | None = None
) -> list[BaseMessage]:
    """`messages` with each unreliable assistant answer swapped for a placeholder.
    Reliable = not an error, no grounding footer in the stored text, and not
    remembered in `untrusted`. User messages always pass through."""
    cleaned: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, AIMessage):
            text = str(message.content)
            if is_error_answer(text):
                message = AIMessage(content=WITHHELD_ERROR)
            elif _GROUNDING_FOOTER_RE.search(text) or (untrusted is not None and text in untrusted):
                message = AIMessage(content=WITHHELD_UNVERIFIED)
        cleaned.append(message)
    return cleaned
