from pathlib import Path
from types import SimpleNamespace

from morphlake.services.embeddings import ModelGateway


def _config(path: Path):
    path.write_text(
        """models:
  text_embedding: {provider: hash, model: text, dimension: 4}
  image_embedding: {provider: hash, model: image, dimension: 5}
  audio_transcription: {provider: none, model: none}
  audio_embedding: {provider: hash, model: audio, dimension: 6}
chunking: {size: 100, overlap: 10}
""",
        encoding="utf-8",
    )


def test_hash_embeddings_are_deterministic_and_normalized(tmp_path: Path):
    path = tmp_path / "models.yaml"
    _config(path)
    gateway = ModelGateway(path)
    first = gateway.embed_text("hello")
    assert first == gateway.embed_text("hello")
    assert len(first) == 4
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6


def test_none_audio_transcription(tmp_path: Path):
    path = tmp_path / "models.yaml"
    _config(path)
    assert ModelGateway(path).transcribe_audio("a.wav", b"audio", "audio/wav") is None


def test_ollama_vision_caption_is_embedded_as_text(monkeypatch, tmp_path: Path):
    path = tmp_path / "models.yaml"
    path.write_text(
        """models:
  text_embedding:
    provider: openai_compatible
    model: nomic-embed-text
    base_url: http://ollama:11434/v1
    dimension: 4
  image_embedding:
    provider: ollama_vision_caption
    model: minicpm-v:8b
    base_url: http://ollama:11434
    dimension: 4
  audio_transcription: {provider: none, model: none}
  audio_embedding: {provider: hash, model: audio, dimension: 6}
chunking: {size: 100, overlap: 10}
""",
        encoding="utf-8",
    )
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/api/chat"):
            payload = {"message": {"content": "a red audit chart"}}
        else:
            payload = {"data": [{"embedding": [1.0, 0.0, 0.0, 0.0]}]}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    monkeypatch.setattr("morphlake.services.embeddings.httpx.post", post)
    vector = ModelGateway(path).embed_image(b"image", "image/png")

    assert vector == [1.0, 0.0, 0.0, 0.0]
    assert calls[0][0] == "http://ollama:11434/api/chat"
    assert calls[0][1]["json"]["messages"][0]["images"]
    assert calls[1][0] == "http://ollama:11434/v1/embeddings"
    assert calls[1][1]["json"]["input"] == "a red audit chart"
