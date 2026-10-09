#!/usr/bin/env python3
"""用你自己的会员登录，把能拿到的竞赛资料、资源库和名单下载并按技能、赛事、类型、语言归档。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from classify import (
    CMS_SECTION_TO_DOC,
    CMS_SLUG_TO_CODE,
    EDITION_EVENT_IDS,
    EDITION_NAMES,
    GLOBAL_CODE,
    RESOURCE_TYPE_TO_DOC,
    Classified,
    classify,
    code_from_event,
    dedupe_event_code,
    doc_folder,
    edition_name,
    lang_folder,
    member_area_code,
    pad_skill,
    parse_lang,
    parse_list_title,
    register_edition,
)
from layout import (
    KIND_FROM_LABEL,
    existing_source,
    finalize_records,
    kind_code,
    pack_releases,
    place_file,
    prune_empty_dirs,
    record_from_row,
    release_tag,
    sha256_file,
    unique_relpath,
    write_indexes,
)
from tables import refresh_tables, update_resource_catalog

ROOT = Path(__file__).resolve().parent
SESSION_DIR = ROOT / ".session"
STORAGE_STATE = SESSION_DIR / "storage_state.json"
TOKEN_PATH = SESSION_DIR / "access_token"
DOWNLOADS = ROOT / "downloads"
ARCHIVE = DOWNLOADS / "archive"
STORE = ROOT / "store"
DIST = ROOT / "dist"
DB_PATH = DOWNLOADS / "catalog.sqlite"
CATALOG_CSV = DOWNLOADS / "catalog.csv"
FORBIDDEN_CSV = DOWNLOADS / "forbidden.csv"
UNIDENTIFIED_CSV = DOWNLOADS / "unidentified.csv"

API = "https://api.worldskills.org"
CMS = "https://worldskills.org"
INTERNAL_DOCS = f"{CMS}/internal/competition-documentation/"
PUBLIC_PAGES = (
    ("https://worldskills.org/about/", "report"),
    ("https://worldskills.org/media/member-resources/", "skill-resource"),
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
TD_LANGS = ("en", "zh", "de", "es", "fr", "ja", "ko", "pt", "fi", "ru", "ar")
MAX_BYTES = 1_800_000_000
MAX_WORKERS = 8
DEFAULT_WORKERS = 4


class LoginRequired(RuntimeError):
    pass


def text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value)


def safe_component(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name).replace("\x00", "").strip(" .")
    if name in {"", ".", ".."}:
        name = "_"
    encoded = name.encode("utf-8", errors="ignore")
    if len(encoded) > 160:
        digest = hashlib.sha1(encoded).hexdigest()[:8]
        trimmed = encoded[:120].decode("utf-8", errors="ignore").rstrip()
        name = f"{trimmed}-{digest}"
    return name


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in {"tkn", "ccm_token"}]
    pairs.sort()
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(pairs, doseq=True), ""))


RESOURCE_DOWNLOAD_RE = re.compile(r"/resources/download/(\d+)(?:/(\d+)/(\d+))?")


def resource_id_of_url(url: str) -> str | None:
    match = RESOURCE_DOWNLOAD_RE.search(url)
    return match.group(1) if match else None


def resource_download_key(url: str) -> str:
    resource_id = resource_id_of_url(url)
    if resource_id:
        return f"{API}/resources/download/{resource_id}"
    return canonical_url(url)


def resource_download_url(resource_id: int | str) -> str:
    return f"{API}/resources/download/{resource_id}"


def url_has_download_token(url: str) -> bool:
    query = urlparse(url).query.lower()
    return "tkn=" in query or "ccm_token=" in query


def url_rank(url: str) -> int:
    rank = 0
    if url_has_download_token(url):
        rank += 2
    match = RESOURCE_DOWNLOAD_RE.search(url)
    if match and match.group(2):
        rank += 1
    return rank


def absolute_download_url(href: str) -> str:
    if not href:
        return href
    if href.startswith("/resources/"):
        return API + href
    if href.startswith("/"):
        return CMS + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("worldskills.org") and "/resources/download/" in parsed.path:
        return urlunparse(("https", "api.worldskills.org", parsed.path, parsed.params, parsed.query, ""))
    return href


def download_href_from_resource(resource: dict[str, Any]) -> str | None:
    version = latest_version(resource)
    if not version:
        return None
    translations = [item for item in (version.get("translations") or []) if isinstance(item, dict)]
    trans = next((item for item in translations if str(item.get("lang_code") or "").lower().startswith("en")), None)
    trans = trans or (translations[0] if translations else None)
    if not trans:
        return None
    for link in trans.get("links") or []:
        href = link.get("href") if isinstance(link, dict) else None
        if href and link.get("rel") == "download":
            return href
    resource_id = resource.get("id")
    version_id = version.get("id")
    trans_id = trans.get("id")
    lang = str(trans.get("lang_code") or "en").split("_")[0]
    if resource_id and version_id and trans_id:
        return f"{API}/resources/download/{resource_id}/{version_id}/{trans_id}?l={lang}"
    return None


def download_href_from_html(html: str, resource_id: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    needle = f"/resources/download/{resource_id}"
    fallback = None
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "")
        if needle not in href:
            continue
        href = absolute_download_url(href)
        if url_has_download_token(href):
            return href
        fallback = href
    return fallback


def live_download_url(client: httpx.Client, url: str) -> str:
    url = absolute_download_url(url)
    if url_has_download_token(url):
        return url
    resource_id = resource_id_of_url(url)
    if not resource_id:
        return url
    status, detail = api_get(client, f"{API}/resources/{resource_id}")
    if status == 200 and isinstance(detail, dict):
        href = download_href_from_resource(detail)
        if href:
            return absolute_download_url(href)
    return url


def filename_from_disposition(header: str | None) -> str | None:
    if not header:
        return None
    message = EmailMessage()
    message["content-disposition"] = header
    return message.get_filename()


def content_type_of(response: httpx.Response) -> str:
    return (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()


def looks_like_login(response: httpx.Response) -> bool:
    for item in [response.url, *[hist.url for hist in response.history]]:
        host = urlparse(str(item)).netloc.lower()
        path = urlparse(str(item)).path
        if host.endswith("auth.worldskills.org") or path.rstrip("/") == "/login":
            return True
    return False


def classified_path(info: Classified, filename: str) -> Path:
    record = record_from_row(
        {
            "edition_code": info.edition_code,
            "edition_name": info.edition_name,
            "skill_number": info.skill_number,
            "skill_name": info.skill_name,
            "doc_type": info.doc_key,
            "stage": info.stage,
            "language": info.lang_code or "",
            "filename": filename,
        }
    )
    return STORE / record["store_path"]


def load_token() -> str | None:
    if TOKEN_PATH.exists():
        value = TOKEN_PATH.read_text(encoding="utf-8").strip()
        return value or None
    return None


def save_token(token: str) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token.strip() + "\n", encoding="utf-8")
    os.chmod(TOKEN_PATH, 0o600)


def cookies_from_storage() -> httpx.Cookies:
    cookies = httpx.Cookies()
    if not STORAGE_STATE.exists():
        return cookies
    state = json.loads(STORAGE_STATE.read_text(encoding="utf-8"))
    for item in state.get("cookies", []):
        cookies.set(
            item["name"],
            item["value"],
            domain=item.get("domain") or "worldskills.org",
            path=item.get("path") or "/",
        )
    return cookies


def make_client(*, require_login: bool = True) -> httpx.Client:
    token = load_token()
    if require_login and not token:
        raise LoginRequired("还没有登录令牌")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,application/pdf,*/*",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        cookies=cookies_from_storage(),
        headers=headers,
        follow_redirects=True,
        timeout=httpx.Timeout(120.0, connect=20.0),
    )


def api_get(client: httpx.Client, url: str) -> tuple[int, Any]:
    response = client.get(url)
    if looks_like_login(response):
        raise LoginRequired(url)
    if response.status_code == 401:
        raise LoginRequired(url)
    try:
        return response.status_code, response.json()
    except Exception:
        return response.status_code, None


def connect() -> sqlite3.Connection:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            key TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            state TEXT NOT NULL,
            edition_code TEXT,
            edition_name TEXT,
            skill_number TEXT,
            skill_name TEXT,
            doc_type TEXT,
            stage TEXT,
            language TEXT,
            filename TEXT,
            local_path TEXT,
            http_status INTEGER,
            bytes INTEGER,
            error TEXT,
            source TEXT,
            queued_at REAL,
            finished_at REAL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_items_state ON items(state)")
    columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
    if "sha256" not in columns:
        db.execute("ALTER TABLE items ADD COLUMN sha256 TEXT")
    return db


def apply_better_url(db: sqlite3.Connection, key: str, new_url: str, old_url: str, state: str) -> None:
    if url_rank(new_url) <= url_rank(old_url):
        return
    db.execute("UPDATE items SET url = ? WHERE key = ?", (new_url, key))
    if state == "forbidden":
        db.execute(
            "UPDATE items SET state = 'queued', error = NULL, http_status = NULL WHERE key = ?",
            (key,),
        )


def enqueue(db: sqlite3.Connection, item: dict[str, Any]) -> bool:
    key = item["key"]
    url = absolute_download_url(item["url"])
    existing = db.execute("SELECT url, state FROM items WHERE key = ?", (key,)).fetchone()
    if existing:
        apply_better_url(db, key, url, existing["url"], existing["state"])
        return False
    cursor = db.execute(
        """
        INSERT INTO items (
            key, url, state, edition_code, edition_name, skill_number, skill_name,
            doc_type, stage, language, filename, source, queued_at
        ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            url,
            item.get("edition_code"),
            item.get("edition_name"),
            item.get("skill_number"),
            item.get("skill_name"),
            item.get("doc_type"),
            item.get("stage"),
            item.get("language"),
            item.get("filename"),
            item.get("source"),
            time.time(),
        ),
    )
    return cursor.rowcount > 0


