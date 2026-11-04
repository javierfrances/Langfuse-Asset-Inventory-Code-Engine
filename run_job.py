#!/usr/bin/env python3
"""Code Engine entrypoint: Langfuse inventory persisted ONLY in IBM Cloud Object Storage.

A local file is used solely as an ephemeral staging file while the JobRun executes.
There is deliberately no local persistence mode or fallback if COS is unavailable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


DEFAULT_KEY = "inventario_langfuse.json"


def enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        raise ValueError(f"{name} must be true/false")
    return raw in ("true", "1", "yes", "on")


def read_config() -> dict[str, str]:
    names = (
        "LANGFUSE_BASE_URL", "LANGFUSE_PROJECT_ID", "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY", "COS_ENDPOINT", "COS_API_KEY",
        "COS_SERVICE_INSTANCE_ID", "COS_BUCKET",
    )
    cfg = {name: os.getenv(name, "").strip() for name in names}
    cfg["COS_OBJECT_KEY"] = os.getenv("COS_OBJECT_KEY", DEFAULT_KEY).strip()
    missing = [name for name in (*names, "COS_OBJECT_KEY") if not cfg[name]]
    if missing:
        raise ValueError("Missing environment variables: " + ", ".join(missing))
    if not cfg["COS_ENDPOINT"].startswith("https://"):
        raise ValueError("COS_ENDPOINT must start with https://")
    if not cfg["LANGFUSE_BASE_URL"].startswith("https://"):
        raise ValueError("LANGFUSE_BASE_URL must start with https://")
    return cfg


def make_cos_client(cfg: dict[str, str]) -> Any:
    try:
        import ibm_boto3
        from ibm_botocore.client import Config
    except ImportError as exc:
        raise RuntimeError("Install the dependencies from requirements.txt (ibm-cos-sdk)") from exc
    return ibm_boto3.client(
        "s3",
        ibm_api_key_id=cfg["COS_API_KEY"],
        ibm_service_instance_id=cfg["COS_SERVICE_INSTANCE_ID"],
        config=Config(signature_version="oauth"),
        endpoint_url=cfg["COS_ENDPOINT"],
    )


def download_inventory(client: Any, bucket: str, key: str, local_path: Path) -> bool:
    """Download a validated inventory; only a genuinely absent object starts a new one.

    head_bucket first ensures a missing/misconfigured bucket is not mistaken for
    a missing inventory object. IAM/network failures abort the job.
    """
    client.head_bucket(Bucket=bucket)
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        raw = response["Body"].read()
    except Exception as exc:
        error = getattr(exc, "response", {}).get("Error", {})
        if str(error.get("Code")) in ("NoSuchKey", "NoSuchObject", "404"):
            print(f"COS object not found: {bucket}/{key}; starting first inventory.", flush=True)
            return False
        raise
    # Invalid stored JSON must never be overwritten.
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict) or "inventory" not in parsed:
        raise ValueError("Existing COS object is not an asset inventory JSON. Refusing to overwrite it.")
    local_path.write_bytes(raw)
    print(f"Previous inventory downloaded from COS: {bucket}/{key}", flush=True)
    return True


def upload_inventory(client: Any, bucket: str, key: str, local_path: Path) -> None:
    # Ensures the output is valid JSON before upload.
    parsed = json.loads(local_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict) or "inventory" not in parsed:
        raise ValueError("Inventory output is invalid; refusing upload")
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=local_path.read_bytes(),
        ContentType="application/json; charset=utf-8",
    )
    print(f"Inventory persisted in COS: {bucket}/{key}", flush=True)


def run_inventory(local_path: Path) -> None:
    cmd = [sys.executable, str(Path(__file__).with_name("inventariar_langfuse.py")),
           "--output", str(local_path)]
    for var, flag in (("INVENTORY_DAYS", "--days"),
                      ("INVENTORY_FROM", "--from"),
                      ("INVENTORY_TO", "--to"),
                      ("INVENTORY_PAGE_SIZE", "--page-size")):
        if os.getenv(var):
            cmd.extend((flag, os.environ[var]))
    if enabled("INVENTORY_INSPECT_IO"):
        cmd.append("--inspect-io")
    if enabled("INVENTORY_DRY_RUN"):
        cmd.append("--dry-run")
    subprocess.run(cmd, check=True)


def main() -> int:
    cfg = read_config()  # fail before starting if COS or Langfuse credentials are absent
    dry_run = enabled("INVENTORY_DRY_RUN")
    client = make_cos_client(cfg)
    with tempfile.TemporaryDirectory(prefix="langfuse-inventory-") as scratch:
        local_path = Path(scratch) / "inventario_langfuse.json"
        download_inventory(client, cfg["COS_BUCKET"], cfg["COS_OBJECT_KEY"], local_path)
        run_inventory(local_path)  # reads prior inventory, merges without duplicate assets
        if dry_run:
            print("Dry-run: COS inventory was NOT modified.", flush=True)
            return 0
        upload_inventory(client, cfg["COS_BUCKET"], cfg["COS_OBJECT_KEY"], local_path)
        if enabled("INVENTORY_PRINT_JSON"):
            print("--- INVENTORY_JSON_BEGIN ---", flush=True)
            print(local_path.read_text(encoding="utf-8"), flush=True)
            print("--- INVENTORY_JSON_END ---", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"INVENTORY JOB ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
