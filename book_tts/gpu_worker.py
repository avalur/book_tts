"""Optional isolated inference server. Keep its ML dependencies out of the web environment."""

from __future__ import annotations

import hashlib
import hmac
import importlib.metadata
import io
import json
import os
import threading
from functools import cached_property
from pathlib import Path
from typing import Literal

import numpy as np
import soundfile as sf
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field


class SynthesisRequest(BaseModel):
    engine: Literal["qwen", "chatterbox"]
    text: str = Field(min_length=1, max_length=3000)
    language: Literal["ru", "en"]
    voice: str = Field(min_length=1, max_length=100)
    speed: float = Field(ge=0.7, le=1.4)
    fingerprint: str = Field(max_length=1000)


class NeuralBackend:
    def __init__(self, engine: str):
        self.engine = engine
        self.model = None
        self.clone_prompt = None
        self.lock = threading.Lock()
        self.device = os.getenv("BOOK_TTS_DEVICE", "cuda")
        self.reference = os.getenv("BOOK_TTS_REFERENCE_AUDIO", "")
        self.reference_text = os.getenv("BOOK_TTS_REFERENCE_TEXT", "")
        self.model_id = os.getenv("BOOK_TTS_MODEL_ID", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")

    def description(self):
        return self.descriptor

    @cached_property
    def descriptor(self):
        if self.engine == "qwen":
            from huggingface_hub import HfApi

            revision = (
                HfApi()
                .model_info(self.model_id, revision=os.getenv("BOOK_TTS_MODEL_REVISION", "main"))
                .sha
            )
            package = importlib.metadata.version("qwen-tts")
            voices = (
                ["reference"]
                if self.reference
                else [
                    "Ryan",
                    "Aiden",
                    "Vivian",
                    "Serena",
                    "Uncle_Fu",
                    "Dylan",
                    "Eric",
                    "Ono_Anna",
                    "Sohee",
                ]
            )
            if "Base" in self.model_id and not self.reference:
                raise ValueError(
                    "Для Qwen Base задайте BOOK_TTS_REFERENCE_AUDIO и BOOK_TTS_REFERENCE_TEXT"
                )
            if self.reference and "Base" not in self.model_id:
                raise ValueError(
                    "Образец голоса используется с Qwen Base. Измените BOOK_TTS_MODEL_ID"
                )
            identity = {
                "engine": "qwen",
                "model": self.model_id,
                "revision": revision,
                "package": package,
            }
            self.revision = revision
        else:
            if not self.reference:
                raise ValueError("Для Chatterbox задайте BOOK_TTS_REFERENCE_AUDIO")
            # The upstream loader manages multiple checkpoints. Require an operator version tag
            # and hash the local checkpoint bytes after loading before serving a catalog.
            self.load_chatterbox()
            from huggingface_hub import scan_cache_dir

            cache = scan_cache_dir()
            revisions = sorted(
                (r.repo_id, sorted(v.commit_hash for v in r.revisions))
                for r in cache.repos
                if "chatterbox" in r.repo_id.lower()
            )
            if not revisions:
                raise ValueError("Не удалось определить версии checkpoint Chatterbox")
            identity = {
                "engine": "chatterbox",
                "checkpoints": revisions,
                "package": importlib.metadata.version("chatterbox-tts"),
            }
            voices = ["reference"]
        if self.reference:
            reference = Path(self.reference)
            with reference.open("rb") as stream:
                identity["reference"] = hashlib.file_digest(stream, "sha256").hexdigest()
            identity["reference_text"] = self.reference_text
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return {
            "id": self.engine,
            "label": "Qwen3-TTS" if self.engine == "qwen" else "Chatterbox Multilingual",
            "languages": ["ru", "en"],
            "ready": True,
            "hint": "Синтез на отдельном worker",
            "fingerprint": fingerprint,
            "voices": [
                {
                    "id": v,
                    "label": "Образец рассказчика" if v == "reference" else v,
                    "language": lang,
                }
                for lang in ("ru", "en")
                for v in voices
            ],
        }

    def load_chatterbox(self):
        if self.model is None:
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS

            self.model = ChatterboxMultilingualTTS.from_pretrained(device=self.device)

    def synthesize(self, request: SynthesisRequest):
        descriptor = self.description()
        if request.fingerprint != descriptor["fingerprint"]:
            raise ValueError("Версия модели не совпадает с заданием")
        if request.voice not in {v["id"] for v in descriptor["voices"]}:
            raise ValueError("Неизвестный голос")
        with self.lock:
            if self.engine == "qwen":
                if self.model is None:
                    import torch
                    from huggingface_hub import snapshot_download
                    from qwen_tts import Qwen3TTSModel

                    snapshot = snapshot_download(self.model_id, revision=self.revision)
                    self.model = Qwen3TTSModel.from_pretrained(
                        snapshot,
                        device_map=self.device,
                        dtype=torch.bfloat16 if self.device.startswith("cuda") else torch.float32,
                        attn_implementation="sdpa",
                    )
                language = "Russian" if request.language == "ru" else "English"
                if self.reference:
                    if self.clone_prompt is None:
                        if not self.reference_text:
                            raise ValueError("Задайте текст образца в BOOK_TTS_REFERENCE_TEXT")
                        self.clone_prompt = self.model.create_voice_clone_prompt(
                            ref_audio=self.reference, ref_text=self.reference_text
                        )
                    wavs, rate = self.model.generate_voice_clone(
                        text=request.text, language=language, voice_clone_prompt=self.clone_prompt
                    )
                else:
                    # No emotion instruction: the 0.6B CustomVoice model does not support it.
                    wavs, rate = self.model.generate_custom_voice(
                        text=request.text, language=language, speaker=request.voice
                    )
                audio = np.asarray(wavs[0], dtype=np.float32)
            else:
                self.load_chatterbox()
                wav = self.model.generate(
                    request.text,
                    language_id=request.language,
                    audio_prompt_path=self.reference,
                    exaggeration=0.25,
                    cfg_weight=0.5,
                )
                audio = wav.squeeze().detach().cpu().numpy()
                rate = self.model.sr
            # Deterministic speed conversion is done by FFmpeg, not an unsupported model kwarg.
            buffer = io.BytesIO()
            sf.write(buffer, audio, rate, format="WAV", subtype="PCM_16")
            if request.speed != 1:
                import subprocess

                conversion = subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-i",
                        "pipe:0",
                        "-af",
                        f"atempo={request.speed}",
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        "-f",
                        "wav",
                        "pipe:1",
                    ],
                    input=buffer.getvalue(),
                    capture_output=True,
                    check=True,
                    timeout=180,
                )
                return conversion.stdout
            return buffer.getvalue()


