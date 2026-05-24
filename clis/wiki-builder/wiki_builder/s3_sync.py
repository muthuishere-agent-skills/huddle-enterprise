"""Parquet ↔ S3 sync. boto3-based; uses standard AWS env/profile auth."""
from __future__ import annotations
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import paths


def _parse_uri(uri: str) -> tuple[str, str]:
    m = re.match(r"^s3://([^/]+)/(.*)$", uri)
    if not m:
        raise ValueError(f"Not an s3:// uri: {uri}")
    bucket, key = m.group(1), m.group(2).rstrip("/")
    return bucket, key


def push(corpus: str, s3_uri: str, *, profile: str | None = None) -> list[str]:
    """Upload all parquet files in exports/ to s3_uri/{date}/."""
    import boto3
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")
    bucket, prefix = _parse_uri(s3_uri)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    uploaded: list[str] = []
    exports = paths.exports_dir(corpus)
    if not exports.exists():
        raise FileNotFoundError(f"No exports/ for corpus {corpus}. Run export first.")
    for f in sorted(exports.glob("*.parquet")):
        key = f"{prefix}/{date}/{f.name}".lstrip("/")
        s3.upload_file(str(f), bucket, key)
        uploaded.append(f"s3://{bucket}/{key}")
    return uploaded


def pull(corpus: str, s3_uri: str, *, profile: str | None = None) -> list[str]:
    """Download parquet files from s3_uri to local exports/."""
    import boto3
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")
    bucket, prefix = _parse_uri(s3_uri)
    out = paths.exports_dir(corpus)
    out.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if not name.endswith(".parquet"):
                continue
            target = out / name
            s3.download_file(bucket, obj["Key"], str(target))
            downloaded.append(str(target))
    return downloaded
