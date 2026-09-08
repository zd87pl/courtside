"""S3-compatible object storage (Tigris on Fly, MinIO locally, plain S3).

The phone never streams a 1.5 GB match through the API: it asks for a presigned
URL and PUTs straight to the bucket. Multipart exists because a cellular upload
of a match file will be interrupted, and a resumable part is the difference
between a retry and a re-upload.
"""

from __future__ import annotations

import mimetypes
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .config import settings

# S3 requires every part except the last to be >= 5 MiB.
MIN_PART_BYTES = 5 * 1024 * 1024
DEFAULT_PART_BYTES = 32 * 1024 * 1024
MAX_PARTS = 10_000

_client = None
_public_client = None
_client_lock = threading.Lock()


class StorageError(RuntimeError):
    pass


def client(*, public: bool = False):
    global _client, _public_client
    cached = _public_client if public else _client
    if cached is None:
        with _client_lock:
            cached = _public_client if public else _client
            if cached is None:
                s = settings()
                cached = boto3.client(
                    "s3",
                    endpoint_url=(s.s3_public_endpoint if public else "") or s.s3_endpoint or None,
                    region_name=s.s3_region or None,
                    aws_access_key_id=s.s3_access_key or None,
                    aws_secret_access_key=s.s3_secret_key or None,
                    config=Config(
                        signature_version="s3v4",
                        connect_timeout=10, read_timeout=60,
                        s3={"addressing_style": "path" if s.s3_force_path_style else "auto"},
                        retries={"max_attempts": 5, "mode": "standard"},
                    ),
                )
                if public:
                    _public_client = cached
                else:
                    _client = cached
    return cached


def bucket() -> str:
    return settings().s3_bucket


# ---------------------------------------------------------------- keys

def video_key(account_id, job_id, filename: str) -> str:
    ext = Path(filename or "").suffix.lower() or ".mp4"
    if len(ext) > 8 or not ext[1:].isalnum():   # never trust a client filename
        ext = ".mp4"
    return f"uploads/{account_id}/{job_id}/source{ext}"


def report_prefix(account_id, job_id) -> str:
    return f"reports/{account_id}/{job_id}"


# ---------------------------------------------------------------- presigning

def presign_put(key: str, content_type: str | None = None, expires: int | None = None) -> str:
    params = {"Bucket": bucket(), "Key": key}
    if content_type:
        params["ContentType"] = content_type
    return client(public=True).generate_presigned_url(
        "put_object", Params=params,
        ExpiresIn=expires or settings().upload_url_ttl_s,
    )


def presign_get(key: str, expires: int | None = None, filename: str | None = None) -> str:
    params = {"Bucket": bucket(), "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return client(public=True).generate_presigned_url(
        "get_object", Params=params,
        ExpiresIn=expires or settings().download_url_ttl_s,
    )


@dataclass(frozen=True)
class MultipartPlan:
    upload_id: str
    part_size: int
    urls: list[dict]


