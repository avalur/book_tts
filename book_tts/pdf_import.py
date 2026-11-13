"""Local PDF text/OCR extraction with resumable page caches and chapter provenance."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader, mult
from pypdf.errors import PyPdfError

from .config import MAX_TEXT
from .importers import Book, Chapter
from .text import clean_text

EXTRACTION_VERSION = 1
MAX_PAGES = 2000


def write_cache(path: Path, content: bytes):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".page-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class PdfLine:
    text: str
    x: float
    y: float
    right: float
    bottom: float
    confidence: float = 100.0
    paragraph: str = ""


@dataclass
class PdfPage:
    number: int
    lines: list[PdfLine] = field(default_factory=list)
    method: str = "text"
    confidence: float | None = None


def parse_tsv(tsv: str, page_number: int) -> PdfPage:
    rows = list(csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE))
    root = next((r for r in rows if r["level"] == "1"), None)
    if root is None:
        raise ValueError(f"OCR не вернул размеры страницы {page_number}")
    width, height = max(1, int(root["width"])), max(1, int(root["height"]))
    groups = defaultdict(list)
    for row in rows:
        if row["level"] == "5" and row.get("text", "").strip() and float(row["conf"]) >= 0:
            key = (row["block_num"], row["par_num"], row["line_num"])
            groups[key].append(row)
    lines = []
    confidences = []
    for key, words in groups.items():
        left = min(int(w["left"]) for w in words)
        top = min(int(w["top"]) for w in words)
        right = max(int(w["left"]) + int(w["width"]) for w in words)
        bottom = max(int(w["top"]) + int(w["height"]) for w in words)
        confidence = sum(float(w["conf"]) * len(w["text"]) for w in words) / sum(
            len(w["text"]) for w in words
        )
        confidences.extend((float(w["conf"]), len(w["text"])) for w in words)
        lines.append(
            PdfLine(
                " ".join(w["text"] for w in words),
                left / width,
                top / height,
                right / width,
                bottom / height,
                confidence,
                ":".join(key[:2]),
            )
        )
    mean = (
        sum(c * n for c, n in confidences) / sum(n for _, n in confidences) if confidences else None
    )
    return PdfPage(page_number, sorted(lines, key=lambda line: (line.y, line.x)), "ocr", mean)


def text_page(page, number: int) -> PdfPage:
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    lines = []

    def visitor(text, cm, tm, font, size):
        matrix = mult(tm, cm)
        x, y = matrix[4] / width, 1 - matrix[5] / height
        for index, value in enumerate(text.splitlines()):
            if value.strip():
                top = y + (index - 1) * size * 1.2 / height
                lines.append(
                    PdfLine(
                        value.strip(),
                        x,
                        top,
                        min(1, x + len(value) * size * 0.5 / width),
                        top + size / height,
                    )
                )

    page.extract_text(0, visitor_text=visitor)
    # Consecutive fragments on the same baseline belong to one visual line.
    grouped = []
    for line in sorted(lines, key=lambda line: (round(line.y, 3), line.x)):
        if grouped and abs(grouped[-1].y - line.y) < 0.004:
            previous = grouped[-1]
            previous.text += " " + line.text
            previous.right = max(previous.right, line.right)
        else:
            grouped.append(line)
    return PdfPage(number, grouped)


def ocr_page(source: Path, number: int, language: str, render_size: int) -> PdfPage:
    environment = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    with tempfile.TemporaryDirectory(prefix="book-tts-ocr-") as temp:
        prefix = Path(temp) / "page"
        try:
            subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    str(number),
                    "-l",
                    str(number),
                    "-scale-to",
                    str(render_size),
                    "-gray",
                    "-singlefile",
                    "-png",
                    str(source),
                    str(prefix),
                ],
                capture_output=True,
                check=True,
                timeout=120,
            )
            result = subprocess.run(
                ["tesseract", str(prefix) + ".png", "stdout", "-l", language, "--psm", "6", "tsv"],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as error:
            detail = (
                error.stderr.decode(errors="replace")
                if isinstance(error.stderr, bytes)
                else error.stderr
            )
            raise ValueError(f"Ошибка OCR страницы {number}: {detail[-600:]}") from error
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                f"Истекло время OCR страницы {number}; готовые страницы сохранены"
            ) from error
    return parse_tsv(result.stdout, number)


def extract_pages(
    raw: bytes,
    cache: Path,
    *,
    ocr="auto",
    language="eng",
    workers=4,
    render_size=4000,
    progress=None,
) -> tuple[list[PdfPage], dict]:
    if ocr not in {"auto", "always", "never"} or language not in {"eng", "rus", "eng+rus"}:
        raise ValueError("Неверный режим OCR или язык распознавания")
    if not 1000 <= render_size <= 4000:
        raise ValueError("Длинная сторона изображения OCR должна быть от 1000 до 4000 пикселей")
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF защищён паролем. Загрузите незашифрованный файл")
        count = len(reader.pages)
        if count > MAX_PAGES:
            raise ValueError(f"Лимит PDF — {MAX_PAGES} страниц")
        metadata = {
            "title": str((reader.metadata or {}).get("/Title", "")).strip(),
            "author": str((reader.metadata or {}).get("/Author", "")).strip(),
        }
    except PyPdfError as error:
        raise ValueError(f"Не удалось прочитать PDF: {error}") from error
    source_hash = hashlib.sha256(raw).hexdigest()
    ocr_version = ""
    if shutil.which("tesseract"):
        ocr_version = subprocess.run(
            ["tesseract", "--version"], capture_output=True, text=True, timeout=10
        ).stdout.splitlines()[0]
    identity = f"{source_hash}:{EXTRACTION_VERSION}:{ocr}:{language}:{render_size}:{ocr_version}"
    directory = cache / hashlib.sha256(identity.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "source.pdf"
    if not source.exists():
        write_cache(source, raw)
    pages = {}
    pending = []
    for index, page in enumerate(reader.pages, 1):
        cached = directory / f"{index:04d}.json"
        if cached.exists():
            try:
                data = json.loads(cached.read_text())
                if data["number"] != index:
                    raise ValueError("Номер сохранённой страницы не совпадает")
                pages[index] = PdfPage(
                    data["number"],
                    [PdfLine(**line) for line in data["lines"]],
                    data["method"],
                    data["confidence"],
                )
                continue
            except (ValueError, KeyError, TypeError):
                pass
        extracted = text_page(page, index) if ocr != "always" else PdfPage(index)
        meaningful = sum(sum(c.isalpha() for c in line.text) for line in extracted.lines)
        resources = page.get("/Resources", {})
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        objects = resources.get("/XObject", {}) if resources else {}
        objects = objects.get_object() if hasattr(objects, "get_object") else objects
        has_images = any(
            obj.get_object().get("/Subtype") in {"/Image", "/Form"} for obj in objects.values()
        )
        if (ocr == "always" or (meaningful < 20 and has_images)) and ocr != "never":
            pending.append(index)
        else:
            pages[index] = extracted
            write_cache(cached, json.dumps(asdict(extracted), ensure_ascii=False).encode("utf-8"))
        if sum(len(line.text) for p in pages.values() for line in p.lines) > MAX_TEXT:
            raise ValueError("Лимит книги — 5 миллионов символов")
    if pending:
        missing = [name for name in ("tesseract", "pdftoppm") if not shutil.which(name)]
        if missing:
            raise ValueError(
                "Для сканированного PDF установите Tesseract и Poppler (macOS: brew install tesseract poppler)"
            )
        available = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10
        ).stdout.splitlines()
        if not all(code in available for code in language.split("+")):
            raise ValueError(
                f"Не установлен язык Tesseract {language}. На macOS: brew install tesseract-lang"
            )
        with ThreadPoolExecutor(max_workers=min(max(workers, 1), 8)) as pool:
            futures = {pool.submit(ocr_page, source, n, language, render_size): n for n in pending}
            for future in as_completed(futures):
                page = future.result()
                pages[page.number] = page
                destination = directory / f"{page.number:04d}.json"
                write_cache(
                    destination, json.dumps(asdict(page), ensure_ascii=False).encode("utf-8")
                )
                if progress:
                    progress(len(pages), count, page.number)
    if progress:
        progress(len(pages), count, 0)
    if sum(len(line.text) for p in pages.values() for line in p.lines) > MAX_TEXT:
        raise ValueError("Лимит книги — 5 миллионов символов")
    return [pages[n] for n in sorted(pages)], {
        **metadata,
        "source_sha256": source_hash,
        "page_count": count,
        "ocr_language": language,
        "ocr_version": ocr_version,
        "render_long_edge": render_size,
        "ocr_pages": sum(p.method == "ocr" for p in pages.values()),
        "cache": str(directory),
    }


def roman_number(value: str) -> int | None:
    value = value.upper()
    if not value or not re.fullmatch(
        r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", value
    ):
        return None
    digits = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for i, char in enumerate(value):
        total += (
            -digits[char]
            if i + 1 < len(value) and digits[char] < digits[value[i + 1]]
            else digits[char]
        )
    return total if 0 < total <= 500 else None


NUMBER_WORDS = {
    name: i
    for i, name in enumerate(
        (
            "",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
            "twenty",
        )
    )
}


def heading_number(text: str) -> tuple[int | None, bool]:
    value = text.strip()
    if len(value) > 120 or re.search(r"\.{2,}|\bpage\s+\d+\s*$", value, re.I):
        return None, False
    explicit = re.fullmatch(
        r"(?:chapter|chap\.?|глава)\s+([\dIVXLCDM]+|[a-z]+)(?:\s*[:.\-–—]\s*.+)?", value, re.I
    )
    token = explicit[1] if explicit else value.rstrip(" °.,)")
    number = (
        int(token)
        if re.fullmatch(r"\d{1,3}", token)
        else NUMBER_WORDS.get(token.lower())
        if explicit
        else None
    )
    if number is None:
        number = roman_number(token)
    return number, bool(explicit)


def strip_margins(pages: list[PdfPage]) -> tuple[list[PdfPage], list[dict]]:
    appearances = defaultdict(set)
    offsets = Counter()
    for page in pages:
        for line in page.lines:
            if line.y < 0.12 or line.y > 0.87:
                signature = clean_text(line.text).casefold()
                if not re.fullmatch(r"[\dIVXLCDM .-]+", signature, re.I) and len(signature) < 140:
                    appearances[signature].add(page.number)
                numeric = re.fullmatch(
                    r"[^A-Za-zА-Яа-я\d]*(\d{1,4})[^A-Za-zА-Яа-я\d]*", line.text.strip()
                )
                if numeric:
                    offsets[int(numeric[1]) - page.number] += 1
    repeated = {
        s for s, numbers in appearances.items() if len(numbers) >= max(3, len(pages) * 0.12)
    }
    dominant_offset, dominant_count = offsets.most_common(1)[0] if offsets else (0, 0)
    removed = []
    result = []
    for page in pages:
        kept = []
        for line in page.lines:
            numeric = re.fullmatch(
                r"[^A-Za-zА-Яа-я\d]*(\d{1,4})[^A-Za-zА-Яа-я\d]*", line.text.strip()
            )
            number = int(numeric[1]) if numeric else None
            token = re.sub(r"[^A-Za-z\d]", "", line.text)
            expected = str(page.number + dominant_offset)
            fuzzy_footer = (
                dominant_count >= 3
                and line.y > 0.94
                and any(c.isdigit() for c in token)
                and len(token) == len(expected)
                and sum(a != b for a, b in zip(token, expected, strict=True)) <= 1
            )
            page_number = number is not None and (
                line.y > 0.95
                or ((line.y < 0.08 or line.y > 0.87) and offsets[number - page.number] >= 3)
            )
            running = (line.y < 0.12 or line.y > 0.87) and clean_text(
                line.text
            ).casefold() in repeated
            noise = page.method == "ocr" and bool(re.fullmatch(r"[|_~\\]+", line.text.strip()))
            if page_number or fuzzy_footer or running or noise:
                removed.append(
                    {
                        "page": page.number,
                        "text": line.text,
                        "reason": "page_number"
                        if page_number or fuzzy_footer
                        else "ocr_edge_noise"
                        if noise
                        else "running_header",
                    }
                )
            else:
                kept.append(line)
        result.append(PdfPage(page.number, kept, page.method, page.confidence))
    return result, removed


@lru_cache(maxsize=1)
def english_words():
    import cmudict

    return set(cmudict.words())


def join_lines(lines: list[tuple[int, PdfLine]], changes: list[dict]) -> str:
    """Reflow visual wraps, retain paragraph indents and conservative hard hyphens."""
    if not lines:
        return ""
    lefts = sorted(line.x for _, line in lines if len(line.text) > 40)
    margin = lefts[len(lefts) // 10] if lefts else min(line.x for _, line in lines)
    output = []
    previous = None
    for page, line in lines:
        value = clean_text(line.text).translate(
            str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"})
        )
        if not value:
            continue
        if previous is None:
            output.append(value)
        else:
            prev_page, prev = previous
            page_break = page != prev_page
            typical_height = max(0.008, min(prev.bottom - prev.y, line.bottom - line.y))
            indented = line.x > margin + 0.018
            large_gap = not page_break and line.y - prev.bottom > typical_height * 1.05
            paragraph = indented or large_gap
            tail = re.search(r"([A-Za-z]+)-$", output[-1])
            head = re.match(r"([a-z]+)\b", value)
            if tail and head and not paragraph:
                combined = (tail[1] + head[1]).lower()
                compound = (tail[1] + "-" + head[1]).lower()
                if combined in english_words() and compound not in english_words():
                    output[-1] = output[-1][:-1] + value
                    changes.append(
                        {"page": page, "from": tail[1] + "- " + head[1], "to": tail[1] + head[1]}
                    )
                else:
                    output[-1] += value
                    changes.append(
                        {
                            "page": page,
                            "from": tail[1] + "- " + head[1],
                            "to": tail[1] + "-" + head[1],
                            "review": compound not in english_words(),
                        }
                    )
            elif paragraph:
                output.append(value)
            else:
                output[-1] += " " + value
        previous = (page, line)
    return "\n\n".join(output)


def chapters_from_pages(pages: list[PdfPage], info: dict, filename: str) -> Book:
    cleaned, removed = strip_margins(pages)
    candidates = []
    toc_pages = set()
    for page in cleaned:
        if any(
            re.fullmatch(
                r"contents|table of contents|оглавление|содержание", line.text.strip(), re.I
            )
            for line in page.lines[:6]
        ):
            toc_pages.add(page.number)
            continue
        if page.confidence is not None and page.confidence < 75:
            continue
        for index, line in enumerate(page.lines):
            number, explicit = heading_number(line.text)
            if number is None:
                continue
            next_line = page.lines[index + 1] if index + 1 < len(page.lines) else None
            previous = page.lines[index - 1] if index else None
            isolated = next_line is not None and next_line.y - line.bottom >= 0.02
            before_gap = previous is None or line.y - previous.bottom >= 0.025
            enough_text = sum(len(item.text) for item in page.lines) >= 150
            if explicit or (line.y < 0.93 and isolated and before_gap and enough_text):
                candidates.append((page.number, index, number, explicit))
    markers = []
    warnings = []
    for page, index, number, explicit in candidates:
        expected = markers[-1][2] + 1 if markers else 1
        if (
            number == expected
            or (markers and expected < number <= expected + 2)
            or (not markers and explicit)
        ):
            if number != expected:
                warnings.append(
                    f"Страница {page}: найдена глава {number}, ожидалась {expected}. Проверьте границу глав"
                )
            markers.append((page, index, number, explicit))
    title = info.get("title") or Path(filename).stem
    book = Book(title, info.get("author", ""), chapters=[])
    spans = []
    changes = []
    all_lines = [
        (page.number, index, line) for page in cleaned for index, line in enumerate(page.lines)
    ]
    positions = {(page, index): pos for pos, (page, index, _) in enumerate(all_lines)}
    heading_positions = [positions[(p, i)] for p, i, _, _ in markers]
    starts = []
    part_labels = []
    for pos in heading_positions:
        start = pos
        labels = []
        for previous in range(pos - 1, max(-1, pos - 4), -1):
            value = all_lines[previous][2].text.strip()
            part = bool(re.fullmatch(r"(?:book|part|книга|часть)\s+[\w]+", value, re.I))
            uppercase = value.isupper() and len(value) <= 80 and sum(c.isalpha() for c in value) > 4
            if not (part or uppercase):
                break
            start = previous
            labels.insert(0, value)
        starts.append(start)
        part_labels.append(" — ".join(labels))
    end_position = len(all_lines)
    if markers:
        for page in cleaned:
            if page.number <= markers[-1][0]:
                continue
            noise_page = (
                page.method == "ocr"
                and page.confidence is not None
                and page.confidence < 60
                and not any(sum(c.isalpha() for c in line.text) >= 15 for line in page.lines)
            )
            following = [p for p in cleaned if p.number > page.number]
            if noise_page and all(
                p.confidence is not None and p.confidence < 85 for p in following
            ):
                end_position = next(
                    (i for i, (p, _, _) in enumerate(all_lines) if p >= page.number), len(all_lines)
                )
                break
    if starts and starts[0]:
        front = all_lines[: starts[0]]
        book.chapters.append(
            Chapter(
                "Перед началом книги",
                join_lines([(p, line) for p, _, line in front], changes),
                included=False,
            )
        )
        spans.append(
            {
                "kind": "front_matter",
                "pdf_start_page": front[0][0],
                "pdf_end_page": front[-1][0],
                "source_line_start": 0,
                "source_line_end": starts[0],
            }
        )
    active_part = ""
    for k, (page, index, number, explicit) in enumerate(markers):
        end = starts[k + 1] if k + 1 < len(starts) else end_position
        body = all_lines[heading_positions[k] + 1 : end]
        # A new part heading before the next chapter belongs to that next chapter.
        chapter_title = f"{'Глава' if info.get('ocr_language') == 'rus' else 'Chapter'} {number}"
        if explicit and len(cleaned[page - 1].lines[index].text) > len(chapter_title):
            chapter_title = cleaned[page - 1].lines[index].text
        if part_labels[k]:
            active_part = part_labels[k]
        text = (
            (part_labels[k] + "\n\n" if part_labels[k] else "")
            + chapter_title
            + "\n\n"
            + join_lines([(p, line) for p, _, line in body], changes)
        )
        book.chapters.append(Chapter(chapter_title, text))
        spans.append(
            {
                "kind": "chapter",
                "number": number,
                "part": active_part,
                "raw_heading": cleaned[page - 1].lines[index].text,
                "source_line_start": starts[k],
                "source_line_end": end,
                "pdf_start_y": round(cleaned[page - 1].lines[index].y, 4),
                "pdf_start_page": page,
                "pdf_end_page": body[-1][0] if body else page,
            }
        )
    if markers and end_position < len(all_lines):
        back = all_lines[end_position:]
        book.chapters.append(
            Chapter(
                "После основного текста",
                join_lines([(p, line) for p, _, line in back], changes),
                included=False,
            )
        )
        spans.append(
            {
                "kind": "back_matter",
                "pdf_start_page": back[0][0],
                "pdf_end_page": back[-1][0],
                "source_line_start": end_position,
                "source_line_end": len(all_lines),
            }
        )
    if not markers:
        text = join_lines([(p, line) for p, _, line in all_lines], changes)
        book.chapters = [Chapter("Текст книги", text)]
        spans = [
            {
                "kind": "unsegmented",
                "pdf_start_page": 1,
                "pdf_end_page": len(pages),
                "source_line_start": 0,
                "source_line_end": len(all_lines),
            }
        ]
        warnings.append("Надёжные заголовки глав не найдены: текст сохранён одним разделом")
    if not any(sum(c.isalpha() for c in chapter.text) > 30 for chapter in book.chapters):
        raise ValueError("В PDF не найден читаемый текст. Для скана включите OCR")
    review = [
        {
            "page": page.number,
            "confidence": round(page.confidence, 1),
            "low_confidence_lines": sum(line.confidence < 75 for line in page.lines),
        }
        for page in pages
        if page.method == "ocr" and page.confidence is not None and page.confidence < 85
    ]
    book.import_report = {
        **{k: v for k, v in info.items() if k != "cache"},
        "chapter_count": len(markers),
        "chapters": spans,
        "warnings": warnings,
        "removed_margins": removed,
        "hyphenation_changes": changes,
        "review_pages": review,
        "toc_pages": sorted(toc_pages),
        "method": "ocr" if info.get("ocr_pages") else "text",
        "retained_line_count": len(all_lines),
        "notice": "OCR может ошибаться в словах. Уверенность Tesseract не является оценкой точности. Проверьте границы глав и текст перед озвучкой.",
    }
    return book


def import_pdf(
    raw: bytes,
    filename: str,
    *,
    cache: Path | None = None,
    ocr="auto",
    language="eng",
    workers=4,
    render_size=4000,
    progress=None,
) -> Book:
    if cache is None:
        from .config import Settings

        cache = Settings.load().data / "pdf-cache"
    pages, info = extract_pages(
        raw,
        cache,
        ocr=ocr,
        language=language,
        workers=workers,
        render_size=render_size,
        progress=progress,
    )
    book = chapters_from_pages(pages, info, filename)
    from .text import guess_language

    book.language = guess_language(" ".join(c.text[:1000] for c in book.chapters if c.included))
    return book
