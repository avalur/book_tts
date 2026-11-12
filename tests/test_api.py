import io

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from book_tts.app import create_app
from book_tts.gpu_worker import create_gpu_app


def test_library_upload_edit_and_cross_origin_protection(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        result = client.post(
            "/api/books", files={"file": ("example.txt", "Chapter 1\nHello world.", "text/plain")}
        )
        assert result.status_code == 201
        book_id = result.json()["id"]
        page = client.get(f"/books/{book_id}")
        assert page.status_code == 200 and "Chapter 1" in page.text
        book = client.get(f"/api/books/{book_id}").json()
        book["title"] = "Changed"
        assert client.put(f"/api/books/{book_id}", json=book).status_code == 200
        assert client.put(f"/api/books/{book_id}", json=book).status_code == 409
        assert (
            client.post(
                "/api/books",
                files={"file": ("x.txt", b"text")},
                headers={"Origin": "https://unrelated.example"},
            ).status_code
            == 403
        )
        assert client.get("/api/health", headers={"Host": "unrelated.example"}).status_code == 400


def test_html_escapes_book_content(settings):
    with TestClient(create_app(settings)) as client:
        book_id = client.post(
            "/api/books", files={"file": ("x.txt", b"</textarea><script>alert(1)</script>")}
        ).json()["id"]
        page = client.get(f"/books/{book_id}").text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
        assert "<script>alert(1)</script>" not in page


def test_missing_resources_are_clean_errors(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/books/missing").status_code == 404
        assert client.get("/download/missing/manifest.json").status_code == 404
        assert client.post("/api/books", files={"file": ("book.pdf", b"bad")}).status_code == 400


def test_synthesis_worker_auth_and_contract():
    class Backend:
        def description(self):
            return {"id": "qwen", "fingerprint": "model-v1"}

        def synthesize(self, request):
            buffer = io.BytesIO()
            sf.write(buffer, np.zeros(2400), 24000, format="WAV")
            return buffer.getvalue()

    token = "test-token-with-at-least-20-characters"
    with TestClient(create_gpu_app("qwen", Backend(), token)) as client:
        assert client.get("/catalog").status_code == 401
        headers = {"Authorization": "Bearer " + token}
        assert client.get("/catalog", headers=headers).json()[0]["id"] == "qwen"
        response = client.post(
            "/synthesize",
            headers=headers,
            json={
                "engine": "qwen",
                "text": "Привет",
                "language": "ru",
                "voice": "Ryan",
                "speed": 1,
                "fingerprint": "model-v1",
            },
        )
        assert response.status_code == 200
        assert response.headers["X-Model-Fingerprint"] == "model-v1"
        assert response.content.startswith(b"RIFF")
    with pytest.raises(ValueError, match="20"):
        create_gpu_app(token="short")
