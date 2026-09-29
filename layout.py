"""正文库路径、索引字段、Release 分包规则。"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sqlite3
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from classify import (
    DOC_TYPES,
    EDITION_NAMES,
    LANG_FOLDERS,
    parse_lang,
    parse_stage,
)

KIND_CODES = {
    "technical-description": "TD",
    "test-project": "TP",
    "infrastructure-list": "IL",
    "skill-management-plan": "SMP",
    "correspondence": "COR",
    "resources": "RES",
    "results": "RST",
    "competition-documents": "DOC",
    "official-document": "OFF",
    "skill-resource": "SKL",
}

KIND_FROM_LABEL = {label: KIND_CODES[key] for key, label in DOC_TYPES.items()}
KIND_FROM_LABEL.update(KIND_CODES)
KIND_FROM_LABEL.update({code: code for code in KIND_CODES.values()})

STAGE_CODES = {"正式": "actual", "赛前": "pre", "提案": "proposal", "actual": "actual", "pre": "pre", "proposal": "proposal"}

EDITION_SLUGS = {
    "WSC2001": "2001-seoul",
    "WSC2003": "2003-st-gallen",
    "WSC2005": "2005-helsinki",
    "WSC2007": "2007-shizuoka",
    "WSC2009": "2009-calgary",
    "WSC2011": "2011-london",
    "WSC2013": "2013-leipzig",
    "WSC2015": "2015-sao-paulo",
    "WSC2017": "2017-abu-dhabi",
    "WSC2019": "2019-kazan",
    "WSC2022": "2022-shanghai",
    "WSC2022SE": "2022-special-edition",
    "WSC2024": "2024-lyon",
    "WSC2026": "2026-shanghai",
}

LANG_FROM_LABEL = {label: code.split("_")[0] for code, label in LANG_FOLDERS.items()}
LANG_FROM_LABEL["未标注"] = "und"
LANG_FROM_LABEL["und"] = "und"
LANG_FROM_LABEL["mul"] = "mul"

KIND_ASSET = {
    "TD": "td",
    "TP": "tp",
    "IL": "il",
    "SMP": "smp",
    "COR": "cor",
    "RES": "other",
    "RST": "other",
    "DOC": "other",
    "OFF": "other",
    "SKL": "other",
}

MAX_ASSET_BYTES = 1_800_000_000
STORED_SUFFIXES = {".zip", ".gz", ".7z", ".rar", ".bz2", ".xz"}


def kind_code(doc_type: str | None) -> str:
    if not doc_type:
        return "RES"
    return KIND_FROM_LABEL.get(doc_type, "RES")


def stage_code(stage: str | None, filename: str = "") -> str | None:
    if stage:
        return STAGE_CODES.get(stage)
    parsed = parse_stage(filename)
    if parsed:
        return STAGE_CODES.get(parsed)
    return None


def lang_code(language: str | None, filename: str = "") -> str:
    if language and language not in {"未标注", ""}:
        mapped = LANG_FROM_LABEL.get(language, language.lower()[:3])
        if mapped:
            return mapped
    parsed = parse_lang(filename)
    return parsed or "und"


def skill_code(number: str | None) -> str:
    if not number:
        return "xx"
    text = str(number).strip()
    if text.isdigit():
        return f"{int(text):02d}"
    return re.sub(r"[^A-Za-z0-9]+", "", text) or "xx"


def edition_slug(code: str | None) -> str:
    if not code:
        return "unknown"
    return EDITION_SLUGS.get(code, code.lower())


def release_tag(code: str | None) -> str:
    return f"wsc-{edition_slug(code)}"


def safe_part(name: str) -> str:
    text = re.sub(r'[<>:"/\\|?*]', "_", str(name)).replace("\x00", "").strip(" .")
    return text or "_"


def store_relpath(
    *,
    edition_code: str | None,
    kind: str,
    skill: str,
    stage: str | None,
    lang: str,
    filename: str,
) -> Path:
    parts = [
        safe_part(edition_code or "UNKNOWN"),
        safe_part(kind),
        safe_part(skill),
    ]
    if kind == "TP" and stage:
        parts.append(safe_part(stage))
    parts.append(safe_part(lang or "und"))
    parts.append(safe_part(filename))
    return Path(*parts)


def unique_relpath(rel: str, used: dict[str, str], key: str) -> str:
    if rel not in used or used[rel] == key:
        used[rel] = key
        return rel
    path = Path(rel)
    stem, suffix = path.stem, path.suffix
    for index in range(2, 1000):
        candidate = (path.parent / f"{stem}-{index}{suffix}").as_posix()
        if candidate not in used or used[candidate] == key:
            used[candidate] = key
            return candidate
    raise RuntimeError(f"无法为 {rel} 分配不重复路径")


def sha256_file(path: Path, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = limit
        while True:
            chunk_size = 1024 * 1024
            if remaining is not None:
                chunk_size = min(chunk_size, remaining)
                if chunk_size <= 0:
                    break
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def row_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    return {key: row[key] for key in row.keys()}


def record_from_row(row: sqlite3.Row | dict[str, Any], filename_hint: str | None = None) -> dict[str, Any]:
    data = row_dict(row)
    filename = filename_hint or data.get("filename") or "file.bin"
    edition = data.get("edition_code") or ""
    kind = kind_code(data.get("doc_type"))
    skill = skill_code(data.get("skill_number"))
    stage = stage_code(data.get("stage"), filename) if kind == "TP" else None
    lang = lang_code(data.get("language"), filename)
    computed = store_relpath(
        edition_code=edition or "UNKNOWN",
        kind=kind,
        skill=skill,
        stage=stage,
        lang=lang,
        filename=filename,
    ).as_posix()
    local = str(data.get("local_path") or "").replace("\\", "/")
    store_path = local[len("store/") :] if local.startswith("store/") else computed
    return {
        "edition": edition,
        "edition_name": data.get("edition_name") or EDITION_NAMES.get(edition, edition),
        "kind": kind,
        "skill": skill,
        "skill_name": (data.get("skill_name") or "").strip(),
        "stage": stage or "",
        "lang": lang,
        "filename": filename,
        "bytes": int(data.get("bytes") or 0),
        "sha256": data.get("sha256") or "",
        "url": data.get("url") or "",
        "state": data.get("state") or "",
        "asset": data.get("asset") or "",
        "release": release_tag(edition),
        "store_path": store_path,
        "key": data.get("key") or data.get("url") or filename,
    }


def finalize_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used: dict[str, str] = {}
    for item in records:
        item["store_path"] = unique_relpath(item["store_path"], used, str(item.get("key") or item["store_path"]))
    plan_assets(records)
    return records


def write_indexes(root: Path, records: Iterable[dict[str, Any]]) -> None:
    items = [item for item in records if item.get("state") == "done"]
    index_dir = root / "indexes"
    for sub in ("by-skill", "by-edition", "by-language"):
        folder = index_dir / sub
        if folder.exists():
            for stale in folder.glob("*.json"):
                stale.unlink()
    by_skill: dict[str, list] = defaultdict(list)
    by_edition: dict[str, list] = defaultdict(list)
    by_lang: dict[str, list] = defaultdict(list)
    skill_names: dict[str, dict[str, str]] = defaultdict(dict)
    for item in items:
        by_skill[item["skill"]].append(item)
        by_edition[item["edition"] or "UNKNOWN"].append(item)
        by_lang[item["lang"]].append(item)
        if item["skill"] != "xx" and item["skill_name"]:
            skill_names[item["skill"]][item["edition"]] = item["skill_name"]

    (index_dir / "by-skill").mkdir(parents=True, exist_ok=True)
    (index_dir / "by-edition").mkdir(parents=True, exist_ok=True)
    (index_dir / "by-language").mkdir(parents=True, exist_ok=True)

    def dump(path: Path, payload: Any) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def public_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{key: value for key, value in item.items() if key != "key"} for item in files]

    for skill, files in by_skill.items():
        dump(
            index_dir / "by-skill" / f"{skill}.json",
            {"skill": skill, "names": skill_names.get(skill, {}), "files": public_files(files)},
        )
    for edition, files in by_edition.items():
        dump(
            index_dir / "by-edition" / f"{edition}.json",
            {
                "edition": edition,
                "name": files[0]["edition_name"] if files else edition,
                "release": files[0]["release"] if files else release_tag(edition),
                "files": public_files(files),
            },
        )
    for lang, files in by_lang.items():
        dump(index_dir / "by-language" / f"{lang}.json", {"lang": lang, "count": len(files), "files": public_files(files)})

    catalog_path = index_dir / "catalog.csv"
    fields = [
        "edition",
        "edition_name",
        "kind",
        "skill",
        "skill_name",
        "stage",
        "lang",
        "filename",
        "bytes",
        "sha256",
        "store_path",
        "url",
        "release",
        "asset",
        "state",
    ]
    with catalog_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in sorted(items, key=lambda row: (row["edition"], row["kind"], row["skill"], row["filename"])):
            writer.writerow(item)

    summary = {
        "files": len(items),
        "editions": sorted(by_edition),
        "skills": sorted(k for k in by_skill if k != "xx"),
        "languages": sorted(by_lang),
        "kinds": sorted({item["kind"] for item in items}),
        "releases": sorted({item["release"] for item in items}),
        "assets": sorted({item.get("asset") for item in items if item.get("asset")}),
    }
    dump(index_dir / "summary.json", summary)


def plan_assets(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        if item.get("state") != "done":
            continue
        groups[(item["release"], KIND_ASSET.get(item["kind"], "other"))].append(item)
    assets: list[dict[str, Any]] = []
    for (tag, bucket), files in sorted(groups.items()):
        files = sorted(files, key=lambda item: item["store_path"])
        part = 1
        current: list[dict[str, Any]] = []
        current_bytes = 0
        parts_needed = sum(int(item["bytes"] or 0) for item in files) > MAX_ASSET_BYTES

        def flush() -> None:
            nonlocal part, current, current_bytes
            if not current:
                return
            suffix = f"-part{part:02d}" if parts_needed or part > 1 else ""
            name = f"{tag}-{bucket}{suffix}.zip"
            for item in current:
                item["asset"] = name
            assets.append({"release": tag, "asset": name, "bytes": current_bytes, "files": len(current)})
            part += 1
            current = []
            current_bytes = 0

        for item in files:
            size = int(item["bytes"] or 0)
            if current and current_bytes + size > MAX_ASSET_BYTES:
                flush()
            current.append(item)
            current_bytes += size
        flush()
    return assets


def compression_for(path: Path) -> int:
    return zipfile.ZIP_STORED if path.suffix.lower() in STORED_SUFFIXES else zipfile.ZIP_DEFLATED


def existing_source(root: Path, store: Path, row: sqlite3.Row | dict[str, Any]) -> Path | None:
    data = row_dict(row)
    record = record_from_row(data)
    candidates = []
    local = data.get("local_path") or ""
    if local:
        candidates.append(root / local)
    candidates.append(store / record["store_path"])
    seen: set[Path] = set()
    for path in candidates:
        resolved = path if not path.exists() else path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.exists() and path.is_file():
            return path
    return None


def place_file(src: Path, dest: Path, *, dry_run: bool = False) -> Path:
    if src.exists() and dest.exists() and src.resolve() == dest.resolve():
        return dest
    if dest.exists():
        digest = sha256_file(src)
        if sha256_file(dest) == digest:
            if not dry_run and src.exists() and src.resolve() != dest.resolve():
                src.unlink()
            return dest
        dest = dest.with_name(f"{dest.stem}-{digest[:8]}{dest.suffix}")
        return place_file(src, dest, dry_run=dry_run)
    if dry_run:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


def prune_empty_dirs(start: Path, stop: Path) -> None:
    try:
        current = start.resolve()
        limit = stop.resolve()
    except FileNotFoundError:
        return
    while current != limit and limit in current.parents:
        if not current.is_dir():
            return
        try:
            next(current.iterdir())
            return
        except StopIteration:
            parent = current.parent
            current.rmdir()
            current = parent
        except FileNotFoundError:
            return


def pack_releases(
    store: Path,
    dist: Path,
    records: list[dict[str, Any]],
    *,
    edition: str | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    selected = [item for item in records if item.get("state") == "done"]
    if edition:
        selected = [item for item in selected if item["edition"] == edition]
    if kind:
        selected = [item for item in selected if item["kind"] == kind.upper()]
    plan_assets(selected)
    dist.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in selected:
        if item.get("asset"):
            grouped[item["asset"]].append(item)
    written: list[dict[str, Any]] = []
    for asset_name, files in sorted(grouped.items()):
        zip_path = dist / asset_name
        packed = 0
        with zipfile.ZipFile(zip_path, "w") as archive:
            for item in files:
                src = store / item["store_path"]
                if not src.is_file():
                    continue
                archive.write(src, item["store_path"], compress_type=compression_for(src))
                packed += 1
        written.append(
            {
                "release": files[0]["release"] if files else "",
                "asset": asset_name,
                "files": packed,
                "bytes": zip_path.stat().st_size,
                "path": str(zip_path),
            }
        )
    manifest_path = dist / "manifest.json"
    existing: list[dict[str, Any]] = []
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except json.JSONDecodeError:
            existing = []
    merged = {item["asset"]: item for item in existing if item.get("asset")}
    for item in written:
        merged[item["asset"]] = item
    manifest_path.write_text(
        json.dumps(sorted(merged.values(), key=lambda item: item["asset"]), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return written
