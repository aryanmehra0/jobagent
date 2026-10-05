"""Artifact storage backends for generated documents.

The database stores metadata and integrity hashes. The bytes live behind this
small abstraction so local development can keep files on disk while production
can use S3-compatible object storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from job_agent.config.settings import settings


@dataclass(frozen=True)
class StoredArtifact:
    backend: str
    object_key: str
    path: Optional[str]
    size_bytes: int
    mime_type: str


class ArtifactStore:
    backend = "local"

    def put_bytes(self, key: str, data: bytes, *, mime_type: str) -> StoredArtifact:
        raise NotImplementedError

    def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        try:
            self.get_bytes(key)
        except FileNotFoundError:
            return False
        return True


class LocalArtifactStore(ArtifactStore):
    backend = "local"

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or settings.artifact_storage_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        safe = Path(*[part for part in Path(key).parts if part not in ("", ".", "..")])
        target = (self.root / safe).resolve()
        root = self.root.resolve()
        if root not in target.parents and target != root:
            raise ValueError("Artifact key escapes the configured storage root.")
        return target

    def put_bytes(self, key: str, data: bytes, *, mime_type: str) -> StoredArtifact:
        target = self._path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return StoredArtifact(
            backend=self.backend,
            object_key=key,
            path=str(target),
            size_bytes=len(data),
            mime_type=mime_type,
        )

    def get_bytes(self, key: str) -> bytes:
        target = self._path_for(key)
        try:
            return target.read_bytes()
        except FileNotFoundError:
            raise FileNotFoundError(f"Artifact not found: {key}") from None


class S3ArtifactStore(ArtifactStore):
    backend = "s3"

    def __init__(self, *, bucket: Optional[str] = None, prefix: Optional[str] = None,
                 endpoint_url: Optional[str] = None):
        self.bucket = bucket or settings.artifact_s3_bucket
        if not self.bucket:
            raise RuntimeError("ARTIFACT_S3_BUCKET is required when ARTIFACT_STORAGE_BACKEND=s3.")
        self.prefix = (prefix if prefix is not None else settings.artifact_s3_prefix).strip("/")
        try:
            import boto3  # type: ignore
        except ImportError as exc:
            raise RuntimeError("S3 artifact storage requires boto3 to be installed.") from exc
        self.client = boto3.client("s3", endpoint_url=endpoint_url or settings.artifact_s3_endpoint_url)

    def put_bytes(self, key: str, data: bytes, *, mime_type: str) -> StoredArtifact:
        object_key = "/".join(part for part in (self.prefix, key.strip("/")) if part)
        self.client.put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=data,
            ContentType=mime_type,
        )
        return StoredArtifact(
            backend=self.backend,
            object_key=object_key,
            path=None,
            size_bytes=len(data),
            mime_type=mime_type,
        )

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                return body.read()
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "get", lambda *_: {})("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError(f"Artifact not found: {key}") from None
            raise


def artifact_store(*, local_root: Optional[Path] = None) -> ArtifactStore:
    if settings.artifact_storage_backend == "s3":
        return S3ArtifactStore()
    return LocalArtifactStore(local_root)
