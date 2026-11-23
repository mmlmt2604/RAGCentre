"""Resolve provider-managed local stores within the data directory."""

from pathlib import Path

from ragcentre.errors import ValidationFailed


def store_path(data_dir: Path, configured: str | None, default: str) -> Path:
    data = data_dir.resolve()
    root = (data / "stores").resolve()
    if not root.is_relative_to(data) or root == data:
        raise ValidationFailed("The stores directory must stay inside the data directory.")
    value = Path(configured or default)
    if value.is_absolute() or ".." in value.parts:
        raise ValidationFailed("Provider store paths must be relative to the stores directory.")
    target = (root / value).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValidationFailed("Provider store path must stay inside the stores directory.")
    if configured is None:
        legacy = data / default
        if legacy.exists() and legacy.resolve() == legacy:
            return legacy
    return target


def validate_store_path(data_dir: Path, type_: str, config: dict[str, object]) -> None:
    if type_ == "sqlite_vec" or (type_ == "chroma" and config.get("mode") == "local"):
        path = config.get("path")
        store_path(
            data_dir, path if isinstance(path, str) else None, "vectors.db" if type_ == "sqlite_vec" else "chroma"
        )
    elif type_ == "qdrant":
        path = config.get("path")
        if isinstance(path, str) and path != ":memory:":
            store_path(data_dir, path, "qdrant")
    elif type_ in {"lancedb", "milvus"}:
        uri = config.get("uri")
        if isinstance(uri, str) and "://" not in uri:
            store_path(data_dir, uri, "lancedb")
        if isinstance(uri, str) and uri.lower().startswith(("file:", "local:")):
            raise ValidationFailed("Local store URIs must use a relative path.")
