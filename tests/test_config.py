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


def test_five_table_defaults_and_index_settings():
    settings = Settings()
    assert settings.admin_db_config == Path("config/database.yaml")
    assert settings.admin_db_path is None
    assert settings.paimon_table == "multimodal_asset_descriptor"
    assert settings.paimon_text_table == "multimodal_text_segment"
    assert settings.paimon_image_table == "multimodal_image_feature"
    assert settings.paimon_audio_table == "multimodal_audio_feature"
    assert settings.paimon_audit_table == "multimodal_transfer_audit"
    assert settings.paimon_domain_shards == 32
    assert settings.paimon_vector_index_type == "ivf-sq"


def test_legacy_sqlite_retention_environment_name_is_supported(monkeypatch):
    monkeypatch.setenv("MORPHLAKE_TRANSFER_SQLITE_RETENTION_DAYS", "45")
    assert Settings().transfer_detail_retention_days == 45
