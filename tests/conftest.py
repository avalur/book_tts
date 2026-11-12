import numpy as np
import pytest
import soundfile as sf

from book_tts.config import Settings
from book_tts.importers import Book, Chapter
from book_tts.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "data", tmp_path / "models", spawn_worker=False)


@pytest.fixture
def store(settings):
    return Store(settings)


@pytest.fixture
def book_id(store):
    return store.add_book(
        Book(
            "Тестовая книга",
            "Автор",
            "ru",
            [
                Chapter("Глава 1", "Глава 1\n\nСначала мы проверим, что всё работает. " * 8),
                Chapter("Глава 2", "Глава 2\n\nЗатем спокойно прочитаем последнюю страницу."),
                Chapter("Примечание", "Это сноска", True, False),
            ],
        ),
        "test.fb2",
        b"original",
    )


class TestEngine:
    __test__ = False

    def __init__(self):
        self.calls = []
        self.after_synthesis = None

    def synthesize(self, profile, text, destination):
        self.calls.append(text)
        samples = np.sin(np.arange(12000) * 2 * np.pi * 220 / 24000) * 0.1
        sf.write(destination, samples, 24000, subtype="PCM_16")
        if self.after_synthesis:
            self.after_synthesis()


@pytest.fixture
def test_engine():
    return TestEngine()


@pytest.fixture
def profile():
    return {
        "engine": "test",
        "voice": "test",
        "speed": 1.0,
        "language": "ru",
        "fingerprint": "test-v1",
    }
