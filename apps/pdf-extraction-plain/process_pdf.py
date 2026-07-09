"""
Phase 1 — Plain Python PDF-to-Markdown pipeline.

Usage:
    $ python process_pdf.py s3://temporal-dev/files/cisco-88xx-user-guide.pdf
"""

import os
import sys
import logging
from pathlib import Path, PurePosixPath
from functools import lru_cache

import boto3
from botocore.exceptions import BotoCoreError, ClientError
import pymupdf4llm
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    """Read configuration from the environment. Fails clearly if anything required is missing."""
    load_dotenv()

    required = [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "AWS_S3_ENDPOINT_URL",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")

    import tempfile

    return {
        "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
        "aws_region": os.environ["AWS_REGION"],
        "aws_s3_endpoint_url": os.environ["AWS_S3_ENDPOINT_URL"],
        "temp_dir": os.environ.get("TEMP_DIR", tempfile.gettempdir()),
    }


# ── S3 helpers ────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def get_s3_client():
    """Return a cached S3 client so it is created only once per process."""
    cfg = load_config()
    return boto3.client(
        "s3",
        region_name=cfg["aws_region"],
        aws_access_key_id=cfg["aws_access_key_id"],
        aws_secret_access_key=cfg["aws_secret_access_key"],
        endpoint_url=cfg["aws_s3_endpoint_url"],
    )


def parse_s3_path(s3_path: str) -> tuple[str, str]:
    """Split an s3://bucket/key path into (bucket, key), validating the input."""
    if not s3_path.startswith("s3://"):
        raise ValueError(f"Not an S3 path (expected 's3://...'): {s3_path!r}")

    bucket, _, key = s3_path[len("s3://"):].partition("/")
    if not bucket or not key:
        raise ValueError(f"S3 path must include both a bucket and a key: {s3_path!r}")

    return bucket, key


def swap_extension(key: str, new_suffix: str) -> str:
    """Replace only the final extension of an S3 key (case-insensitive, path-safe)."""
    return str(PurePosixPath(key).with_suffix(new_suffix))


# ── Step 1: Download ──────────────────────────────────────────────────────────
def download_pdf(s3_path: str, temp_dir: str) -> str:
    """Download a PDF from S3. Returns the local file path."""
    bucket, key = parse_s3_path(s3_path)
    local_path = str(Path(temp_dir) / Path(key).name)

    log.info(f"Downloading: s3://{bucket}/{key} => {local_path}")
    try:
        get_s3_client().download_file(bucket, key, local_path)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to download s3://{bucket}/{key}: {exc}") from exc

    log.info(f"Download complete: {local_path}")
    return local_path


# ── Step 2: Extract to Markdown ───────────────────────────────────────────────
def extract_to_markdown(local_pdf_path: str) -> str:
    """Extract text from a PDF and convert it to Markdown. Returns the markdown string."""
    log.info(f"Extracting text from {local_pdf_path}")
    try:
        markdown_text = pymupdf4llm.to_markdown(local_pdf_path)
    except Exception as exc:
        raise RuntimeError(f"Failed to extract markdown from {local_pdf_path}: {exc}") from exc

    log.info(f"Extraction complete — {len(markdown_text)} characters")
    return markdown_text


# ── Step 3: Upload Markdown ───────────────────────────────────────────────────
def upload_markdown(markdown_text: str, original_s3_path: str) -> str:
    """Upload markdown content to S3 alongside the source. Returns the output S3 path."""
    bucket, key = parse_s3_path(original_s3_path)
    md_key = swap_extension(key, ".md")

    if md_key == key:
        raise ValueError(f"Refusing to overwrite source object; key has no distinct .md target: {key!r}")

    log.info(f"Uploading markdown → s3://{bucket}/{md_key}")
    try:
        get_s3_client().put_object(
            Bucket=bucket,
            Key=md_key,
            Body=markdown_text.encode("utf-8"),
            ContentType="text/markdown",
        )
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to upload s3://{bucket}/{md_key}: {exc}") from exc

    output_path = f"s3://{bucket}/{md_key}"
    log.info(f"Upload complete: {output_path}")
    return output_path


# ── Main pipeline ─────────────────────────────────────────────────────────────
def process_pdf(s3_input_path: str) -> str:
    """Run the full pipeline. Returns the output S3 path."""
    cfg = load_config()
    os.makedirs(cfg["temp_dir"], exist_ok=True)

    log.info(f"Starting pipeline for: {s3_input_path}")

    local_pdf = download_pdf(s3_input_path, cfg["temp_dir"])
    try:
        markdown = extract_to_markdown(local_pdf)
        output_s3 = upload_markdown(markdown, s3_input_path)
    finally:
        # Always clean up the temp file, even if extraction or upload fails.
        if os.path.exists(local_pdf):
            os.remove(local_pdf)
            log.info(f"Removed temp file: {local_pdf}")

    log.info(f"Pipeline complete. Output: {output_s3}")
    return output_s3


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: python {Path(sys.argv[0]).name} s3://bucket/path/to/file.pdf", file=sys.stderr)
        return 2

    try:
        output_s3 = process_pdf(sys.argv[1])
    except Exception as exc:
        log.error(str(exc))
        return 1

    print(f"\nDone! Markdown saved to: {output_s3}")
    return 0


if __name__ == "__main__":
    sys.exit(main())