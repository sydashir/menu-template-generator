"""
S3 Asset Library — resolves semantic graphic element labels to high-quality PNG bytes.

Instead of relying solely on pixel crops from source menus (which suffer from JPEG
artifacts, halos, and low resolution), this module matches Claude's `semantic_label`
slugs to a curated library of clean PNG assets hosted on S3.

Lookup priority:
  1. In-process LRU memory cache (fastest)
  2. Local disk cache at ~/.menu_assets_cache/ (avoids repeat S3 downloads)
  3. S3 bucket fetch

Usage:
    from s3_asset_library import resolve_asset, list_assets, upload_asset

    png_bytes = resolve_asset("badge/food_network")   # → bytes or None
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import functools
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Local asset override folder — drop PNGs here (e.g. from upload_assets.py --dry-run)
# Pipeline uses these directly without needing S3 connectivity.
_LOCAL_ASSETS_DIR = Path(os.environ.get(
    "MENU_LOCAL_ASSETS_DIR",
    Path(__file__).parent / "local_assets"
))

# ---------------------------------------------------------------------------
# Config (from environment)
# ---------------------------------------------------------------------------

_AWS_ACCESS_KEY_ID     = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
_AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
_AWS_REGION            = os.environ.get("AWS_S3_REGION_NAME", os.environ.get("AWS_REGION", "eu-north-1")).strip()
_S3_BUCKET             = os.environ.get("AWS_STORAGE_BUCKET_NAME", os.environ.get("S3_BUCKET", "djangobucketfab01")).strip()
_S3_PREFIX             = os.environ.get("S3_ASSET_PREFIX", "menu-assets/v1").strip().strip("/")

# Local disk cache directory
_DISK_CACHE_DIR = Path(os.environ.get("MENU_ASSET_CACHE_DIR", Path.home() / ".menu_assets_cache"))

# Max in-process LRU entries (~200 assets at avg 50KB each ≈ 10MB cap)
_LRU_MAXSIZE = 200

# ---------------------------------------------------------------------------
# Canonical label catalogue
# Known S3 keys under {S3_PREFIX}/  (without .png extension)
# ---------------------------------------------------------------------------

KNOWN_LABELS: dict[str, list[str]] = {
    # Badges — brand circl es / icons
    "badge/food_network":              ["food_network", "food network", "food network circle"],
    "badge/opentable_diners_choice":   ["opentable", "opentable diners choice", "diners choice", "diners' choice"],
    "badge/youtube":                   ["youtube", "youtube button", "yt"],
    "badge/hulu":                      ["hulu", "hulu logo"],
    "badge/tripadvisor":               ["tripadvisor", "trip advisor", "tripadvisor owl"],
    "badge/yelp":                      ["yelp", "yelp logo"],
    "badge/michelin":                  ["michelin", "michelin star", "michelin guide"],
    "badge/zagat":                     ["zagat", "zagat guide"],
    "badge/best_of":                   ["best of", "best of award"],

    # Ornaments — calligraphic / decorative divider swashes
    "ornament/floral_swash_centered":  ["floral swash", "centered swash", "symmetrical swash", "calligraphic flourish"],
    "ornament/floral_swash_left":      ["left swash", "left flourish"],
    "ornament/calligraphic_rule":      ["calligraphic rule", "calligraphic separator"],
    "ornament/diamond_rule":           ["diamond rule", "diamond separator", "diamond divider"],
    "ornament/vine_separator":         ["vine", "vine separator", "botanical separator"],
    "ornament/scroll_divider":         ["scroll divider", "scroll separator"],

    # Separators — decorative line styles
    "separator/wavy_line":             ["wavy line", "wavy rule", "wave separator"],
    "separator/double_line":           ["double line", "double rule"],
    "separator/dotted_ornament":       ["dotted ornament", "dot separator"],
}

# Reverse index: lowercase phrase → canonical label
_PHRASE_TO_LABEL: dict[str, str] = {}
for _label, _phrases in KNOWN_LABELS.items():
    for _phrase in _phrases:
        _PHRASE_TO_LABEL[_phrase.lower()] = _label


# ---------------------------------------------------------------------------
# Fuzzy label normalisation
# ---------------------------------------------------------------------------

def normalise_label(raw: Optional[str]) -> Optional[str]:
    """
    Map a raw string from Claude (e.g. 'badge/food_network' or 'Food Network circle')
    to a canonical label key, or None if unrecognised.
    """
    if not raw:
        return None
    cleaned = raw.strip().lower()

    # Direct match (Claude already uses canonical slug)
    if cleaned in KNOWN_LABELS:
        return cleaned

    # Phrase match in reverse index
    if cleaned in _PHRASE_TO_LABEL:
        return _PHRASE_TO_LABEL[cleaned]

    # Partial match — check if any known phrase is a substring of the raw text
    for phrase, label in _PHRASE_TO_LABEL.items():
        if phrase in cleaned:
            return label

    # Prefix-type normalisation: 'food_network' → 'badge/food_network'
    for label in KNOWN_LABELS:
        slug = label.split("/", 1)[-1]
        if slug in cleaned.replace(" ", "_").replace("-", "_"):
            return label

    return None


# ---------------------------------------------------------------------------
# S3 client (lazy singleton)
# ---------------------------------------------------------------------------

_s3_client = None
_s3_available = None   # None = not tested, True/False = result


def _get_s3():
    """Return a boto3 S3 client, or None if boto3 / credentials are unavailable."""
    global _s3_client, _s3_available
    if _s3_available is False:
        return None
    if _s3_client is not None:
        return _s3_client
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError  # noqa: F401
        kwargs: dict = {"region_name": _AWS_REGION}
        if _AWS_ACCESS_KEY_ID and _AWS_SECRET_ACCESS_KEY:
            kwargs["aws_access_key_id"]     = _AWS_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = _AWS_SECRET_ACCESS_KEY
        _s3_client = boto3.client("s3", **kwargs)
        _s3_available = True
        logger.info("[s3_assets] S3 client initialised (bucket=%s, prefix=%s)", _S3_BUCKET, _S3_PREFIX)
        return _s3_client
    except ImportError:
        logger.warning("[s3_assets] boto3 not installed — S3 lookups disabled. Run: pip install boto3")
        _s3_available = False
        return None
    except Exception as exc:
        logger.warning("[s3_assets] S3 client init failed: %s", exc)
        _s3_available = False
        return None


# ---------------------------------------------------------------------------
# Disk cache helpers
# ---------------------------------------------------------------------------

def _disk_cache_path(label: str) -> Path:
    safe = label.replace("/", "__")
    return _DISK_CACHE_DIR / f"{safe}.png"


def _read_disk_cache(label: str) -> Optional[bytes]:
    p = _disk_cache_path(label)
    if p.is_file():
        try:
            return p.read_bytes()
        except OSError:
            pass
    return None


def _write_disk_cache(label: str, data: bytes) -> None:
    try:
        _DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _disk_cache_path(label).write_bytes(data)
    except OSError as exc:
        logger.debug("[s3_assets] disk cache write failed (%s): %s", label, exc)


# ---------------------------------------------------------------------------
# Local asset folder helpers (local_assets/ in project root)
# ---------------------------------------------------------------------------

def _local_asset_path(label: str) -> Path:
    """Map label e.g. 'ornament/floral_swash_centered' → local_assets/floral_swash_centered.png"""
    slug = label.split("/", 1)[-1]
    return _LOCAL_ASSETS_DIR / f"{slug}.png"


def _read_local_asset(label: str) -> Optional[bytes]:
    """Read PNG bytes from local_assets/ folder, or None if not present."""
    p = _local_asset_path(label)
    if p.is_file():
        try:
            data = p.read_bytes()
            logger.debug("[s3_assets] local_assets hit: %s (%d bytes)", p.name, len(data))
            return data
        except OSError:
            pass
    return None


# ---------------------------------------------------------------------------
# LRU in-process cache
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=_LRU_MAXSIZE)
def _lru_fetch(canonical_label: str) -> Optional[bytes]:
    """
    Inner cached fetch — checks local_assets/ → disk cache → S3.
    LRU cache is keyed on canonical_label (a hashable string).
    """
    # 1. local_assets/ directory (generated by upload_assets.py or manually placed)
    local = _read_local_asset(canonical_label)
    if local is not None:
        return local

    # 2. Disk cache
    cached = _read_disk_cache(canonical_label)
    if cached is not None:
        logger.debug("[s3_assets] disk cache hit: %s", canonical_label)
        return cached

    # 3. S3 fetch
    s3 = _get_s3()
    if s3 is None:
        return None

    s3_key = f"{_S3_PREFIX}/{canonical_label}.png"
    try:
        resp = s3.get_object(Bucket=_S3_BUCKET, Key=s3_key)
        data = resp["Body"].read()
        logger.info("[s3_assets] S3 hit: s3://%s/%s (%d bytes)", _S3_BUCKET, s3_key, len(data))
        _write_disk_cache(canonical_label, data)
        return data
    except Exception as exc:
        # Safe error code extraction — EndpointConnectionError has no .response
        try:
            err_code = exc.response.get("Error", {}).get("Code", "")  # type: ignore[attr-defined]
        except AttributeError:
            err_code = ""
        if err_code == "NoSuchKey":
            logger.debug("[s3_assets] S3 miss: %s", s3_key)
        elif "EndpointConnection" in type(exc).__name__ or "NameResolution" in str(exc):
            # No network — mark S3 as unavailable so we skip retries on future calls
            global _s3_available
            _s3_available = False
            logger.warning("[s3_assets] S3 unreachable — disabling S3 lookups, using local_assets only")
        else:
            logger.warning("[s3_assets] S3 fetch error (%s): %s", s3_key, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_asset(raw_label: Optional[str]) -> Optional[bytes]:
    """
    Resolve a semantic_label from Claude to clean PNG bytes from S3.

    Args:
        raw_label: The semantic_label string from Claude's graphic_elements output,
                   e.g. 'badge/food_network' or 'Food Network circle'.

    Returns:
        PNG bytes if a matching high-quality asset exists in S3, else None.

    On None return, the caller should fall back to pixel crop from the source image.
    """
    canonical = normalise_label(raw_label)
    if canonical is None:
        logger.debug("[s3_assets] no canonical match for: %r", raw_label)
        return None
    return _lru_fetch(canonical)


def list_assets() -> list[str]:
    """List all canonical label slugs that exist in the S3 bucket."""
    s3 = _get_s3()
    if s3 is None:
        return list(KNOWN_LABELS.keys())

    prefix = f"{_S3_PREFIX}/"
    try:
        paginator = s3.get_paginator("list_objects_v2")
        slugs = []
        for page in paginator.paginate(Bucket=_S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # key = "menu-assets/v1/badge/food_network.png"
                rel = key[len(prefix):]          # "badge/food_network.png"
                if rel.endswith(".png"):
                    slugs.append(rel[:-4])        # "badge/food_network"
        return slugs
    except Exception as exc:
        logger.warning("[s3_assets] list_assets failed: %s", exc)
        return []


def upload_asset(label: str, png_path: str) -> bool:
    """
    Upload a local PNG file to the S3 asset library under the canonical label key.

    Args:
        label:    Canonical label e.g. 'badge/food_network'
        png_path: Absolute path to local PNG file.

    Returns True on success, False on failure.
    """
    s3 = _get_s3()
    if s3 is None:
        logger.error("[s3_assets] upload_asset: S3 unavailable")
        return False

    p = Path(png_path)
    if not p.is_file():
        logger.error("[s3_assets] upload_asset: file not found: %s", png_path)
        return False

    s3_key = f"{_S3_PREFIX}/{label}.png"
    try:
        s3.upload_file(
            str(p), _S3_BUCKET, s3_key,
            ExtraArgs={"ContentType": "image/png", "ACL": "public-read"},
        )
        logger.info("[s3_assets] uploaded: %s → s3://%s/%s", png_path, _S3_BUCKET, s3_key)
        # Invalidate LRU cache for this label so next resolve gets fresh data
        _lru_fetch.cache_clear()
        # Write to disk cache immediately
        _write_disk_cache(label, p.read_bytes())
        return True
    except Exception as exc:
        logger.error("[s3_assets] upload failed (%s): %s", s3_key, exc)
        return False


def clear_cache(label: Optional[str] = None) -> None:
    """
    Clear the in-process LRU cache and optionally the disk cache.
    Pass label=None to clear everything, or a specific label to remove just that entry.
    """
    _lru_fetch.cache_clear()
    if label is None:
        # Clear all disk cache files
        if _DISK_CACHE_DIR.is_dir():
            for f in _DISK_CACHE_DIR.glob("*.png"):
                try:
                    f.unlink()
                except OSError:
                    pass
    else:
        p = _disk_cache_path(label)
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass


def asset_url(label: str) -> str:
    """Return the public S3 URL for a label (useful for debugging)."""
    region = _AWS_REGION
    bucket = _S3_BUCKET
    key = f"{_S3_PREFIX}/{label}.png"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
