.PHONY: setup run models check

setup:
	uv sync --frozen --extra kokoro --extra dev

models:
	uv run --extra kokoro book-tts models download

run:
	uv run --extra kokoro book-tts serve

check:
	uv run --extra kokoro --extra dev ruff check book_tts tests
	uv run --extra kokoro --extra dev ruff format --check book_tts tests
	uv run --extra kokoro --extra dev pytest -q
