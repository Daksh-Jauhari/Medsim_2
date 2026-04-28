"""
One-time helper: fetch the LeTourneau seed splat from World Labs and upload
it to R2 under the key the in-memory IRIS seed expects.

After running this once, GET /api/models/unit_1/splat/stream will succeed
and the demo viewer will render the real Gaussian splat instead of 502'ing.

Usage (from the project root):
    python -m scripts.seed_splat
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from backend.config import get_settings
from backend.db.r2_client import r2_client
from backend.pipeline.world_model import _extract_spz_url


SEED_WORLD_ID = "a97601cc-31b4-418f-a247-35ac9c9b8cb0"
SEED_R2_KEY = f"facilities/fac_demo/models/{SEED_WORLD_ID}/scene.spz"


async def fetch_world_payload(api_base: str, world_id: str, api_key: str) -> dict:
    """
    Try the most likely World Labs endpoints for retrieving an existing
    world's metadata. World Labs sometimes treats world_id and operation_id
    interchangeably; we try operations first, then a worlds path.
    """
    candidates = [
        f"{api_base}/marble/v1/operations/{world_id}",
        f"{api_base}/marble/v1/worlds/{world_id}",
    ]
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=60) as client:
        for url in candidates:
            try:
                response = await client.get(
                    url,
                    headers={"WLT-Api-Key": api_key},
                )
                if response.status_code == 404:
                    last_error = RuntimeError(f"{url} → 404")
                    continue
                response.raise_for_status()
                payload = response.json()
                # operations endpoint wraps the world under "response"
                world = payload.get("response") or payload
                if world.get("assets") or world.get("world_id"):
                    return world
                last_error = RuntimeError(f"{url} returned no assets")
            except Exception as exc:
                last_error = exc
    raise RuntimeError(f"Could not retrieve world {world_id}: {last_error}")


async def main() -> int:
    settings = get_settings()
    if not settings.world_labs_api_key:
        print("ERROR: WORLD_LABS_API_KEY not set in .env", file=sys.stderr)
        return 1
    if not r2_client.enabled:
        print("ERROR: R2 credentials not set in .env", file=sys.stderr)
        return 1

    api_base = settings.world_labs_api_base.rstrip("/")
    print(f"Fetching seed world {SEED_WORLD_ID} from {api_base}...")
    try:
        world = await fetch_world_payload(api_base, SEED_WORLD_ID, settings.world_labs_api_key)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "\nThe World Labs world for the demo seed may have expired, been deleted, "
            "or your API key may not have access to it. As a workaround, create your "
            "own facility through /facility/new and the pipeline will generate a fresh "
            "splat tied to that facility's unit.",
            file=sys.stderr,
        )
        return 2

    spz_url, caption, _thumb = _extract_spz_url(world)
    print(f"Caption: {caption}")
    print(f"Downloading splat from: {spz_url[:80]}...")
    async with httpx.AsyncClient(timeout=300) as client:
        asset = await client.get(spz_url)
        asset.raise_for_status()

    content_type = asset.headers.get("content-type", "application/octet-stream")
    size_kb = len(asset.content) / 1024
    print(f"Got {size_kb:.0f} KB ({content_type}). Uploading to R2 key: {SEED_R2_KEY}")
    r2_client.upload_bytes(SEED_R2_KEY, asset.content, content_type=content_type)
    print("Done. /api/models/unit_1/splat/stream should now work.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