def create_gpu_app(engine: str = "qwen", backend=None, token: str | None = None):
    token = token if token is not None else os.getenv("BOOK_TTS_GPU_TOKEN", "")
    if len(token) < 20:
        raise ValueError("Задайте BOOK_TTS_GPU_TOKEN длиной не менее 20 символов")
    backend = backend or NeuralBackend(engine)
    app = FastAPI(title="Book TTS inference worker")

    def authorized(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "Authentication required")

    @app.get("/health", dependencies=[Depends(authorized)])
    def health():
        return {"ok": True, "engine": engine}

    @app.get("/catalog", dependencies=[Depends(authorized)])
    def catalog():
        try:
            return [backend.description()]
        except Exception as error:
            import logging

            logging.exception("Cannot initialize model catalog")
            raise HTTPException(503, "Не удалось настроить модель. Проверьте лог worker") from error

    @app.post("/synthesize", dependencies=[Depends(authorized)])
    def synthesize(request: SynthesisRequest):
        if request.engine != engine:
            raise HTTPException(400, "Этот движок не запущен")
        try:
            audio = backend.synthesize(request)
            return Response(
                audio,
                media_type="audio/wav",
                headers={"X-Model-Fingerprint": backend.description()["fingerprint"]},
            )
        except Exception as error:
            import logging

            logging.exception("Synthesis failed")
            raise HTTPException(500, "Ошибка синтеза. Проверьте лог worker") from error

    return app
