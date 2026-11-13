from __future__ import annotations

import base64
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote
from xml.etree.ElementTree import Element, tostring

from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from PIL import Image

from .config import MAX_TEXT
from .text import clean_text, guess_language


@dataclass
class Chapter:
    title: str
    text: str
    is_note: bool = False
    included: bool = True


@dataclass
class Book:
    title: str
    author: str = ""
    language: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    cover: bytes | None = None
    import_report: dict | None = None


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def elements(root, tag: str):
    return [e for e in root.iter() if local(e.tag) == tag]


def first_text(root, tag: str, fallback: str = "") -> str:
    return next((clean_text("".join(e.itertext())) for e in elements(root, tag)), fallback)


def image_bytes(raw: bytes) -> bytes:
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("Обложка слишком большая")
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 20_000_000:
            raise ValueError("Слишком большое разрешение обложки")
        image.thumbnail((1600, 1600))
        out = io.BytesIO()
        image.convert("RGB").save(out, "JPEG", quality=90)
        return out.getvalue()


def import_txt(raw: bytes, filename: str) -> Book:
    text = None
    encodings = (
        ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp1251")
    )
    for encoding in encodings:
        try:
            candidate = raw.decode(encoding)
            if "\x00" not in candidate:
                text = clean_text(candidate)
                break
        except UnicodeError:
            continue
    if text is None:
        raise ValueError("Не удалось прочитать кодировку TXT. Сохраните файл в UTF-8")
    heading = re.compile(
        r"^(?:глава|часть|chapter|part)\s+(?:[\dIVXLCDM]+|[а-яa-z]+)\b[^\n]*$", re.I | re.M
    )
    marks = list(heading.finditer(text))
    chapters = []
    if marks:
        if text[: marks[0].start()].strip():
            chapters.append(Chapter("Вступление", text[: marks[0].start()].strip()))
        for i, mark in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            chapters.append(Chapter(mark[0].strip(), text[mark.start() : end].strip()))
    else:
        chapters = [Chapter("Текст книги", text)]
    return Book(Path(filename).stem, chapters=chapters)


def import_fb2(raw: bytes, filename: str) -> Book:
    root = ET.fromstring(raw)
    if local(root.tag) != "FictionBook":
        raise ValueError("Файл не является FB2")
    info = next(iter(elements(root, "title-info")), root)
    authors = [
        clean_text(
            " ".join(first_text(a, tag) for tag in ("first-name", "middle-name", "last-name"))
        )
        for a in elements(info, "author")
    ]
    book = Book(
        first_text(info, "book-title", Path(filename).stem),
        ", ".join(authors),
        first_text(info, "lang"),
    )

    def blocks(node) -> list[str]:
        result = []
        for child in node:
            tag = local(child.tag)
            if tag in {"p", "subtitle", "v", "text-author", "date"}:
                # Inline references are excluded, but ordinary hyperlinks retain their text.
                clone = ET.fromstring(tostring(child))
                for parent in clone.iter():
                    for inline in list(parent):
                        if local(inline.tag) == "a" and inline.get("type") == "note":
                            index = list(parent).index(inline)
                            tail = inline.tail or ""
                            if index:
                                parent[index - 1].tail = (parent[index - 1].tail or "") + tail
                            else:
                                parent.text = (parent.text or "") + tail
                            parent.remove(inline)
                result.append(clean_text("".join(clone.itertext())))
            elif tag == "empty-line":
                result.append("")
            elif tag not in {"title", "section", "image"}:
                result.extend(blocks(child))
        return result

    def section(node, note: bool, fallback: str):
        title_node = next((c for c in node if local(c.tag) == "title"), None)
        title = clean_text(" ".join(title_node.itertext())) if title_node is not None else fallback
        # Flush runs around nested sections to preserve exact reading order.
        run = []
        if title_node is not None:
            run.append(title)

        def flush():
            if any(run):
                book.chapters.append(Chapter(title, "\n\n".join(run), note, not note))
                run.clear()

        for child in node:
            if local(child.tag) == "section":
                flush()
                section(child, note, f"Раздел {len(book.chapters) + 1}")
            elif local(child.tag) != "title":
                wrapper = Element("wrapper")
                wrapper.append(child)
                run.extend(blocks(wrapper))
        flush()

    for body in elements(root, "body"):
        note = body.get("name", "").lower() in {"notes", "comments", "footnotes"}
        section(body, note, "Сноски" if note else "Текст книги")
    coverpage = next(iter(elements(info, "coverpage")), None)
    if coverpage is not None:
        image = next(iter(elements(coverpage, "image")), None)
        if image is not None:
            href = next((v for k, v in image.attrib.items() if local(k) == "href"), "").lstrip("#")
            binary = next((e for e in elements(root, "binary") if e.get("id") == href), None)
            if binary is not None and binary.text:
                book.cover = image_bytes(base64.b64decode(binary.text))
    return book


