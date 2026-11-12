from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from num2words import num2words


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("\ufeff", "").replace("\u00ad", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    value = re.sub(r"[^\S\n]+", " ", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def guess_language(text: str) -> str:
    letters = re.findall(r"[a-zа-яё]", text.lower())
    return "ru" if sum("а" <= c <= "я" or c == "ё" for c in letters) > len(letters) / 3 else "en"


def parse_dictionary(value: str) -> dict[str, str]:
    result = {}
    for line in value.splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise ValueError("Словарь: одна замена на строку в формате слово = произношение")
        source, target = (p.strip() for p in line.split("=", 1))
        if not source or not target or len(source) > 150 or len(target) > 300:
            raise ValueError("Проверьте длину и обе стороны замены в словаре")
        result[source] = target
    if len(result) > 500:
        raise ValueError("В словаре можно сохранить до 500 замен")
    return result


def prepare_text(text: str, language: str, dictionary: dict, numbers: bool = False) -> str:
    text = clean_text(text)
    if dictionary:
        # One pass: replacements do not recursively replace each other.
        pattern = "|".join(re.escape(x) for x in sorted(dictionary, key=len, reverse=True))
        text = re.sub(r"(?<!\w)(?:" + pattern + r")(?!\w)", lambda m: dictionary[m[0]], text)
    if numbers:
        # Only unambiguous standalone integers. Dates, decimals, versions and units stay intact.
        text = re.sub(
            r"(?<![\w.,:/+−-])\d{1,12}(?![\w.,:/-])",
            lambda m: num2words(int(m[0]), lang=language),
            text,
        )
    return text


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    text: str
    pause: float


def chunk_text(text: str, limit: int = 480) -> list[Chunk]:
    """Return a gap-free partition with original offsets, preferably at sentence boundaries."""
    if not text.strip():
        return []
    chunks = []
    start = 0
    while start < len(text):
        ceiling = min(start + limit, len(text))
        end = ceiling
        if ceiling < len(text):
            candidates = list(re.finditer(r"[.!?…][\"»”')]*\s+|\n\s*\n", text[start:ceiling]))
            if candidates and candidates[-1].end() >= limit // 3:
                end = start + candidates[-1].end()
            else:
                spaces = list(re.finditer(r"\s+", text[start:ceiling]))
                if spaces:
                    end = start + spaces[-1].end()
                else:
                    # An unusually long word is kept whole rather than losing characters.
                    next_space = re.search(r"\s+", text[ceiling:])
                    end = ceiling + next_space.end() if next_space else len(text)
        segment = text[start:end]
        pause = 0.5 if re.search(r"\n\s*\n\s*$", segment) else 0.18
        chunks.append(Chunk(start, end, segment, pause))
        start = end
    return chunks
