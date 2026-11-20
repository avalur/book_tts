from dataclasses import dataclass, field

TEXT_VERSION = 2


@dataclass
class PdfLine:
    text: str
    x: float
    y: float
    right: float
    bottom: float
    confidence: float = 100.0
    paragraph: str = ""
    continuation: bool = False


@dataclass
class PdfPage:
    number: int
    lines: list[PdfLine] = field(default_factory=list)
    method: str = "text"
    confidence: float | None = None
    references: list[dict] = field(default_factory=list)
    adjustments: list[dict] = field(default_factory=list)
    text_version: int = TEXT_VERSION
