"""Supporting documents (PDFs) in DigitalOcean Spaces — private, S3-compatible object storage.

Used by Brand Profitability settings: a contract or letter backing a rate change is stored
here and listed against the rule. Objects are private; the site streams them back to people
who may open the settings (never a public or pre-signed link).

Settings (env, gitignored .env / the droplet's deploy/.env), a bucket of its own with a key
scoped to that one bucket:
    SPACES_ENDPOINT    https://syd1.digitaloceanspaces.com
    SPACES_REGION      syd1
    SPACES_BUCKET      e.g. macgear-reporting-docs
    SPACES_ACCESS_KEY / SPACES_SECRET_KEY
Unset = uploads are refused with a message; nothing else breaks.
"""
import hashlib
import os
import re

MAX_BYTES = 10 * 1024 * 1024          # 10 MB (user, 2026-11-20)
_KEYS = ("SPACES_ENDPOINT", "SPACES_REGION", "SPACES_BUCKET", "SPACES_ACCESS_KEY", "SPACES_SECRET_KEY")


class NotConfigured(RuntimeError):
    pass


class Rejected(ValueError):
    pass


def configured() -> bool:
    return all(os.environ.get(k) for k in _KEYS)


def _client():
    if not configured():
        raise NotConfigured("Document storage isn't set up (SPACES_* settings).")
    import boto3
    return boto3.client("s3", endpoint_url=os.environ["SPACES_ENDPOINT"],
                        region_name=os.environ["SPACES_REGION"],
                        aws_access_key_id=os.environ["SPACES_ACCESS_KEY"],
                        aws_secret_access_key=os.environ["SPACES_SECRET_KEY"])


def check_pdf(filename: str, data: bytes) -> dict:
    """A PDF of at most MAX_BYTES, judged by its content (the %PDF- header), not only its name."""
    name = os.path.basename((filename or "").replace("\\", "/")).strip() or "document.pdf"
    if not data:
        raise Rejected("The file is empty.")
    if len(data) > MAX_BYTES:
        raise Rejected(f"The file is {len(data) / 1048576:.1f} MB; the limit is {MAX_BYTES // 1048576} MB.")
    if not name.lower().endswith(".pdf") or not data[:1024].lstrip().startswith(b"%PDF-"):
        raise Rejected("Only PDF files can be attached.")
    return {"filename": name[:200], "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def object_key(prefix: str, sha256: str, filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[:120]
    return f"{prefix.strip('/')}/{sha256[:16]}_{safe}"


def put(key: str, data: bytes) -> None:
    _client().put_object(Bucket=os.environ["SPACES_BUCKET"], Key=key, Body=data,
                         ContentType="application/pdf", ACL="private")


def get(key: str) -> bytes:
    obj = _client().get_object(Bucket=os.environ["SPACES_BUCKET"], Key=key)
    return obj["Body"].read()


def delete(key: str) -> None:
    """Only to clean up an upload whose database write then failed."""
    _client().delete_object(Bucket=os.environ["SPACES_BUCKET"], Key=key)