def mark(db: sqlite3.Connection, key: str, **fields: Any) -> None:
    fields["finished_at"] = time.time()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    db.execute(f"UPDATE items SET {assignments} WHERE key = ?", (*fields.values(), key))
    db.commit()


def count_state(db: sqlite3.Connection, state: str) -> int:
    row = db.execute("SELECT COUNT(*) AS n FROM items WHERE state = ?", (state,)).fetchone()
    return int(row["n"])


def clamp_workers(value: int) -> int:
    return max(1, min(int(value), MAX_WORKERS))


def resolve_kind_filter(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    mapped = KIND_FROM_LABEL.get(text) or KIND_FROM_LABEL.get(text.upper()) or KIND_FROM_LABEL.get(text.lower())
    if not mapped:
        raise SystemExit(f"不认识的类型 {value}。例如 TP、TD、IL、VID。")
    return mapped


def queue_where(
    edition: str | None = None,
    kind: str | None = None,
    keys: list[str] | None = None,
) -> tuple[str, list[Any]]:
    clauses = ["state = 'queued'"]
    params: list[Any] = []
    if edition:
        clauses.append("edition_code = ?")
        params.append(edition)
    code = resolve_kind_filter(kind)
    if code:
        labels = sorted({name for name, mapped in KIND_FROM_LABEL.items() if mapped == code})
        clauses.append(f"doc_type IN ({','.join('?' * len(labels))})")
        params.extend(labels)
    if keys:
        clauses.append(f"key IN ({','.join('?' * len(keys))})")
        params.extend(keys)
    return " AND ".join(clauses), params


FAILED_KEYS_PATH = DOWNLOADS / "failed-resource-keys.txt"


def resource_keys_from_forbidden_csv() -> list[str]:
    keys: list[str] = []
    if FAILED_KEYS_PATH.exists():
        keys.extend(
            line.strip()
            for line in FAILED_KEYS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("http")
        )
    if FORBIDDEN_CSV.exists():
        with FORBIDDEN_CSV.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                url = row.get("网址") or ""
                if "/resources/download/" in url:
                    keys.append(resource_download_key(url))
    return list(dict.fromkeys(keys))


def claim_next(
    db: sqlite3.Connection,
    edition: str | None = None,
    kind: str | None = None,
    keys: list[str] | None = None,
) -> sqlite3.Row | None:
    where, params = queue_where(edition, kind, keys)
    order = "CASE WHEN doc_type IN ('视频', 'video') THEN 1 ELSE 0 END, queued_at, key"
    while True:
        row = db.execute(
            f"SELECT * FROM items WHERE {where} ORDER BY {order} LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        cursor = db.execute(
            "UPDATE items SET state = 'working' WHERE key = ? AND state = 'queued'",
            (row["key"],),
        )
        db.commit()
        if cursor.rowcount == 1:
            return row


def export_tables(db: sqlite3.Connection, *, public: bool = True) -> None:
    rows = db.execute("SELECT * FROM items ORDER BY edition_code, skill_number, doc_type, filename").fetchall()
    with CATALOG_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["届次", "技能编号", "技能", "类型", "阶段", "语言", "文件名", "本地路径", "状态", "字节", "网址"])
        for row in rows:
            writer.writerow(
                [
                    row["edition_name"],
                    row["skill_number"] or "",
                    row["skill_name"] or "",
                    row["doc_type"] or "",
                    row["stage"] or "",
                    row["language"] or "",
                    row["filename"] or "",
                    row["local_path"] or "",
                    row["state"],
                    row["bytes"] or 0,
                    row["url"],
                ]
            )
    with FORBIDDEN_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["届次", "技能", "类型", "说明", "网址"])
        for row in db.execute("SELECT * FROM items WHERE state = 'forbidden'"):
            writer.writerow([row["edition_name"], row["skill_name"] or row["skill_number"], row["doc_type"], row["error"], row["url"]])
    with UNIDENTIFIED_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["届次", "类型", "文件名", "网址"])
        for row in db.execute("SELECT * FROM items WHERE skill_number IS NULL OR skill_number = ''"):
            writer.writerow([row["edition_name"], row["doc_type"], row["filename"], row["url"]])
    if public:
        update_resource_catalog(ROOT, db.execute("SELECT * FROM items").fetchall())
        export_indexes(db)


def items_records(db: sqlite3.Connection) -> list[dict[str, Any]]:
    records = [record_from_row(row) for row in db.execute("SELECT * FROM items")]
    done = [item for item in records if item.get("state") == "done"]
    rest = [item for item in records if item.get("state") != "done"]
    finalize_records(done)
    return done + rest


def export_indexes(db: sqlite3.Connection) -> None:
    write_indexes(ROOT, items_records(db))


def queue_item(
    db: sqlite3.Connection,
    *,
    url: str,
    info: Classified,
    filename: str,
    source: str,
    extra_key: str = "",
) -> bool:
    if not info.edition_code:
        info.edition_code = GLOBAL_CODE
        info.edition_name = edition_name(GLOBAL_CODE)
    if not member_area_code(info.edition_code):
        return False
    key = extra_key or resource_download_key(url)
    return enqueue(
        db,
        {
            "key": key,
            "url": absolute_download_url(url),
            "edition_code": info.edition_code,
            "edition_name": info.edition_name,
            "skill_number": info.skill_number,
            "skill_name": info.skill_name,
            "doc_type": doc_folder(info.doc_key),
            "stage": info.stage,
            "language": lang_folder(info.lang_code),
            "filename": filename,
            "source": source,
        },
    )


class SkillIndex:
    def __init__(self) -> None:
        self.by_event: dict[str, dict[str, str]] = {}
        self.event_ids: dict[str, int] = {}
        self.event_names: dict[int, str] = {}

    def add(self, code: str, number: str, name: str, event_id: int | None = None) -> None:
        padded = pad_skill(number)
        if not padded or not name:
            return
        self.by_event.setdefault(code, {})[padded] = name
        if event_id is not None:
            self.event_ids[code] = event_id
            self.event_names[event_id] = edition_name(code)

    def name(self, code: str | None, number: str | None) -> str | None:
        if not code or not number:
            return None
        return self.by_event.get(code, {}).get(pad_skill(number) or "")


def skill_id_of(row: dict[str, Any]) -> int:
    inner = row.get("skill") if isinstance(row.get("skill"), dict) else None
    if inner and inner.get("id"):
        return int(inner["id"])
    return int(row["id"])


