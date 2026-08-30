from pathlib import Path

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
