import re

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """Break an over-long paragraph on sentence boundaries (hard-cut as a last resort)."""
    pieces, current = [], ""
    for sentence in _SENTENCE_SPLIT.split(paragraph):
        while len(sentence) > max_chars:  # a single enormous sentence
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if current and len(current) + 1 + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def chunk_text(text: str, max_chars: int = 700) -> list[str]:
    """Split text into chunks of roughly <= max_chars.

    Strategy: split on blank lines (paragraphs), then pack neighbouring paragraphs together
    until the size limit. Keeping whole paragraphs means each chunk reads as a coherent answer.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    for p in paragraphs:
        units.extend(_split_long(p, max_chars) if len(p) > max_chars else [p])

    chunks, current = [], ""
    for unit in units:
        if current and len(current) + 2 + len(unit) > max_chars:
            chunks.append(current)
            current = unit
        else:
            current = f"{current}\n\n{unit}".strip()
    if current:
        chunks.append(current)
    return chunks