def html_text(soup) -> str:
    for node in soup.find_all(["script", "style", "nav"]):
        node.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for node in soup.find_all(["p", "h1", "h2", "h3", "li", "blockquote", "div"]):
        node.insert_after("\n\n")
    return clean_text(soup.get_text(" "))


def import_epub(raw: bytes, filename: str) -> Book:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sum(i.file_size for i in archive.infolist()) > 150 * 1024 * 1024:
            raise ValueError("Слишком большой распакованный EPUB")

        def read(name: str) -> bytes:
            info = archive.getinfo(name)
            if info.file_size > 25 * 1024 * 1024:
                raise ValueError("Раздел EPUB слишком большой")
            return archive.read(name)

        container = ET.fromstring(read("META-INF/container.xml"))
        rootfiles = elements(container, "rootfile")
        if not rootfiles:
            raise ValueError("В EPUB отсутствует содержимое")
        opf = rootfiles[0].get("full-path", "")
        package = ET.fromstring(read(opf))
        base = posixpath.dirname(opf)
        metadata = next(iter(elements(package, "metadata")), package)
        book = Book(
            first_text(metadata, "title", Path(filename).stem),
            ", ".join("".join(e.itertext()) for e in elements(metadata, "creator")),
            first_text(metadata, "language"),
        )
        manifest = {e.get("id"): e for e in elements(package, "item")}

        def path(item):
            return posixpath.normpath(
                posixpath.join(base, unquote(item.get("href", "").split("#")[0]))
            )

        encrypted = set()
        if "META-INF/encryption.xml" in archive.namelist():
            encryption = ET.fromstring(read("META-INF/encryption.xml"))
            encrypted = {unquote(e.get("URI", "")) for e in elements(encryption, "CipherReference")}
        for ref in elements(package, "itemref"):
            item = manifest.get(ref.get("idref"))
            if item is None or "nav" in item.get("properties", "").split():
                continue
            location = path(item)
            if location in encrypted:
                raise ValueError("Зашифрованный текст EPUB не поддерживается")
            soup = BeautifulSoup(read(location), "html.parser")
            heading = soup.find(["h1", "h2", "h3"])
            title = (
                heading.get_text(" ", strip=True) if heading else f"Раздел {len(book.chapters) + 1}"
            )
            notes = []
            for node in list(
                soup.find_all(attrs={"epub:type": re.compile(r"\b(?:footnote|endnote)\b")})
            ):
                notes.append(node.get_text(" ", strip=True))
                node.decompose()
            for node in soup.find_all(attrs={"epub:type": re.compile(r"\bnoteref\b")}):
                node.decompose()
            text = html_text(soup.body or soup)
            if text:
                book.chapters.append(Chapter(title, text, included=ref.get("linear") != "no"))
            if notes:
                book.chapters.append(Chapter(f"Сноски: {title}", "\n\n".join(notes), True, False))
        cover_id = next(
            (m.get("content") for m in elements(metadata, "meta") if m.get("name") == "cover"), None
        )
        cover = next(
            (i for i in manifest.values() if "cover-image" in i.get("properties", "").split()),
            manifest.get(cover_id),
        )
        if cover is not None:
            book.cover = image_bytes(read(path(cover)))
        return book


def import_book(raw: bytes, filename: str, **pdf_options) -> Book:
    extension = Path(filename).suffix.lower()
    if extension == ".pdf":
        from .pdf_import import import_pdf

        def importer(data, name):
            return import_pdf(data, name, **pdf_options)
    else:
        importer = {".txt": import_txt, ".fb2": import_fb2, ".epub": import_epub}.get(extension)
    if not importer:
        raise ValueError("Поддерживаются PDF, EPUB, FB2 и TXT")
    try:
        book = importer(raw, filename)
    except (
        ValueError,
        ET.ParseError,
        DefusedXmlException,
        zipfile.BadZipFile,
        KeyError,
        OSError,
    ) as error:
        raise ValueError(f"Не удалось импортировать книгу: {error}") from error
    book.chapters = [c for c in book.chapters if c.text.strip()]
    if not book.chapters:
        raise ValueError("В книге не найден текст")
    if sum(len(c.text) for c in book.chapters) > MAX_TEXT:
        raise ValueError("Лимит книги — 5 миллионов символов")
    language = book.language.lower().split("-")[0].split("_")[0]
    book.language = (
        language
        if language in {"ru", "en"}
        else guess_language(" ".join(c.text[:500] for c in book.chapters))
    )
    return book
