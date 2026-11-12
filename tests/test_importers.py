import io
import zipfile

import pytest

from book_tts.importers import import_book


def test_txt_encoding_headings_and_language():
    raw = "Название\n\nГлава 1\nПервый абзац.\n\nГлава 2\nВторой абзац.".encode("cp1251")
    book = import_book(raw, "Книга.txt")
    assert book.title == "Книга"
    assert book.language == "ru"
    assert [c.title for c in book.chapters] == ["Вступление", "Глава 1", "Глава 2"]
    assert "Второй абзац." in book.chapters[-1].text


def test_fb2_nested_sections_notes_and_inline_tail():
    raw = """<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
    <description><title-info><book-title>Книга</book-title><lang>ru</lang>
    <author><first-name>Иван</first-name><last-name>Иванов</last-name></author></title-info></description>
    <body><section><title><p>Часть 1</p></title><p>Начало.</p>
    <section><title><p>Глава 1</p></title><p>Текст<a type="note" l:href="#n1">1</a> после сноски.</p></section>
    <p>Конец части.</p></section></body><body name="notes"><section id="n1"><p>Примечание.</p></section></body>
    </FictionBook>""".encode()
    book = import_book(raw, "book.fb2")
    assert book.author == "Иван Иванов"
    assert (
        "\n\n".join(c.text for c in book.chapters)
        == "Часть 1\n\nНачало.\n\nГлава 1\n\nТекст после сноски.\n\nКонец части.\n\nПримечание."
    )
    assert book.chapters[-1].is_note and not book.chapters[-1].included


def epub_bytes(encrypted=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/book.opf",
            """<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>Example</dc:title><dc:language>en</dc:language></metadata><manifest>
        <item id="b" href="b.xhtml"/><item id="a" href="a.xhtml"/><item id="nav" href="nav.xhtml" properties="nav"/>
        </manifest><spine><itemref idref="nav"/><itemref idref="a"/><itemref idref="b"/></spine></package>""",
        )
        archive.writestr(
            "OEBPS/a.xhtml",
            '<html><head><title>Do not read</title><style>Bad</style></head><body><h1>First</h1><p>Hello<a epub:type="noteref">1</a> world.</p><aside epub:type="footnote">A note.</aside></body></html>',
        )
        archive.writestr(
            "OEBPS/b.xhtml", "<html><body><h1>Second</h1><p>The end.</p></body></html>"
        )
        if encrypted:
            archive.writestr(
                "META-INF/encryption.xml",
                '<encryption><CipherReference URI="OEBPS/a.xhtml"/></encryption>',
            )
    return buffer.getvalue()


def test_epub_spine_order_notes_and_no_html_head():
    book = import_book(epub_bytes(), "book.epub")
    assert [c.title for c in book.chapters] == ["First", "Сноски: First", "Second"]
    assert "world." in book.chapters[0].text
    assert "Do not read" not in book.chapters[0].text
    assert not book.chapters[1].included
    assert book.language == "en"


def test_epub_rejects_encrypted_text():
    with pytest.raises(ValueError, match="Зашифрованный"):
        import_book(epub_bytes(True), "book.epub")


@pytest.mark.parametrize(
    "filename,raw",
    [("book.pdf", b"x"), ("book.txt", b""), ("book.fb2", b"<broken>"), ("book.epub", b"invalid")],
)
def test_invalid_imports(filename, raw):
    with pytest.raises(ValueError):
        import_book(raw, filename)


def test_xml_entities_are_rejected():
    with pytest.raises(ValueError):
        import_book(
            b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><FictionBook>&x;</FictionBook>',
            "book.fb2",
        )
