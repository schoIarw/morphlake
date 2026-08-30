"""Application exceptions and error response helpers."""

from __future__ import annotations


class MorphLakeError(Exception):
    """Base application error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class NotFoundError(MorphLakeError):
    def __init__(self, message: str):
        super().__init__("not_found", message, 404)


class ConfigurationError(MorphLakeError):
    def __init__(self, message: str):
        super().__init__("configuration_error", message, 500)


class StorageError(MorphLakeError):
    def __init__(self, message: str):
        super().__init__("storage_error", message, 503)