def skill_fields(row: dict[str, Any]) -> tuple[int, str | None, str]:
    inner = row.get("skill") if isinstance(row.get("skill"), dict) else row
    skill_id = int(inner.get("id") or row["id"])
    number = pad_skill(str(inner.get("number") or row.get("number") or ""))
    name = text_of(inner.get("name") or row.get("name"))
    return skill_id, number, name


def load_events(client: httpx.Client, skills: SkillIndex) -> None:
    offset = 0
    seen: set[int] = set()
    occupied: dict[str, int] = {}
    while True:
        status, data = api_get(client, f"{API}/events?limit=100&offset={offset}")
        if status != 200 or not data:
            break
        items = data.get("events") or []
        if not items:
            break
        ids = {event["id"] for event in items}
        if ids & seen:
            break
        seen |= ids
        for event in items:
            name = text_of(event.get("name"))
            event_id = event["id"]
            etype = event.get("type")
            if isinstance(etype, dict):
                etype = etype.get("code") or ""
            code = dedupe_event_code(code_from_event(event.get("code"), name, event_id), event_id, occupied)
            register_edition(code, name or edition_name(code), event_id, str(etype or ""))
            skills.event_ids[code] = event_id
            skills.event_names[event_id] = name or edition_name(code)
        if len(items) < 100:
            break
        offset += 100
    for code, event_id in EDITION_EVENT_IDS.items():
        skills.event_ids.setdefault(code, event_id)
        skills.event_names.setdefault(event_id, edition_name(code))


def load_skill_maps(client: httpx.Client, skills: SkillIndex) -> None:
    for code, event_id in list(skills.event_ids.items()):
        status, data = api_get(client, f"{API}/skillman/skills?event={event_id}")
        rows = []
        if status == 200 and data:
            rows = data.get("skills") or []
        if not rows:
            status, data = api_get(client, f"{API}/events/{event_id}/skills?limit=200")
            if status == 200 and data:
                rows = data.get("skills") or []
        for skill in rows:
            number = pad_skill(str(skill.get("number") or ""))
            name = text_of(skill.get("name"))
            if number and name:
                skills.add(code, number, name, event_id)
        time.sleep(0.05)


def iter_resources(client: httpx.Client, type_id: int, tag: str | None = None):
    offset = 0
    limit = 50
    while True:
        query = f"{API}/resources?type={type_id}&limit={limit}&offset={offset}"
        if tag:
            query += f"&tags={tag}"
        status, data = api_get(client, query)
        if status in {400, 403}:
            return
        if status != 200 or not data:
            return
        rows = data.get("resources") or []
        if not rows:
            return
        yield from rows
        if len(rows) < limit:
            return
        offset += limit
        time.sleep(0.15)


def latest_version(resource: dict[str, Any]) -> dict[str, Any] | None:
    versions = resource.get("versions") or []
    if not versions:
        return None
    return sorted(versions, key=lambda item: item.get("date") or "", reverse=True)[0]


def discover_resources(db: sqlite3.Connection, client: httpx.Client, skills: SkillIndex) -> int:
    added = 0
    seen: set[int] = set()
    for type_id, doc_key in RESOURCE_TYPE_TO_DOC.items():
        typed = 0
        for row in iter_resources(client, type_id):
            resource_id = row.get("id")
            if not resource_id or resource_id in seen:
                continue
            seen.add(resource_id)
            name = text_of(row.get("name"))
            tags = row.get("tags") or []
            filename = name or f"resource-{resource_id}"
            url = resource_download_url(resource_id)
            info = classify(
                filename=filename,
                tags=tags,
                doc_key=doc_key,
            )
            if info.skill_number:
                info.skill_name = skills.name(info.edition_code, info.skill_number)
            if queue_item(db, url=url, info=info, filename=filename, source="resources"):
                added += 1
                typed += 1
        db.commit()
        print(f"资源类型 {doc_key} 新加入 {typed}")
    return added


def probe_td_langs(client: httpx.Client, document_id: int, skill_id: int) -> list[str]:
    found: list[str] = []
    for lang in TD_LANGS:
        url = f"{API}/skillman/documents/{document_id}/skills/{skill_id}/pdf?l={lang}"
        response = client.get(url, headers={"Range": "bytes=0-2047"})
        if response.status_code == 401:
            raise LoginRequired(url)
        ctype = content_type_of(response)
        filename = (filename_from_disposition(response.headers.get("content-disposition")) or "").lower()
        if response.status_code in {200, 206} and "pdf" in ctype:
            if lang == "en" or f"_{lang}." in filename:
                found.append(lang)
        time.sleep(0.2)
    return found or ["en"]


def skillman_targets(client: httpx.Client, skills: SkillIndex) -> dict[str, int]:
    wanted: dict[str, int] = {}
    status, data = api_get(client, f"{API}/skillman/events")
    if status == 200 and data:
        id_to_code = {event_id: code for code, event_id in skills.event_ids.items()}
        for event in data.get("events") or []:
            event_id = event.get("id")
            name = text_of(event.get("name"))
            code = code_from_event(event.get("code"), name, event_id) or id_to_code.get(event_id)
            if not code:
                continue
            register_edition(code, name or edition_name(code), event_id)
            skills.event_ids[code] = event_id
            skills.event_names[event_id] = name or edition_name(code)
            wanted[code] = event_id
    for code, event_id in list(skills.event_ids.items()):
        if str(code).startswith("WSC"):
            wanted.setdefault(code, event_id)
    return wanted


def discover_skillman(db: sqlite3.Connection, client: httpx.Client, skills: SkillIndex) -> int:
    added = 0
    for code, event_id in skillman_targets(client, skills).items():
        status, data = api_get(client, f"{API}/skillman/documents/events/{event_id}")
        if status in {400, 403}:
            info = classify(edition_hint=code, doc_key="technical-description")
            queue_item(
                db,
                url=f"{API}/skillman/documents/events/{event_id}",
                info=info,
                filename="documents.json",
                source="skillman",
                extra_key=f"forbidden:skillman-docs:{event_id}",
            )
            mark(db, f"forbidden:skillman-docs:{event_id}", state="forbidden", error="没有权限查看该届技能管理文档", http_status=status)
            continue
        if status != 200 or not data:
            continue
        documents = data.get("documents") or []
        if not documents:
            print(f"{code} 技能管理无在线文档")
            continue
        for document in documents:
            doc_id = document["id"]
            doc_name = text_of(document.get("name")).lower()
            doc_key = "technical-description"
            if "management plan" in doc_name or "smp" in doc_name:
                doc_key = "skill-management-plan"
            status, skill_data = api_get(client, f"{API}/skillman/skills?event={event_id}")
            if status != 200 or not skill_data:
                continue
            skill_rows = skill_data.get("skills") or []
            langs = ["en"]
            if skill_rows:
                first_id = skill_id_of(skill_rows[0])
                langs = probe_td_langs(client, doc_id, first_id)
                print(f"{code} 技术描述语言：{', '.join(langs)}")
            for skill in skill_rows:
                skill_id, number, name = skill_fields(skill)
                if number and name:
                    skills.add(code, number, name, event_id)
                for lang in langs:
                    filename = f"{code}_TD{number or skill_id}_{lang}.pdf"
                    url = f"{API}/skillman/documents/{doc_id}/skills/{skill_id}/pdf?l={lang}"
                    info = classify(
                        filename=filename,
                        doc_key=doc_key,
                        lang_code=lang,
                        edition_hint=code,
                        skill_number=number,
                        skill_name=name or skills.name(code, number),
                    )
                    if queue_item(db, url=url, info=info, filename=filename, source="skillman"):
                        added += 1
            db.commit()
    return added


