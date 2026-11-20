"""Reading order for digital book PDFs, including drop capitals and linked note markers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache

from pypdf import mult

from .pdf_types import PdfLine, PdfPage
from .text import clean_text


@lru_cache(maxsize=1)
def english_words():
    import cmudict

    return set(cmudict.words())


def read_bookmarks(reader) -> list[dict]:
    entries = []

    def walk(nodes, depth=0):
        for node in nodes:
            if isinstance(node, list):
                walk(node, depth + 1)
                continue
            try:
                index = reader.get_destination_page_number(node)
                if index is None or not 0 <= index < len(reader.pages):
                    continue
                height = float(reader.pages[index].mediabox.height)
                top = node.get("/Top")
                top = 1 - float(top) / height if top is not None else 0
                title = clean_text(str(node.title))
                if title:
                    entries.append(
                        {
                            "title": title,
                            "page": index + 1,
                            "y": max(0, min(1, top)),
                            "depth": depth,
                        }
                    )
            except (TypeError, ValueError, AttributeError):
                continue

    walk(reader.outline)
    return entries


@dataclass
class Span:
    text: str
    x: float
    baseline: float
    size: float
    continuation: bool = False


def text_page(page, number: int) -> PdfPage:
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    spans = []

    def visitor(text, cm, tm, font, size):
        matrix = mult(tm, cm)
        user_size = size * math.hypot(matrix[0], matrix[1])
        for index, value in enumerate(text.splitlines()):
            if value.strip():
                spans.append(Span(value, matrix[4], matrix[5] - index * user_size * 1.2, user_size))

    page.extract_text(0, visitor_text=visitor)
    weights = {}
    for span in spans:
        size = round(span.size, 2)
        weights[size] = weights.get(size, 0) + len(span.text)
    body_size = max(weights, key=weights.get) if weights else 12
    reference_rects = []
    for annotation in page.get("/Annots", []):
        annotation = annotation.get_object()
        action = annotation.get("/A", {})
        action = action.get_object() if hasattr(action, "get_object") else action
        internal = annotation.get("/Dest") is not None or action.get("/S") == "/GoTo"
        rect = annotation.get("/Rect")
        if annotation.get("/Subtype") == "/Link" and internal and rect:
            reference_rects.append([float(v) for v in rect])
    references = []
    kept = []
    for span in spans:
        marker = re.fullmatch(r"(?:fn)?\d{1,4}|[*†‡]", span.text.strip(), re.I)
        linked = any(
            x0 - 2 <= span.x <= x1 + 2 and y0 - 2 <= span.baseline <= y1 + 2
            for x0, y0, x1, y1 in reference_rects
        )
        if marker and linked and span.size <= body_size * 0.85:
            references.append(
                {
                    "text": span.text.strip(),
                    "x": round(span.x / width, 4),
                    "y": round(1 - span.baseline / height, 4),
                    "reason": "linked_note_marker",
                }
            )
        else:
            kept.append(span)
    spans = kept
    adjustments = []
    capitals = [
        s for s in spans if re.fullmatch(r"[A-ZА-ЯЁ]", s.text.strip()) and s.size >= body_size * 2.5
    ]
    for capital in capitals:
        candidates = [
            s
            for s in spans
            if s is not capital
            and s.size <= body_size * 1.4
            and capital.x < s.x < capital.x + capital.size * 1.5
            and capital.baseline - body_size * 0.2 <= s.baseline <= capital.baseline + capital.size
            and re.match(r"[a-zа-яё]", s.text.lstrip())
        ]
        if not candidates:
            continue
        first = max(candidates, key=lambda s: s.baseline)
        word = re.match(r"\w+", first.text.lstrip())[0]
        joined = capital.text.strip() + word
        separator = "" if joined.lower() in english_words() else " "
        first.text = capital.text.strip() + separator + first.text.lstrip()
        first.x = capital.x
        for span in spans:
            if (
                span is not first
                and span is not capital
                and span.size <= body_size * 1.4
                and capital.baseline - body_size * 0.2 <= span.baseline < first.baseline
                and capital.x < span.x < capital.x + capital.size * 1.5
            ):
                span.continuation = True
        spans.remove(capital)
        adjustments.append(
            {
                "kind": "drop_cap",
                "letter": capital.text.strip(),
                "word": capital.text.strip() + separator + word,
            }
        )
    groups = []
    for span in sorted(spans, key=lambda s: (-s.baseline, s.x)):
        if groups and abs(groups[-1][0].baseline - span.baseline) <= max(1, body_size * 0.12):
            groups[-1].append(span)
        else:
            groups.append([span])
    lines = []
    for group in groups:
        group.sort(key=lambda s: s.x)
        text = "".join(s.text for s in group)
        size = max(s.size for s in group)
        baseline = max(s.baseline for s in group)
        x = min(s.x for s in group)
        lines.append(
            PdfLine(
                clean_text(text),
                x / width,
                1 - (baseline + size) / height,
                min(1, (x + len(text) * size * 0.5) / width),
                1 - baseline / height,
                continuation=any(s.continuation for s in group),
            )
        )
    return PdfPage(number, lines, references=references, adjustments=adjustments)
