"""
Rebrand any splat already in your R2 bucket as the LeTourneau seed.

The seed model record in iris_client.py expects a splat at:
    facilities/fac_demo/models/a97601cc-.../scene.spz

But the original developers' splat lives in their R2, not yours. This
script lists every .spz already in your bucket (any facility you've
created via /facility/new), shows them, and copies the one you pick
to the seed key. After running, the LeTourneau viewer will stream
your own world model under the LeTourneau branding/findings.

Usage:
    python -m scripts.rebrand_seed             # list all .spz files
    python -m scripts.rebrand_seed --pick N    # copy item N to seed key
    python -m scripts.rebrand_seed --pick latest  # copy newest .spz
"""
from __future__ import annotations

import argparse
import sys

import boto3

from backend.config import get_settings


SEED_R2_KEY = "facilities/fac_demo/models/a97601cc-31b4-418f-a247-35ac9c9b8cb0/scene.spz"


def _client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def list_spz_objects() -> list[dict]:
    settings = get_settings()
    s3 = _client()
    paginator = s3.get_paginator("list_objects_v2")
    out: list[dict] = []
    for page in paginator.paginate(Bucket=settings.r2_bucket_name):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            if key.endswith((".spz", ".ksplat", ".splat", ".ply")):
                out.append({"key": key, "size": obj["Size"], "modified": obj["LastModified"]})
    return sorted(out, key=lambda o: o["modified"], reverse=True)


def copy_to_seed(source_key: str) -> None:
    settings = get_settings()
    s3 = _client()
    bucket = settings.r2_bucket_name
    print(f"Copying {source_key} -> {SEED_R2_KEY}")
    s3.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": source_key},
        Key=SEED_R2_KEY,
    )
    print("Done. /api/models/unit_1/splat/stream will now serve this splat.")
    print("Restart the backend (Ctrl+C, re-run uvicorn) and reload the viewer.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pick", help='Index from the list, or "latest" to pick the newest .spz')
    args = parser.parse_args()

    splats = list_spz_objects()
    if not splats:
        print("No .spz files found in your R2 bucket.", file=sys.stderr)
        print(
            "\nCreate a facility through /facility/new first. Once World Labs "
            "finishes generating a splat for it, the .spz will be uploaded to "
            "R2 and you can run this script again to rebrand it as the seed.",
            file=sys.stderr,
        )
        return 1

    print(f"Found {len(splats)} splat asset(s) in R2:")
    for i, obj in enumerate(splats):
        size_mb = obj["size"] / (1024 * 1024)
        print(f"  [{i}] {obj['key']}")
        print(f"      {size_mb:.1f} MB · modified {obj['modified']}")

    if not args.pick:
        print("\nRe-run with --pick <index> or --pick latest to copy one of these to the seed key.")
        return 0

    if args.pick == "latest":
        chosen = splats[0]
    else:
        try:
            chosen = splats[int(args.pick)]
        except (ValueError, IndexError):
            print(f"Invalid --pick value: {args.pick}", file=sys.stderr)
            return 1

    copy_to_seed(chosen["key"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