def discover_il(db: sqlite3.Connection, client: httpx.Client, skills: SkillIndex) -> int:
    added = 0
    status, data = api_get(client, f"{API}/il/events")
    if status != 200 or not data:
        return 0
    for event in data.get("events") or []:
        event_id = event["id"]
        name = text_of(event.get("name"))
        code = next((item for item, stored in skills.event_ids.items() if stored == event_id), None)
        if not code:
            code = code_from_event(event.get("code"), name, event_id)
            register_edition(code, name or edition_name(code), event_id)
            skills.event_ids[code] = event_id
            skills.event_names[event_id] = name or edition_name(code)
        list_status, lists = api_get(client, f"{API}/il/events/{event_id}/lists")
        if list_status in {400, 403}:
            info = classify(edition_hint=code, doc_key="infrastructure-list")
            key = f"forbidden:il:{event_id}"
            queue_item(db, url=f"{API}/il/events/{event_id}/lists", info=info, filename="lists.json", source="il", extra_key=key)
            mark(db, key, state="forbidden", error="没有权限查看该届基础设施清单", http_status=list_status)
            continue
        if list_status != 200 or not lists:
            continue
        for item in lists.get("lists") or []:
            title = text_of(item.get("name"))
            number, skill_name = parse_list_title(title)
            if number and skill_name:
                skills.add(code, number, skill_name, event_id)
            filename = f"{code}_IL{number or item['id']}.xlsx"
            url = f"{API}/il/reports/requested/lists/{event_id}/{item['id']}?s=xlsx"
            info = classify(
                filename=filename,
                doc_key="infrastructure-list",
                edition_hint=code,
                skill_number=number,
                skill_name=skill_name or skills.name(code, number),
            )
            if queue_item(db, url=url, info=info, filename=filename, source="il"):
                added += 1
        db.commit()
        print(f"基础设施清单 {code} 已加入队列")
    return added


def cms_resource_pages() -> list[tuple[str, str, str]]:
    pages = []
    for slug, code in CMS_SLUG_TO_CODE.items():
        for section, doc_key in (
            ("test-projects", "test-project"),
            ("technical-descriptions", "technical-description"),
        ):
            url = f"{CMS}/internal/competition-documentation/{slug}/{section}/"
            pages.append((code, doc_key, url))
    return pages


def scrape_cms_download_hrefs(client: httpx.Client) -> list[tuple[str, str, str, str]]:
    found: list[tuple[str, str, str, str]] = []
    for code, doc_key, url in cms_resource_pages():
        try:
            page = client.get(url, headers={"Accept": "text/html"})
        except httpx.HTTPError:
            continue
        if looks_like_login(page) or page.status_code >= 400:
            continue
        soup = BeautifulSoup(page.text, "html.parser")
        for anchor in soup.select('a[href*="resources/download/"]'):
            href = str(anchor.get("href") or "")
            text = anchor.get_text(" ", strip=True)
            if href:
                found.append((code, doc_key, text or Path(urlparse(href).path).name, href))
        time.sleep(0.15)
    return found


def ingest_cms_hrefs(db: sqlite3.Connection, skills: SkillIndex, rows: list[tuple[str, str, str, str]]) -> int:
    added = 0
    for code, doc_key, filename, href in rows:
        url = absolute_download_url(href)
        info = classify(filename=filename, doc_key=doc_key, edition_hint=code)
        if info.skill_number:
            info.skill_name = skills.name(code, info.skill_number)
        info.edition_code = code
        info.edition_name = edition_name(code)
        if queue_item(db, url=url, info=info, filename=filename or "file.bin", source="cms"):
            added += 1
    return added


def requeue_short_download_forbidden(db: sqlite3.Connection) -> int:
    cursor = db.execute(
        """
        UPDATE items
        SET state = 'queued', error = NULL, http_status = NULL
        WHERE state = 'forbidden' AND url LIKE '%/resources/download/%'
        """
    )
    db.commit()
    return int(cursor.rowcount)


