"""Configuration-driven model gateway."""

from __future__ import annotations

import base64
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from morphlake.config import load_model_config
from morphlake.errors import ConfigurationError, StorageError


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    dimension: int | None = None
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: float = 60
    max_chars: int = 500


class ModelGateway:
    """Calls configured model endpoints without introducing a model server component."""

    def __init__(self, config_path: Path):
        raw = load_model_config(config_path)
        self.specs = {name: ModelSpec(**value) for name, value in raw["models"].items()}
        self.chunk_size = int(raw["chunking"]["size"])
        self.chunk_overlap = int(raw["chunking"]["overlap"])

    def embed_text(self, text: str) -> list[float]:
        return self._embed("text_embedding", text)

    def embed_image(
        self, body: bytes, content_type: str, description: str | None = None
    ) -> list[float]:
        spec = self._spec("image_embedding")
        if spec.provider == "hash":
            return _hash_embedding(body, self._dimension(spec))
        if spec.provider == "ollama_vision_caption":
            return self._ollama_vision_embedding(spec, body, description)
        data_url = f"data:{content_type};base64,{base64.b64encode(body).decode('ascii')}"
        return self._openai_embedding(spec, data_url)

    def describe_image(self, body: bytes, content_type: str) -> str:
        spec = self._spec("image_embedding")
        if spec.provider == "ollama_vision_caption":
            return self._ollama_vision_caption(spec, body)
        return f"图片文件，格式 {content_type}，大小 {len(body)} 字节。"

    def summarize(self, text: str, fallback: str) -> str:
        normalized = " ".join(text.split())
        if not normalized:
            return fallback
        spec = self.specs.get(
            "text_summary",
            ModelSpec(provider="extractive", model="morphlake-extractive-summary-v1"),
        )
        if spec.provider == "extractive":
            return normalized[: spec.max_chars]
        if spec.provider != "ollama_chat":
            raise ConfigurationError(f"Unsupported text summary provider: {spec.provider}")
        try:
            response = httpx.post(
                f"{spec.base_url.rstrip('/')}/api/chat",
                headers=self._headers(spec),
                json={
                    "model": spec.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "请用中文客观概括以下内容，保留主题、关键实体和结论，"
                                f"不超过{spec.max_chars}字：\n\n{normalized[:12000]}"
                            ),
                        }
                    ],
                    "stream": False,
                    "options": {"temperature": 0},
                },
                timeout=spec.timeout_seconds,
            )
            response.raise_for_status()
            summary = response.json()["message"]["content"].strip()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise StorageError(f"Summary request failed for {spec.model}: {exc}") from exc
        return summary[: spec.max_chars] or fallback

    def embed_audio(self, body: bytes) -> list[float]:
        spec = self._spec("audio_embedding")
        if spec.provider == "hash":
            return _hash_embedding(body, self._dimension(spec))
        payload = base64.b64encode(body).decode("ascii")
        return self._openai_embedding(spec, payload)

    def transcribe_audio(self, filename: str, body: bytes, content_type: str) -> str | None:
        spec = self._spec("audio_transcription")
        if spec.provider == "none":
            return None
        if spec.provider != "openai_compatible":
            raise ConfigurationError(f"Unsupported audio transcription provider: {spec.provider}")
        url = f"{spec.base_url.rstrip('/')}/audio/transcriptions"
        try:
            response = httpx.post(
                url,
                headers={
                    key: value
                    for key, value in self._headers(spec).items()
                    if key.lower() != "content-type"
                },
                data={"model": spec.model},
                files={"file": (filename, body, content_type)},
                timeout=spec.timeout_seconds,
            )
            response.raise_for_status()
            return response.json().get("text")
        except (httpx.HTTPError, ValueError) as exc:
            raise StorageError(f"Audio transcription failed: {exc}") from exc

    def ping(self) -> None:
        """Check each distinct configured remote model endpoint."""
        endpoints: dict[str, ModelSpec] = {}
        for spec in self.specs.values():
            if spec.provider == "openai_compatible" and spec.base_url:
                endpoints[f"{spec.base_url.rstrip('/')}/models"] = spec
            elif spec.provider in {"ollama_vision_caption", "ollama_chat"} and spec.base_url:
                endpoints[f"{spec.base_url.rstrip('/')}/api/tags"] = spec
        for url, spec in endpoints.items():
            try:
                response = httpx.get(url, headers=self._headers(spec), timeout=5)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise StorageError(f"Model endpoint unavailable at {url}: {exc}") from exc

    def _embed(self, name: str, value: str) -> list[float]:
        spec = self._spec(name)
        if spec.provider == "hash":
            return _hash_embedding(value.encode("utf-8"), self._dimension(spec))
        return self._openai_embedding(spec, value)

    def _openai_embedding(self, spec: ModelSpec, value: Any) -> list[float]:
        if spec.provider != "openai_compatible":
            raise ConfigurationError(f"Unsupported embedding provider: {spec.provider}")
        if not spec.base_url:
            raise ConfigurationError(f"base_url is required for model {spec.model}")
        try:
            response = httpx.post(
                f"{spec.base_url.rstrip('/')}/embeddings",
                headers=self._headers(spec),
                json={"model": spec.model, "input": value},
                timeout=spec.timeout_seconds,
            )
            response.raise_for_status()
            vector = [float(item) for item in response.json()["data"][0]["embedding"]]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise StorageError(f"Embedding request failed for {spec.model}: {exc}") from exc
        if len(vector) != self._dimension(spec):
            raise StorageError(
                f"Model {spec.model} returned dimension {len(vector)}; expected {spec.dimension}"
            )
        return vector

    def _ollama_vision_embedding(
        self, spec: ModelSpec, body: bytes, description: str | None = None
    ) -> list[float]:
        """Describe an image with Ollama vision, then embed that description as text."""
        caption = description or self._ollama_vision_caption(spec, body)
        vector = self.embed_text(caption)
        if len(vector) != self._dimension(spec):
            raise StorageError(
                f"Image pipeline returned dimension {len(vector)}; expected {spec.dimension}"
            )
        return vector

    def _ollama_vision_caption(self, spec: ModelSpec, body: bytes) -> str:
        if not spec.base_url:
            raise ConfigurationError(f"base_url is required for model {spec.model}")
        try:
            response = httpx.post(
                f"{spec.base_url.rstrip('/')}/api/chat",
                headers=self._headers(spec),
                json={
                    "model": spec.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "请用中文描述这张图片，用于语义检索和摘要。包括可见文字（OCR）、"
                                "对象、场景、布局和关键属性；保持客观、简洁。"
                            ),
                            "images": [base64.b64encode(body).decode("ascii")],
                        }
                    ],
                    "stream": False,
                    "options": {"temperature": 0},
                },
                timeout=spec.timeout_seconds,
            )
            response.raise_for_status()
            caption = response.json()["message"]["content"].strip()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise StorageError(f"Ollama vision request failed for {spec.model}: {exc}") from exc
        if not caption:
            raise StorageError(f"Ollama vision model {spec.model} returned an empty description")
        return caption

    def _spec(self, name: str) -> ModelSpec:
        try:
            return self.specs[name]
        except KeyError as exc:
            raise ConfigurationError(f"Missing model configuration: {name}") from exc

    @staticmethod
    def _dimension(spec: ModelSpec) -> int:
        if not spec.dimension or spec.dimension <= 0:
            raise ConfigurationError(f"A positive dimension is required for model {spec.model}")
        return spec.dimension

    @staticmethod
    def _headers(spec: ModelSpec) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if spec.api_key:
            headers["Authorization"] = f"Bearer {spec.api_key}"
        return headers


def _hash_embedding(value: bytes, dimension: int) -> list[float]:
    """Deterministic development embedding; not intended for semantic production search."""
    output: list[float] = []
    counter = 0
    while len(output) < dimension:
        digest = hashlib.sha256(value + counter.to_bytes(4, "big")).digest()
        output.extend((byte - 127.5) / 127.5 for byte in digest)
        counter += 1
    output = output[:dimension]
    norm = math.sqrt(sum(item * item for item in output)) or 1.0
    return [item / norm for item in output]
