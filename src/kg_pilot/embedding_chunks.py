"""Keep the source spelling of symbols in token-limited embedding passages."""
from __future__ import annotations


def source_chunks(tokenizer, text: str) -> list[str]:
    # Decoding token IDs back to text can drop unknown glyphs or change units.
    # The pinned BGE fast tokenizer gives character offsets into the input.
    offsets = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True,
                        verbose=False)["offset_mapping"]
    if len(offsets) <= 480:
        return [text] if text.strip() else []
    chunks = []
    for start in range(0, len(offsets), 416):
        end = min(start + 480, len(offsets))
        left = offsets[start][0] if start else 0
        right = offsets[end - 1][1] if end < len(offsets) else len(text)
        chunks.append(text[left:right])
        if end == len(offsets):
            break
    return chunks
