"""Durable, catalog-only recovery snapshots for the assisted pilot.

These helpers intentionally never copy the SQLite database.  Replacing a
catalog must be recoverable without rolling back orders, approvals, audit data,
or customer records written after that catalog upload.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .money import legacy_to_storage

SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_RETENTION = 10
_SAFE_FILENAME = re.compile(r"^catalog-[0-9T-]{17,32}-[0-9a-f]{32}\.json$")


class CatalogBackupError(RuntimeError):
    """A snapshot cannot be written, validated, or safely recovered."""


class CatalogBackupMissingError(CatalogBackupError):
    """Durable metadata exists but its immutable snapshot file is gone."""


class CatalogBackupIntegrityError(CatalogBackupError):
    """A snapshot exists but cannot prove it matches its recorded contents."""


class CatalogBackupUnavailableError(CatalogBackupError):
    """A snapshot cannot currently be inspected for a non-integrity reason."""


def backup_directory(db_path: os.PathLike[str] | str) -> Path:
    """Return the configured persistent catalog-backup directory.

    In production the runtime validator requires either configured directory or
    database parent to live inside the mounted persistent volume.  Development
    defaults to a sibling of the explicitly selected SQLite file, which keeps
    a test database and its recovery data together.
    """
    configured = (os.getenv("CATALOG_BACKUP_DIR") or "").strip()
    if configured:
        directory = Path(configured)
        if not directory.is_absolute():
            raise CatalogBackupError("CATALOG_BACKUP_DIR must be an absolute path")
        return directory.resolve(strict=False)
    volume = (os.getenv("PERSISTENT_VOLUME_PATH") or "").strip()
    if volume:
        return (Path(volume) / "catalog-backups").resolve(strict=False)
    return (Path(db_path).resolve(strict=False).parent / "catalog-backups").resolve(strict=False)


def _safe_child(directory: Path, name: str, *, temporary: bool = False) -> Path:
    if not _SAFE_FILENAME.fullmatch(name) and not (
        temporary and name.endswith(".tmp") and _SAFE_FILENAME.fullmatch(name[:-4] + ".json")
    ):
        raise CatalogBackupError("Catalog backup filename is invalid")
    candidate = (directory / name).resolve(strict=False)
    try:
        candidate.relative_to(directory)
    except ValueError as exc:
        raise CatalogBackupError("Catalog backup path escapes the backup directory") from exc
    return candidate


def snapshot_path(*, db_path: os.PathLike[str] | str, filename: str) -> Path:
    """Resolve one metadata-selected snapshot without permitting path escape."""
    directory = backup_directory(db_path)
    try:
        return _safe_child(directory, filename)
    except CatalogBackupError as exc:
        # A filename that cannot pass the immutable-snapshot safety checks is
        # metadata/file integrity evidence, not a caller-supplied path error.
        raise CatalogBackupIntegrityError("Catalog backup filename is invalid") from exc


def _fsync_directory(directory: Path) -> None:
    """Persist a rename/link entry on POSIX filesystems before reporting success."""
    if os.name == "nt":  # Windows does not support opening directories this way.
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise CatalogBackupError("Could not durably sync the catalog backup directory") from exc


def canonical_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Create the deterministic catalog content used in manifests and hashes."""
    normalised: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, source in enumerate(rows):
        product_id = str(source.get("product_id") or "").strip()
        product_name = str(source.get("product_name") or "").strip()
        unit = str(source.get("unit") or "").strip()
        if not product_id or not product_name or not unit:
            raise CatalogBackupError("Catalog snapshot contains an incomplete product row")
        if product_id in seen:
            raise CatalogBackupError(f"Catalog snapshot contains duplicate SKU {product_id!r}")
        seen.add(product_id)
        aliases_value = source.get("aliases") or []
        aliases = (
            [part for part in str(aliases_value).split("|") if part]
            if isinstance(aliases_value, str)
            else [str(part) for part in aliases_value if str(part)]
        )
        try:
            price = legacy_to_storage(source.get("price"), "Catalog price")
        except ValueError as exc:
            raise CatalogBackupError("Catalog snapshot contains an invalid price") from exc
        normalised.append(
            {
                "product_id": product_id,
                "product_name": product_name,
                "aliases": aliases,
                "unit": unit,
                "price": price,
                "sort_order": int(source.get("sort_order", index) or 0),
            }
        )
    return sorted(normalised, key=lambda item: (item["sort_order"], item["product_id"]))