def part_plan(total_bytes: int) -> tuple[int, int]:
    """(part_size, n_parts) for a multipart upload.

    S3 caps a multipart upload at 10,000 parts, so the part size grows for very
    large sources rather than the upload failing on part 10,001.
    """
    part_size = max(DEFAULT_PART_BYTES, MIN_PART_BYTES)
    while -(-total_bytes // part_size) > MAX_PARTS:
        part_size *= 2
    return part_size, max(1, -(-total_bytes // part_size))


def plan_multipart(key: str, total_bytes: int, content_type: str | None = None,
                   expires: int | None = None) -> MultipartPlan:
    """Open a multipart upload and presign every part URL up front."""
    part_size, n_parts = part_plan(total_bytes)
    deadline = time.time() + (expires or settings().upload_url_ttl_s)

    params = {"Bucket": bucket(), "Key": key}
    if content_type:
        params["ContentType"] = content_type
    upload_id = client().create_multipart_upload(**params)["UploadId"]

    ttl = int(deadline - time.time())
    if ttl <= 0:
        abort_multipart(key, upload_id)
        raise StorageError("Reservation expired while initializing multipart upload")
    urls = [
        {
            "part_number": n,
            "url": client(public=True).generate_presigned_url(
                "upload_part",
                Params={"Bucket": bucket(), "Key": key, "UploadId": upload_id, "PartNumber": n},
                ExpiresIn=ttl,
            ),
        }
        for n in range(1, n_parts + 1)
    ]
    return MultipartPlan(upload_id=upload_id, part_size=part_size, urls=urls)


def complete_multipart(key: str, upload_id: str, parts: list[dict]) -> None:
    ordered = sorted(parts, key=lambda p: p["part_number"])
    try:
        client().complete_multipart_upload(
            Bucket=bucket(), Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": [
                {"PartNumber": p["part_number"], "ETag": p["etag"]} for p in ordered
            ]},
        )
    except ClientError as e:
        # Completing again after a lost HTTP response is safe. S3 removes the
        # multipart ID on success; the completed object remains at this job's key.
        if e.response.get("Error", {}).get("Code") == "NoSuchUpload" and head(key):
            return
        raise StorageError(f"could not complete multipart upload: {e}") from e


def abort_multipart(key: str, upload_id: str) -> None:
    try:
        client().abort_multipart_upload(Bucket=bucket(), Key=key, UploadId=upload_id)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "NoSuchUpload":
            raise StorageError("Could not abort multipart upload") from e


# ---------------------------------------------------------------- transfer

def head(key: str) -> dict | None:
    try:
        r = client().head_object(Bucket=bucket(), Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise StorageError("Could not inspect uploaded object.") from e
    return {"size": r["ContentLength"], "content_type": r.get("ContentType"),
            "etag": r.get("ETag", "").strip('"')}


def download(key: str, dest: Path, callback=None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        client().download_file(bucket(), key, str(dest), Callback=callback)
    except ClientError as e:
        raise StorageError(f"could not download {key}: {e}") from e
    return dest


def upload(path: Path, key: str, content_type: str | None = None, callback=None) -> str:
    ctype = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        client().upload_file(str(path), bucket(), key, ExtraArgs={"ContentType": ctype}, Callback=callback)
    except ClientError as e:
        raise StorageError(f"could not upload {key}: {e}") from e
    return key


def upload_tree(root: Path, prefix: str, skip: set[str] | None = None) -> list[str]:
    """Upload a finished output directory, returning the keys written."""
    skip = skip or set()
    keys: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name in skip:
            continue
        rel = p.relative_to(root).as_posix()
        keys.append(upload(p, f"{prefix}/{rel}"))
    return keys


def delete_prefix(prefix: str) -> None:
    paginator = client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            result = client().delete_objects(Bucket=bucket(), Delete={"Objects": objs})
            if result.get("Errors"):
                raise StorageError("Object deletion incomplete")


def ping() -> bool:
    try:
        client().head_bucket(Bucket=bucket())
        return True
    except Exception:
        return False


def resume_multipart(key: str, upload_id: str, total_bytes: int, expires: int) -> MultipartPlan:
    size, n_parts = part_plan(total_bytes)
    urls = [{"part_number": n, "url": client(public=True).generate_presigned_url(
        "upload_part", Params={"Bucket": bucket(), "Key": key, "UploadId": upload_id, "PartNumber": n},
        ExpiresIn=expires)} for n in range(1, n_parts + 1)]
    return MultipartPlan(upload_id, size, urls)


def list_parts(key: str, upload_id: str) -> list[dict]:
    try:
        return [{"part_number": p["PartNumber"], "etag": p["ETag"], "size_bytes": p["Size"]}
                for page in client().get_paginator("list_parts").paginate(
                    Bucket=bucket(), Key=key, UploadId=upload_id)
                for p in page.get("Parts", [])]
    except ClientError as e:
        raise StorageError("Multipart upload is no longer available; check completion status.") from e


def purge_prefix(prefix: str) -> None:
    """Delete all versions, delete markers, and incomplete uploads under a job prefix.

    Missing permissions and per-object errors fail the sweep for a durable retry.
    """
    c = client()
    # Some S3-compatible services return no multipart entries with Prefix.
    # List bucket uploads and filter locally; never abort another job's upload.
    for page in c.get_paginator("list_multipart_uploads").paginate(Bucket=bucket()):
        for upload in page.get("Uploads", []):
            if upload["Key"].startswith(prefix):
                abort_multipart(upload["Key"], upload["UploadId"])
    versioned = c.get_bucket_versioning(Bucket=bucket()).get("Status") in ("Enabled", "Suspended")
    paginator = c.get_paginator("list_object_versions" if versioned else "list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix):
        if versioned:
            objects = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                       for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        else:
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        for start in range(0, len(objects), 1000):
            result = c.delete_objects(Bucket=bucket(), Delete={"Objects": objects[start:start+1000]})
            if result.get("Errors"):
                raise StorageError("Object deletion incomplete; check bucket permissions/retention locks.")
