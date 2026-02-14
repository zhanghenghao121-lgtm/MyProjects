import logging
import os
from pathlib import Path
from typing import Optional

try:
    from qcloud_cos import CosConfig, CosS3Client
    from qcloud_cos.cos_exception import CosClientError, CosServiceError
except Exception:  # pragma: no cover - SDK may not be installed in some envs
    CosConfig = CosS3Client = None
    CosClientError = CosServiceError = Exception

logger = logging.getLogger(__name__)

_cos_client: Optional["CosS3Client"] = None


def _get_client() -> Optional["CosS3Client"]:
    """Lazy init COS client; return None if config incomplete."""
    global _cos_client
    if _cos_client:
        return _cos_client

    secret_id = os.getenv("COS_SECRET_ID")
    secret_key = os.getenv("COS_SECRET_KEY")
    region = os.getenv("COS_REGION")
    scheme = os.getenv("COS_SCHEME", "https")

    if not all([secret_id, secret_key, region]):
        logger.debug("COS config missing, skip client init")
        return None
    if CosConfig is None or CosS3Client is None:
        logger.warning("qcloud_cos SDK not installed; cannot upload to COS")
        return None

    config = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Token=None,
        Scheme=scheme,
    )
    _cos_client = CosS3Client(config)
    return _cos_client


def _build_key(key: str) -> str:
    prefix = os.getenv("COS_UPLOAD_PREFIX", "").strip().strip("/")
    cleaned_key = key.lstrip("/")
    if prefix:
        return f"{prefix}/{cleaned_key}"
    return cleaned_key


def build_public_url(key: str) -> Optional[str]:
    bucket = os.getenv("COS_BUCKET")
    region = os.getenv("COS_REGION")
    scheme = os.getenv("COS_SCHEME", "https")
    if not (bucket and region):
        return None
    return f"{scheme}://{bucket}.cos.{region}.myqcloud.com/{key.lstrip('/')}"


def upload_file_to_cos(local_path: str, key: Optional[str] = None) -> Optional[str]:
    """
    Upload local file to COS. Returns public URL if success, otherwise None.
    """
    client = _get_client()
    bucket = os.getenv("COS_BUCKET")
    if not client or not bucket:
        return None

    path = Path(local_path)
    if not path.exists() or not path.is_file():
        logger.warning("COS upload skipped, file missing: %s", local_path)
        return None

    cos_key = _build_key(key or path.name)

    try:
        with path.open("rb") as fp:
            client.put_object(Bucket=bucket, Body=fp, Key=cos_key)
        # Prefer signed URL if bucket is private; fall back to public URL.
        if os.getenv("COS_SIGNED_URL", "true").lower() == "true":
            try:
                expires = int(os.getenv("COS_SIGN_EXPIRES", "86400"))
            except ValueError:
                expires = 86400
            return client.get_object_url(Bucket=bucket, Key=cos_key, Expired=expires)
        return build_public_url(cos_key)
    except (CosClientError, CosServiceError, Exception) as exc:  # noqa: BLE001
        logger.warning("Upload to COS failed for %s: %s", cos_key, exc)
        return None