def resource_download_needs_token(db: sqlite3.Connection) -> bool:
    row = db.execute(
        """
        SELECT 1 FROM items
        WHERE state IN ('queued', 'forbidden', 'error')
          AND url LIKE '%/resources/download/%'
          AND instr(lower(url), 'tkn=') = 0
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def collect_cms_download_urls(db: sqlite3.Connection, client: httpx.Client, skills: SkillIndex) -> int:
    hrefs = scrape_cms_download_hrefs(client)
    if not hrefs:
        print("竞赛文档页没有拿到带令牌的链接。请先运行 python sync.py login。")
        return 0
    added = ingest_cms_hrefs(db, skills, hrefs)
    db.commit()
    print(f"竞赛文档页收集到 {len(hrefs)} 个完整下载地址，新加入 {added}")
    return len(hrefs)


def discover_cms(db: sqlite3.Connection, client: httpx.Client, skills: SkillIndex) -> int:
    added = ingest_cms_hrefs(db, skills, scrape_cms_download_hrefs(client))
    try:
        response = client.get(INTERNAL_DOCS, headers={"Accept": "text/html"})
    except httpx.HTTPError:
        return 0
    if looks_like_login(response) or response.status_code >= 400:
        return 0
    soup = BeautifulSoup(response.text, "html.parser")
    edition_links = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        match = re.search(r"/internal/competition-documentation/([^/]+)/?$", href)
        if match and match.group(1) in CMS_SLUG_TO_CODE:
            edition_links.append((CMS_SLUG_TO_CODE[match.group(1)], href if href.startswith("http") else CMS + href))
    seen_pages: set[str] = set()
    for code, edition_url in edition_links:
        page = client.get(edition_url, headers={"Accept": "text/html"})
        if page.status_code != 200:
            continue
        edition_soup = BeautifulSoup(page.text, "html.parser")
        section_urls = []
        for anchor in edition_soup.select("a[href]"):
            href = anchor.get("href") or ""
            for section, doc_key in CMS_SECTION_TO_DOC.items():
                if f"/{section}" in href:
                    url = href if href.startswith("http") else urlparse(edition_url)._replace(path=href).geturl() if href.startswith("/") else edition_url.rstrip("/") + "/" + href
                    if href.startswith("/"):
                        url = CMS + href
                    elif not href.startswith("http"):
                        url = edition_url.rstrip("/") + "/" + href
                    section_urls.append((doc_key, url.split("?")[0]))
        for doc_key, section_url in {item for item in section_urls}:
            if section_url in seen_pages:
                continue
            seen_pages.add(section_url)
            section_page = client.get(section_url, headers={"Accept": "text/html"})
            if section_page.status_code != 200:
                if section_page.status_code >= 500:
                    info = classify(edition_hint=code, doc_key=doc_key)
                    key = f"missing:{section_url}"
                    if queue_item(db, url=section_url, info=info, filename="page.html", source="cms", extra_key=key):
                        mark(db, key, state="error", http_status=section_page.status_code, error=f"栏目打开失败 HTTP {section_page.status_code}")
                continue
            html = BeautifulSoup(section_page.text, "html.parser")
            for anchor in html.select("a[href]"):
                href = anchor.get("href") or ""
                if "resources/download/" not in href and not re.search(r"\.(pdf|zip|docx?|xlsx?)$", href, re.I):
                    continue
                url = absolute_download_url(href if href.startswith("http") or href.startswith("/") else CMS + "/" + href)
                filename = (anchor.get_text(" ", strip=True) or Path(urlparse(url).path).name or "file.bin")
                info = classify(
                    filename=filename,
                    doc_key=doc_key,
                    edition_hint=code,
                    skill_name=None,
                )
                if info.skill_number:
                    info.skill_name = skills.name(code, info.skill_number)
                info.edition_code = code
                info.edition_name = edition_name(code)
                if queue_item(db, url=url, info=info, filename=filename, source="cms"):
                    added += 1
            time.sleep(0.2)
        db.commit()
    return added


def discover_public_pages(db: sqlite3.Connection, client: httpx.Client, skills: SkillIndex) -> int:
    added = 0
    for page_url, doc_key in PUBLIC_PAGES:
        try:
            response = client.get(page_url, headers={"Accept": "text/html"})
        except httpx.HTTPError:
            continue
        if looks_like_login(response) or response.status_code >= 400:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        found = 0
        for anchor in soup.select("a[href]"):
            href = anchor.get("href") or ""
            match = re.search(r"/resources/download/(\d+)", href)
            if not match:
                continue
            url = absolute_download_url(href) if url_has_download_token(href) else resource_download_url(match.group(1))
            filename = anchor.get_text(" ", strip=True) or f"resource-{match.group(1)}"
            info = classify(filename=filename, doc_key=doc_key)
            if info.skill_number:
                info.skill_name = skills.name(info.edition_code, info.skill_number)
            if queue_item(db, url=url, info=info, filename=filename, source="public"):
                added += 1
                found += 1
        db.commit()
        print(f"公开页 {page_url} 新加入 {found}")
    return added


def discover_sample(db: sqlite3.Connection, client: httpx.Client) -> None:
    skills = SkillIndex()
    load_events(client, skills)
    load_skill_maps(client, skills)
    status, data = api_get(client, f"{API}/resources?type=7&tags=WSC2026&limit=1")
    rows = (data or {}).get("resources") or [] if status == 200 else []
    if rows:
        row = rows[0]
        status, detail = api_get(client, f"{API}/resources/{row['id']}")
        resource = detail if status == 200 and isinstance(detail, dict) else row
        name = text_of(resource.get("name") or row.get("name"))
        version = latest_version(resource) if isinstance(resource, dict) else None
        translation = ((version or {}).get("translations") or [None])[0]
        filename = name
        url = f"{API}/resources/download/{row['id']}"
        lang = None
        if isinstance(translation, dict):
            lang = (translation.get("lang_code") or "").split("_")[0].lower() or None
            filename = translation.get("filename") or name
            links = translation.get("links") or []
            download = next((link["href"] for link in links if link.get("rel") == "download"), None)
            if download:
                url = download
        info = classify(filename=filename, tags=resource.get("tags") or row.get("tags"), doc_key="test-project", lang_code=lang, edition_hint="WSC2026")
        info.skill_name = skills.name("WSC2026", info.skill_number)
        info.edition_code = "WSC2026"
        info.edition_name = edition_name("WSC2026")
        queue_item(db, url=url, info=info, filename=filename, source="resources")
    event_id = skills.event_ids.get("WSC2026", 611)
    status, data = api_get(client, f"{API}/skillman/documents/events/{event_id}")
    if status == 200 and data:
        documents = data.get("documents") or []
        if documents:
            doc_id = documents[0]["id"]
            _, skill_data = api_get(client, f"{API}/skillman/skills?event={event_id}")
            skill_rows = (skill_data or {}).get("skills") or []
            target = next((item for item in skill_rows if str(skill_fields(item)[1]) == "33"), skill_rows[0] if skill_rows else None)
            if target:
                skill_id, number, name = skill_fields(target)
                filename = f"WSC2026_TD{number or skill_id}_en.pdf"
                url = f"{API}/skillman/documents/{doc_id}/skills/{skill_id}/pdf?l=en"
                info = classify(filename=filename, doc_key="technical-description", lang_code="en", edition_hint="WSC2026", skill_number=number, skill_name=name)
                queue_item(db, url=url, info=info, filename=filename, source="skillman")
    _, lists = api_get(client, f"{API}/il/events/{event_id}/lists")
    if lists:
        item = next((entry for entry in (lists.get("lists") or []) if str(text_of(entry.get("name"))).startswith("33 ")), (lists.get("lists") or [None])[0])
        if item:
            title = text_of(item.get("name"))
            number, skill_name = parse_list_title(title)
            filename = f"WSC2026_IL{number or item['id']}.xlsx"
            url = f"{API}/il/reports/requested/lists/{event_id}/{item['id']}?s=xlsx"
            info = classify(filename=filename, doc_key="infrastructure-list", edition_hint="WSC2026", skill_number=number, skill_name=skill_name)
            queue_item(db, url=url, info=info, filename=filename, source="il")
    db.commit()
    print(f"样例队列 {count_state(db, 'queued')} 个文件")


def discover(db: sqlite3.Connection, client: httpx.Client) -> None:
    skills = SkillIndex()
    print("正在读取赛事和技能对照…")
    load_events(client, skills)
    load_skill_maps(client, skills)
    print(f"已对照 {len(skills.event_ids)} 届赛事的技能表")
    added = 0
    added += discover_resources(db, client, skills)
    added += discover_skillman(db, client, skills)
    added += discover_il(db, client, skills)
    added += discover_cms(db, client, skills)
    added += discover_public_pages(db, client, skills)
    db.commit()
    print(f"新加入队列 {added} 个文件。当前队列 {count_state(db, 'queued')}。")
    refresh_tables(ROOT, client, db.execute("SELECT * FROM items").fetchall())


def is_binary_ok(response: httpx.Response) -> bool:
    ctype = content_type_of(response)
    if response.status_code not in {200, 206}:
        return False
    if "json" in ctype or "text/html" in ctype:
        return False
    return True


def request_download(client: httpx.Client, url: str) -> tuple[httpx.Response | None, Exception | None]:
    last_error = None
    for attempt in range(3):
        try:
            return client.get(url, headers={"Accept": "*/*"}), None
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    return None, last_error


def deny_download(db: sqlite3.Connection, row: sqlite3.Row, url: str, response: httpx.Response) -> bool:
    if looks_like_login(response) or response.status_code == 401:
        db.execute("UPDATE items SET state = 'queued' WHERE key = ?", (row["key"],))
        db.commit()
        raise LoginRequired(url)
    if response.status_code in {200, 206} and "text/html" in content_type_of(response):
        mark(db, row["key"], state="skipped", http_status=response.status_code, error="网页链接，不归档正文")
        print(f"跳过网页  {row['filename']}  {row['edition_name']}")
        return True
    if response.status_code in {400, 403}:
        body = ""
        try:
            body = (response.json() or {}).get("user_msg") or ""
        except Exception:
            body = ""
        if resource_id_of_url(url) and not url_has_download_token(url):
            mark(db, row["key"], state="error", http_status=response.status_code, error=body or "短下载地址被拒绝，需要竞赛文档页上的完整链接")
            print(f"地址不完整  {row['filename']}  {row['edition_name']}")
            return True
        mark(db, row["key"], state="forbidden", http_status=response.status_code, error=body or "没有权限")
        print(f"无权限  {row['filename']}  {row['edition_name']}")
        return True
    if response.status_code == 404:
        mark(db, row["key"], state="missing", http_status=404, error="找不到")
        print(f"找不到  {row['filename']}")
        return True
    if response.status_code >= 400 or not is_binary_ok(response):
        mark(db, row["key"], state="error", http_status=response.status_code, error=f"HTTP {response.status_code} {content_type_of(response)}")
        print(f"失败 {response.status_code}  {row['filename']}")
        return True
    return False


def process_one(db: sqlite3.Connection, client: httpx.Client, row: sqlite3.Row, max_bytes: int) -> None:
    url = live_download_url(client, row["url"])
    if url != row["url"]:
        db.execute("UPDATE items SET url = ? WHERE key = ?", (url, row["key"]))
        db.commit()
    response, last_error = request_download(client, url)
    if response is None:
        mark(db, row["key"], state="error", error=str(last_error)[:500])
        print(f"出错  {row['filename']}  {last_error}")
        return
    if deny_download(db, row, url, response):
        return
    data = response.content
    if len(data) > max_bytes:
        mark(db, row["key"], state="skipped", http_status=response.status_code, bytes=len(data), error=f"大于 {max_bytes} 字节")
        print(f"跳过过大文件 {len(data)}  {row['filename']}")
        return
    filename = row["filename"] or "file.bin"
    disposition = filename_from_disposition(response.headers.get("content-disposition"))
    if disposition and not disposition.startswith("report_requested"):
        filename = disposition
    digest = hashlib.sha256(data).hexdigest()
    record = record_from_row({**{key: row[key] for key in row.keys()}, "filename": filename, "sha256": digest, "local_path": ""})
    target = STORE / record["store_path"]
    if target.exists() and sha256_file(target) == digest:
        pass
    elif target.exists():
        target = target.with_name(f"{target.stem}-{digest[:8]}{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    mark(
        db,
        row["key"],
        state="done",
        filename=filename,
        local_path=target.relative_to(ROOT).as_posix(),
        http_status=response.status_code,
        bytes=len(data),
        sha256=digest,
        error=None,
    )
    print(f"已保存 {len(data)} 字节  {target.relative_to(STORE)}")


def _download_worker(
    args: argparse.Namespace,
    stop: threading.Event,
    login_fail: threading.Event,
    seen: dict[str, int],
    lock: threading.Lock,
) -> None:
    db = connect()
    client = None
    try:
        client = make_client()
        while not stop.is_set() and not login_fail.is_set():
            with lock:
                if args.max and seen["n"] >= args.max:
                    stop.set()
                    return
                seen["n"] += 1
                n = seen["n"]
            row = claim_next(db, args.edition, args.kind, getattr(args, "retry_keys", None))
            if row is None:
                with lock:
                    seen["n"] -= 1
                return
            try:
                process_one(db, client, row, args.max_bytes)
            except LoginRequired:
                db.execute("UPDATE items SET state = 'queued' WHERE key = ?", (row["key"],))
                db.commit()
                login_fail.set()
                return
            except Exception as exc:
                mark(db, row["key"], state="error", error=str(exc)[:500])
                print(f"出错  {row['filename']}  {exc}")
            if n % 20 == 0:
                export_tables(db)
                print(f"进度 已保存 {count_state(db, 'done')}，队列 {count_state(db, 'queued')}")
            if args.delay:
                time.sleep(args.delay)
    finally:
        if client is not None:
            client.close()
        db.close()


def run_download_queue(args: argparse.Namespace, workers: int) -> None:
    stop = threading.Event()
    login_fail = threading.Event()
    seen: dict[str, int] = {"n": 0}
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_download_worker, args, stop, login_fail, seen, lock) for _ in range(workers)
        ]
        try:
            for fut in as_completed(futures):
                fut.result()
        except BaseException:
            stop.set()
            raise
    if args.max and seen["n"] >= args.max:
        print(f"已达到本次上限 {args.max}")
    if login_fail.is_set():
        raise LoginRequired("下载过程中登录失效")


def download(args: argparse.Namespace) -> None:
    db = connect()
    db.execute("UPDATE items SET state = 'queued' WHERE state = 'working'")
    if args.retry_errors:
        db.execute("UPDATE items SET state = 'queued' WHERE state IN ('error', 'missing')")
    requeued = requeue_short_download_forbidden(db)
    if requeued:
        print(f"已把 {requeued} 条因短地址被拒的资源改回队列")
    args.retry_keys = resource_keys_from_forbidden_csv() if args.retry_forbidden else None
    if args.retry_forbidden:
        if not args.retry_keys:
            raise SystemExit("没有此前失败的试题记录（downloads/forbidden.csv）。")
        placeholders = ",".join("?" * len(args.retry_keys))
        db.execute(
            f"UPDATE items SET state = 'queued', error = NULL, http_status = NULL WHERE key IN ({placeholders}) AND state IN ('forbidden', 'error')",
            args.retry_keys,
        )
        print(f"只重试此前短地址失败的 {len(args.retry_keys)} 个试题")
    db.commit()
    client = make_client()
    workers = clamp_workers(args.workers)
    try:
        if args.sample:
            discover_sample(db, client)
            sample_download(db, client, args.max_bytes)
            export_tables(db)
            return
        did_discover = False
        if not args.retry_forbidden and (args.refresh or (count_state(db, "queued") + count_state(db, "done") < 50)):
            discover(db, client)
            did_discover = True
        elif not args.retry_forbidden:
            print("继续未完成的队列。若要重新扫描网站，请加 --refresh")
        if resource_download_needs_token(db):
            skills = SkillIndex()
            try:
                load_events(client, skills)
                load_skill_maps(client, skills)
            except Exception:
                pass
            collect_cms_download_urls(db, client, skills)
        where, params = queue_where(args.edition, args.kind, args.retry_keys)
        queued = db.execute(f"SELECT COUNT(*) AS n FROM items WHERE {where}", params).fetchone()["n"]
        if args.retry_forbidden:
            with_tkn = db.execute(
                f"SELECT COUNT(*) AS n FROM items WHERE {where} AND instr(lower(url), 'tkn=') > 0",
                params,
            ).fetchone()["n"]
            print(f"其中 {with_tkn} 条已有完整下载地址")
            if queued and with_tkn == 0:
                raise SystemExit("竞赛文档页没有收到带令牌的链接。请先运行 python sync.py login 后再试。")
        if args.edition or args.kind:
            print(f"过滤：届次 {args.edition or '全部'}，类型 {args.kind or '全部'}。视频默认排到最后。")
        print(f"队列 {queued}。并发 {workers} 路。保存位置：{STORE}")
        print("查看进度：python sync.py progress --open")
        run_download_queue(args, workers)
    finally:
        db.execute("UPDATE items SET state = 'queued' WHERE state = 'working'")
        db.commit()
        if not args.retry_forbidden:
            try:
                refresh_tables(ROOT, client, db.execute("SELECT * FROM items").fetchall())
            except Exception as exc:
                print(f"名单表更新失败：{exc}")
        export_tables(db, public=not args.retry_forbidden)
        client.close()
        print(
            "完成："
            f"已保存 {count_state(db, 'done')}，"
            f"队列 {count_state(db, 'queued')}，"
            f"无权限 {count_state(db, 'forbidden')}，"
            f"失败 {count_state(db, 'error')}，"
            f"找不到 {count_state(db, 'missing')}"
        )
        print(f"总表：{CATALOG_CSV}")


def sample_download(db: sqlite3.Connection, client: httpx.Client, max_bytes: int) -> None:
    wanted: list[str] = []
    for source, doc in (("resources", "试题"), ("skillman", "技术描述"), ("il", "基础设施清单")):
        row = db.execute(
            "SELECT key FROM items WHERE state IN ('queued', 'done') AND source = ? AND doc_type = ? ORDER BY state DESC, queued_at LIMIT 1",
            (source, doc),
        ).fetchone()
        if row:
            wanted.append(row["key"])
    print(f"样例下载 {len(wanted)} 个文件")
    for key in wanted:
        row = db.execute("SELECT * FROM items WHERE key = ?", (key,)).fetchone()
        if not row:
            continue
        if row["state"] == "done" and row["local_path"] and (ROOT / row["local_path"]).exists():
            print(f"样例已存在  {row['local_path']}")
            continue
        db.execute("UPDATE items SET state = 'working' WHERE key = ?", (key,))
        db.commit()
        process_one(db, client, row, max_bytes)


def login() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("请先安装依赖：.venv/bin/pip install -r requirements.txt") from exc

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    profile = SESSION_DIR / "chrome"
    print("即将打开浏览器。请用你自己的 WorldSkills 会员账号登录，不要使用他人账号。")
    print("看到 Member Area 后，脚本会继续打开技能管理和基础设施清单，并把令牌写到本机 .session/。")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            channel="chrome",
            headless=False,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://worldskills.org/internal/", wait_until="domcontentloaded")
        try:
            page.wait_for_url(re.compile(r"https://worldskills\.org/internal"), timeout=300_000)
            page.wait_for_selector("text=Member Area", timeout=300_000)
        except Exception as exc:
            context.close()
            raise SystemExit("登录超时。请再次运行 python sync.py login。") from exc
        for url in (
            "https://skill-management.worldskills.org/",
            "https://il.worldskills.org/",
        ):
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            token = page.evaluate("() => sessionStorage.getItem('access_token')")
            if token:
                save_token(token)
        context.storage_state(path=str(STORAGE_STATE))
        os.chmod(STORAGE_STATE, 0o600)
        context.close()
    if not load_token():
        raise SystemExit("没有拿到接口令牌。请确认技能管理页面已打开后再试。")
    client = make_client()
    try:
        status, data = api_get(client, f"{API}/auth/users/loggedIn")
    finally:
        client.close()
    if status != 200:
        raise SystemExit("登录会话无效，请重新运行 python sync.py login。")
    print(f"登录有效。当前用户：{text_of((data or {}).get('first_name'))} {text_of((data or {}).get('last_name'))}".strip())
    print("试题等会员资料要用竞赛文档页上的完整下载地址。登录后脚本才能收集这些链接。")
    print("下一步：python sync.py download --sample")


def show_status() -> None:
    if not DB_PATH.exists():
        print("还没有下载记录。")
        print("克隆后可从 GitHub Releases 解压 zip 到 store/，或运行 python sync.py login 后再 download。")
        return
    db = connect()
    states = {row[0]: row[1] for row in db.execute("SELECT state, COUNT(*) FROM items GROUP BY state")}
    total = sum(states.values())
    done = int(states.get("done", 0))
    if total:
        print(f"进度：{100.0 * done / total:.1f}%  {done}/{total}")
    labels = (
        ("done", "已保存"),
        ("queued", "队列中"),
        ("working", "正在下"),
        ("forbidden", "无权限"),
        ("error", "失败"),
        ("missing", "找不到"),
        ("skipped", "已跳过"),
    )
    for state, label in labels:
        n = int(states.get(state, 0))
        if n or state in {"done", "queued"}:
            print(f"{label}：{n}")
    if STORE.exists():
        print(f"正文库：{STORE}")
    if (ROOT / "indexes" / "summary.json").exists():
        print(f"索引：{ROOT / 'indexes' / 'summary.json'}")
    print("浏览器进度：python sync.py progress --open")


def reindex(args: argparse.Namespace) -> None:
    db = connect()
    STORE.mkdir(parents=True, exist_ok=True)
    moved = 0
    missing = 0
    used: dict[str, str] = {}
    rows = db.execute("SELECT * FROM items WHERE state = 'done' ORDER BY edition_code, skill_number, filename").fetchall()
    for row in rows:
        record = record_from_row(row)
        record["store_path"] = unique_relpath(record["store_path"], used, row["key"])
        src = existing_source(ROOT, STORE, row)
        dest = STORE / record["store_path"]
        if src is None:
            missing += 1
            print(f"找不到  {row['filename']}")
            continue
        dest = place_file(src, dest, dry_run=args.dry_run)
        store_rel = dest.relative_to(STORE).as_posix() if dest.is_relative_to(STORE) else record["store_path"]
        src_rel = src.relative_to(ROOT).as_posix() if src.is_relative_to(ROOT) else str(src)
        if args.dry_run:
            print(f"{src_rel} -> store/{store_rel}")
            moved += 1
            continue
        digest = row["sha256"] if "sha256" in row.keys() and row["sha256"] else sha256_file(dest)
        mark(
            db,
            row["key"],
            local_path=(STORE / store_rel).relative_to(ROOT).as_posix(),
            sha256=digest,
            bytes=dest.stat().st_size,
            filename=row["filename"],
        )
        if src.is_relative_to(ARCHIVE):
            prune_empty_dirs(src.parent, ARCHIVE)
        moved += 1
        if moved % 25 == 0:
            print(f"已整理 {moved}")
    if not args.dry_run:
        export_tables(db)
    print(f"整理完成：{moved} 个文件" + (f"，缺 {missing}" if missing else ""))
    if args.dry_run:
        print("这是预览，没有移动文件。去掉 --dry-run 才会真正整理。")


def pack(args: argparse.Namespace) -> None:
    db = connect()
    records = items_records(db)
    written = pack_releases(STORE, DIST, records, edition=args.edition, kind=args.kind)
    if not written:
        print("没有可打包的已保存文件。")
        return
    for item in written:
        print(f"{item['asset']}  {item['files']} 个文件  {item['bytes']} 字节")
    print(f"清单：{DIST / 'manifest.json'}")
    print("上传：python sync.py publish")


def run_progress(args: argparse.Namespace) -> None:
    import webbrowser

    import progress as progress_mod

    path = progress_mod.write_file()
    print(f"进度页：{path}")
    if args.open:
        webbrowser.open(path.as_uri())
    if args.http:
        progress_mod.serve()
        return
    if args.once:
        return
    print("页面会持续更新，用 Ctrl+C 停止。")
    progress_mod.watch()


def run_search(args: argparse.Namespace) -> None:
    import search as search_mod

    search_mod.serve(host=args.host, port=args.port, open_browser=args.open)


def run_extract(args: argparse.Namespace) -> None:
    from extract import extract_store_zip

    dest, files = extract_store_zip(args.path)
    print(f"解开 {len(files)} 个文件：{dest}")
    print("正文库里的 zip 原件没有改动。")
    if args.open:
        subprocess.run(["open", str(dest)], check=False)


def run_unpack(args: argparse.Namespace) -> None:
    from extract import unpack_release

    for raw in args.zips:
        path = Path(raw)
        files = unpack_release(path, STORE)
        print(f"{path.name} → store/  {len(files)} 个文件")


def run_zipindex(_args: argparse.Namespace) -> None:
    from extract import MEMBERS_PATH, build_zip_members

    mapping = build_zip_members(STORE)
    print(f"已扫描 {len(mapping)} 个 zip，索引：{MEMBERS_PATH}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 WorldSkills 会员区可读资料下载到正文库，并用索引按技能、赛事、语言查阅。",
        epilog="用法见 README.md。可从 GitHub Releases 解压 zip 到 store/，或用自己的账号 login 后再 download。",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="用你自己的账号打开浏览器登录并保存会话")
    download_parser = sub.add_parser("download", help="发现并下载文件")
    download_parser.add_argument("--delay", type=float, default=1.0, help="每个线程两次下载之间的间隔秒数，默认 1")
    download_parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"同时下载的文件数，默认 {DEFAULT_WORKERS}，最多 {MAX_WORKERS}",
    )
    download_parser.add_argument("--max", type=int, default=0, help="本次最多下载多少个文件，0 表示不限")
    download_parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    download_parser.add_argument("--sample", action="store_true", help="先各下一份试题、技术描述、基础设施清单")
    download_parser.add_argument("--refresh", action="store_true", help="重新扫描目录，已下载的文件仍会跳过")
    download_parser.add_argument("--retry-errors", action="store_true")
    download_parser.add_argument("--edition", help="只下载某一届，如 WSC2026；不填则继续全队列")
    download_parser.add_argument("--kind", help="只下载某一类，如 TP、TD、IL、VID；不填则该届全部类型")
    download_parser.add_argument(
        "--retry-forbidden",
        action="store_true",
        help="只重试 forbidden.csv 里此前短地址失败的试题，不改公开索引",
    )
    sub.add_parser("discover", help="只扫描目录，不下载")
    sub.add_parser("status", help="在终端查看进度")
    progress_parser = sub.add_parser("progress", help="生成本地进度页")
    progress_parser.add_argument("--open", action="store_true", help="用系统浏览器打开进度页")
    progress_parser.add_argument("--once", action="store_true", help="只写一次 HTML，不循环更新")
    progress_parser.add_argument("--http", action="store_true", help="在 127.0.0.1:8765 提供页面")
    search_parser = sub.add_parser("search", help="打开本机搜索页，按技能、文件名、选手姓名查找")
    search_parser.add_argument("--open", action="store_true", help="用系统浏览器打开")
    search_parser.add_argument("--port", type=int, default=8766)
    search_parser.add_argument("--host", default="127.0.0.1")
    sub.add_parser("data", help="刷新届次、项目、成员、成绩等名单表")
    reindex_parser = sub.add_parser("reindex", help="把已下载文件迁到正文库并生成索引")
    reindex_parser.add_argument("--dry-run", action="store_true", help="只预览新路径，不移动文件")
    extract_parser = sub.add_parser("extract", help="把 store 里的某个 zip 解到 work/，不改原件")
    extract_parser.add_argument("--path", required=True, help="相对 store/ 的路径，如 WSC2015/TP/34/actual/und/foo.zip")
    extract_parser.add_argument("--open", action="store_true", help="解完后打开工作目录")
    unpack_parser = sub.add_parser("unpack", help="把 GitHub Release 的 zip 解到 store/")
    unpack_parser.add_argument("zips", nargs="+", help="Release zip 路径")
    sub.add_parser("zipindex", help="扫描 store 里每个试题 zip 的内部文件名，供搜索使用")
    pack_parser = sub.add_parser("pack", help="按届次和类型打成 Release zip")
    pack_parser.add_argument("--edition", help="如 WSC2005，默认打包已保存的全部届次")
    pack_parser.add_argument("--kind", help="TD/TP/IL 等，默认该届次下全部分类")
    publish_parser = sub.add_parser("publish", help="打包并把 zip 上传到 GitHub Releases")
    publish_parser.add_argument("--edition", help="如 WSC2005，默认上传已打包的全部届次")
    publish_parser.add_argument("--kind", help="TD/TP/IL 等，默认该届次下全部分类")
    publish_parser.add_argument("--skip-data", action="store_true", help="跳过刷新名单表")
    publish_parser.add_argument("--dry-run", action="store_true", help="只打包，不上传")
    return parser


def run_discover() -> None:
    db = connect()
    client = make_client()
    try:
        discover(db, client)
    finally:
        export_tables(db)
        client.close()


def run_data() -> None:
    db = connect()
    client = make_client(require_login=False)
    try:
        refresh_tables(ROOT, client, db.execute("SELECT * FROM items").fetchall())
    finally:
        export_tables(db)
        client.close()


def publish(args: argparse.Namespace) -> None:
    if not args.skip_data:
        run_data()
    pack(args)
    if args.dry_run:
        print("这是预览，没有上传 GitHub。")
        return
    manifest_path = DIST / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit("没有打包清单。请先下载文件再 publish。")
    items = json.loads(manifest_path.read_text(encoding="utf-8"))
    wanted = release_tag(args.edition) if args.edition else None
    grouped: dict[str, list[Path]] = {}
    for item in items:
        tag = item.get("release") or ""
        if wanted and tag != wanted:
            continue
        path = Path(item.get("path") or "")
        if not path.is_file():
            path = DIST / str(item.get("asset") or "")
        if path.is_file():
            grouped.setdefault(tag, []).append(path)
    if not grouped:
        raise SystemExit("没有可上传的 zip。请先 download 再 publish。")
    titles = {release_tag(code): f"{name} ({code})" for code, name in EDITION_NAMES.items()}
    for tag, files in grouped.items():
        title = titles.get(tag, tag)
        notes = (
            f"{title} 竞赛资料包。\n\n"
            "名单和索引在仓库的 data/ 与 indexes/。\n"
            "zip 内路径与 store/ 一致，解压到 store/ 即可。"
        )
        viewed = subprocess.run(["gh", "release", "view", tag], cwd=ROOT, capture_output=True, text=True)
        if viewed.returncode != 0:
            created = subprocess.run(
                ["gh", "release", "create", tag, "--title", title, "--notes", notes],
                cwd=ROOT,
            )
            if created.returncode != 0:
                raise SystemExit(f"无法创建 Release {tag}。请确认已安装 gh 并已登录。")
        for path in files:
            print(f"上传 {path.name} -> {tag}")
            uploaded = subprocess.run(["gh", "release", "upload", tag, str(path), "--clobber"], cwd=ROOT)
            if uploaded.returncode != 0:
                raise SystemExit(f"上传失败 {path.name}")
    print("文件已上传到 GitHub Releases。请把 data/ 和 indexes/ 提交到 git。")


def _self_test() -> None:
    tp = classify(filename="WSC2026_TP24_actual_en.zip", tags=["WSC2026", "Skill 24", "Actual", "Test Project"], doc_key="test-project")
    assert tp.edition_code == "WSC2026"
    assert tp.skill_number == "24"
    assert tp.stage == "正式"
    assert tp.lang_code == "en"
    td = classify(filename="WSC2026_TD33_zh.pdf", doc_key="technical-description", lang_code="zh")
    assert td.skill_number == "33" and td.lang_code == "zh"
    se = classify(filename="WSC2022_TP01_actual.zip", tags=["WSC2022SE", "Skill 01"])
    assert se.edition_code == "WSC2022SE"
    es = classify(filename="ES2025_TD12_en.pdf", tags=["ES2025", "Skill 12"], doc_key="technical-description")
    assert es.edition_code == "ES2025" and es.skill_number == "12"
    untagged = classify(filename="WSI_MS_benefits_of_membership.pdf", tags=["Official Document"], doc_key="official-document")
    assert untagged.edition_code == GLOBAL_CODE
    occupied: dict[str, int] = {}
    first = dedupe_event_code("Taitaja2023Espoo", 596, occupied)
    second = dedupe_event_code("Taitaja2023Espoo", 602, occupied)
    assert first == "Taitaja2023Espoo" and second == "Taitaja2023Espoo-E602"
    number, name = parse_list_title("33 Automobile Technology")
    assert number == "33" and name == "Automobile Technology"
    path = classified_path(tp, "WSC2026_TP24_actual_en.zip")
    posix = path.as_posix()
    assert "WSC2026/TP/24/actual/en/WSC2026_TP24_actual_en.zip" in posix
    assert parse_lang("WSC2026_TP10_38FI_pre_EN_v3.zip") == "en"
    assert parse_lang("TP01_36KR.zip") is None
    record = record_from_row(
        {
            "edition_code": "WSC2005",
            "doc_type": "试题",
            "skill_number": "38",
            "stage": "正式",
            "language": "未标注",
            "filename": "TP38_38FI.zip",
            "state": "done",
            "bytes": 10,
        }
    )
    assert record["store_path"] == "WSC2005/TP/38/actual/und/TP38_38FI.zip"
    assert record["release"] == "wsc-2005-helsinki"
    assert release_tag("ES2025") == "es-2025-herning"
    assert release_tag(GLOBAL_CODE) == "global"
    assert kind_code("视频") == "VID"
    assert kind_code("video") == "VID"
    assert RESOURCE_TYPE_TO_DOC[3] == "video"
    assert RESOURCE_TYPE_TO_DOC[19] == "wsss"
    assert clamp_workers(0) == 1
    assert clamp_workers(4) == 4
    assert clamp_workers(99) == MAX_WORKERS
    short = f"{API}/resources/download/32711"
    tokenized = f"{short}/35220/36347?l=en&tkn=example-token"
    cms_host = "https://worldskills.org/resources/download/32711/35220/36347?l=en&tkn=example-token"
    assert resource_download_key(tokenized) == short
    assert resource_download_key(cms_host) == short
    assert canonical_url(tokenized) == f"{short}/35220/36347?l=en"
    assert url_has_download_token(tokenized) and not url_has_download_token(short)
    assert url_rank(tokenized) > url_rank(short)
    assert absolute_download_url(cms_host) == tokenized
    assert absolute_download_url("/resources/download/32711/35220/36347?l=en&tkn=example-token") == tokenized
    from layout import public_url
    assert "tkn=" not in public_url(tokenized)
    assert resolve_kind_filter("试题") == "TP"
    assert resolve_kind_filter("TD") == "TD"
    where, params = queue_where("WSC2026", "TP")
    assert "edition_code = ?" in where and "WSC2026" in params and "试题" in params
    if FORBIDDEN_CSV.exists():
        keys = resource_keys_from_forbidden_csv()
        assert keys and all("/resources/download/" in key for key in keys)
    from extract import _self_test as extract_self_test
    from search import _self_test as search_self_test
    from progress import counts, eta_text, write_file

    extract_self_test()
    search_self_test()

    assert eta_text(0, 8, None) == "—"
    assert "states" in counts()
    progress_path = write_file()
    assert progress_path.exists() and "下载进度" in progress_path.read_text(encoding="utf-8")
    print("self-test ok")


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if argv[:1] == ["--self-test"]:
        _self_test()
        return
    args = build_parser().parse_args(argv)
    try:
        if args.command == "login":
            login()
        elif args.command == "download":
            download(args)
        elif args.command == "discover":
            run_discover()
        elif args.command == "status":
            show_status()
        elif args.command == "progress":
            run_progress(args)
        elif args.command == "search":
            run_search(args)
        elif args.command == "extract":
            run_extract(args)
        elif args.command == "unpack":
            run_unpack(args)
        elif args.command == "zipindex":
            run_zipindex(args)
        elif args.command == "reindex":
            reindex(args)
        elif args.command == "pack":
            pack(args)
        elif args.command == "data":
            run_data()
        elif args.command == "publish":
            publish(args)
    except LoginRequired:
        raise SystemExit("登录已失效。请运行：python sync.py login") from None


if __name__ == "__main__":
    main()