def content_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        canonical_rows(rows), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_snapshot(
    rows: Iterable[Mapping[str, Any]],
    *,
    db_path: os.PathLike[str] | str,
    source_catalog_version: int | None,
    actor: str | None,
    created_at: str,
) -> dict[str, Any]:
    """Atomically create one immutable catalog recovery file.

    The generated UUID and exclusively-created temporary file prevent overwrite;
    ``fsync`` plus ``os.replace`` means an interrupted write never leaves a
    partial file under its final recoverable name.
    """
    directory = backup_directory(db_path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CatalogBackupError("Could not create the persistent catalog backup directory") from exc
    if not directory.is_dir():
        raise CatalogBackupError("Catalog backup directory is not a directory")

    catalog_rows = canonical_rows(rows)
    backup_id = str(uuid.uuid4())
    # ISO punctuation is replaced only in the filename. The original UTC ISO
    # timestamp remains in the manifest/audit record.
    stamp = re.sub(r"[^0-9T-]", "", created_at.replace("+00:00", "Z"))[:24]
    filename = f"catalog-{stamp}-{backup_id.replace('-', '')}.json"
    target = _safe_child(directory, filename)
    manifest = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "backup_id": backup_id,
        "created_at": created_at,
        "source_catalog_version": source_catalog_version,
        "actor": actor,
        "product_count": len(catalog_rows),
        "content_sha256": content_sha256(catalog_rows),
        "products": catalog_rows,
    }
    payload = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    temporary = _safe_child(directory, filename[:-5] + ".tmp", temporary=True)
    # Temporary names are not accepted by read/restore helpers. Use exclusive
    # creation even though UUID collision is extraordinarily unlikely.
    try:
        with open(temporary, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # ``link`` creates the final name only if it does not already exist;
        # unlike os.replace it cannot overwrite an older snapshot on the
        # vanishingly unlikely UUID collision. Both paths are on one volume.
        os.link(temporary, target)
        temporary.unlink()
        _fsync_directory(directory)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CatalogBackupError("Could not write the persistent catalog backup") from exc
    return {**manifest, "filename": filename, "path": str(target)}


def read_snapshot(
    *,
    db_path: os.PathLike[str] | str,
    filename: str,
    backup_id: str,
) -> dict[str, Any]:
    """Read and fully validate a snapshot selected by durable metadata."""
    try:
        uuid.UUID(str(backup_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CatalogBackupIntegrityError("Catalog backup ID is invalid") from exc
    path = snapshot_path(db_path=db_path, filename=filename)
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise CatalogBackupMissingError(
            "Catalog backup file cannot be read because it is missing"
        ) from exc
    except OSError as exc:
        raise CatalogBackupUnavailableError("Catalog backup file cannot be read") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogBackupIntegrityError("Catalog backup file is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise CatalogBackupIntegrityError("Catalog backup schema is unsupported")
    if manifest.get("backup_id") != backup_id:
        raise CatalogBackupIntegrityError("Catalog backup identity does not match its metadata")
    products = manifest.get("products")
    if not isinstance(products, list) or manifest.get("product_count") != len(products):
        raise CatalogBackupIntegrityError("Catalog backup product count is invalid")
    try:
        actual_content_sha256 = content_sha256(products)
    except (AttributeError, CatalogBackupError, OverflowError, TypeError, ValueError) as exc:
        raise CatalogBackupIntegrityError("Catalog backup products are invalid") from exc
    if manifest.get("content_sha256") != actual_content_sha256:
        raise CatalogBackupIntegrityError("Catalog backup content hash does not match")
    try:
        manifest["products"] = canonical_rows(products)
    except (AttributeError, CatalogBackupError, OverflowError, TypeError, ValueError) as exc:
        raise CatalogBackupIntegrityError("Catalog backup products are invalid") from exc
    return manifest


def prune_snapshot_files(
    records: Iterable[Mapping[str, Any]],
    *,
    db_path: os.PathLike[str] | str,
    retain: int = DEFAULT_RETENTION,
) -> list[str]:
    """Remove only validated old files; callers remove matching DB metadata."""
    if retain < 1:
        raise CatalogBackupError("Catalog backup retention must be at least one")
    ordered = sorted(records, key=lambda row: str(row.get("created_at") or ""), reverse=True)
    removed: list[str] = []
    for record in ordered[retain:]:
        filename = str(record.get("filename") or "")
        path = _safe_child(backup_directory(db_path), filename)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            # Missing durable recovery data is an operational failure, not a
            # reason to silently pretend retention succeeded.
            raise CatalogBackupError(
                "Catalog backup file is missing during retention cleanup"
            ) from exc
        except OSError as exc:
            raise CatalogBackupError("Could not prune an old catalog backup") from exc
        removed.append(str(record.get("backup_id") or ""))
    return removed
