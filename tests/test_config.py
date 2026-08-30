from pathlib import Path

from morphlake.config import Settings, load_model_config


def test_model_config_expands_environment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MODEL_NAME", "production-model")
    path = tmp_path / "models.yaml"
    path.write_text(
        "models:\n"
        "  text_embedding:\n"
        "    provider: hash\n"
        "    model: '${MODEL_NAME:-fallback}'\n"
        "    dimension: 4\n"
        "chunking:\n"
        "  size: 100\n"
        "  overlap: 10\n",
        encoding="utf-8",
    )
    assert load_model_config(path)["models"]["text_embedding"]["model"] == "production-model"


def test_catalog_options_use_path_style_minio():
    settings = Settings(
        MINIO_ENDPOINT="minio.internal:9000",
        MINIO_SECURE=True,
        MINIO_ACCESS_KEY="access",
        MINIO_SECRET_KEY="secret",
    )
    options = settings.paimon_catalog_options()
    assert options["s3.endpoint"] == "https://minio.internal:9000"
    assert options["s3.path-style-access"] == "true"
