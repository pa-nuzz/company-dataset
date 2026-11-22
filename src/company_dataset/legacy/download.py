from __future__ import annotations

import hashlib
import json
from pathlib import Path

from company_dataset import reference
from company_dataset.http import HttpClient, stream_to
from company_dataset.models import utc_now
from company_dataset.sources import SOURCES


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _has_data(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, "rb") as fh:
        head = fh.read(4096)
    # A CSV with only its header row is an empty harvest, not data.
    return head.count(b"\n") > 1 or path.suffix in (".gz", ".json", ".jsonl")


def run_download(dirs: dict, client: HttpClient | None = None, refresh: bool = False) -> list[dict]:
    client = client or HttpClient(timeout=120.0)
    raw_dir = dirs["raw"]
    manifest_path = dirs["intermediate"] / "manifest.json"
    previous = {}
    if manifest_path.exists():
        try:
            previous = {e["source"]: e for e in json.loads(manifest_path.read_text(encoding="utf-8"))}
        except (ValueError, KeyError):
            previous = {}
    entries = []
    modules = [(mod, "boards") for mod in SOURCES] + [(reference, "reference")]
    for mod, role in modules:
        meta = mod.META
        dest = raw_dir / meta.name / meta.filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "source": meta.name,
            "role": role,
            "description": meta.description,
            "download_url": meta.download_url,
            "license": meta.license,
            "license_url": meta.license_url,
            "license_note": meta.license_note,
            "attribution": meta.attribution,
            "path": str(dest),
            "downloaded_at": previous.get(meta.name, {}).get("downloaded_at"),
        }
        needs = refresh or not _has_data(dest)
        if not meta.downloadable and not hasattr(mod, "run_download"):
            if not _has_data(dest):
                entry["error"] = "not downloadable: harvested once, raw file kept outside the pipeline"
            needs = False
        if needs:
            try:
                if hasattr(mod, "run_download"):
                    mod.run_download(raw_dir / meta.name, client)
                    status = 200 if _has_data(dest) else 0
                else:
                    status = stream_to(client, meta.download_url, dest)
            except Exception as exc:
                status = 0
                entry["error"] = str(exc)[:300]
            if status != 200:
                entry.setdefault("error", f"download failed (status {status})")
                print(f"[download] FAILED {meta.name}: {entry['error']}")
            else:
                entry["downloaded_at"] = utc_now()
                print(f"[download] {meta.name}: {dest.stat().st_size} bytes")
        if _has_data(dest):
            entry["sha256"] = _sha256(dest)
            entry["size_bytes"] = dest.stat().st_size
        entries.append(entry)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
    return entries
