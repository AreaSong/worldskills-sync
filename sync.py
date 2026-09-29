#!/usr/bin/env python3
"""用你自己的会员登录，把 2001–2026 竞赛资料下载并按技能、届次、类型、语言归档。"""

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
import time
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
    MEMBER_AREA_CODES,
    RESOURCE_TYPE_TO_DOC,
    Classified,
    classify,
    doc_folder,
    edition_name,
    lang_folder,
    member_area_code,
    pad_skill,
    parse_lang,
    parse_list_title,
)
from layout import (
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
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
TD_LANGS = ("en", "zh", "de", "es", "fr", "ja", "ko", "pt", "fi", "ru", "ar")
MAX_BYTES = 1_800_000_000


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
    db = sqlite3.connect(DB_PATH)
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


def enqueue(db: sqlite3.Connection, item: dict[str, Any]) -> bool:
    cursor = db.execute(
        """
        INSERT INTO items (
            key, url, state, edition_code, edition_name, skill_number, skill_name,
            doc_type, stage, language, filename, source, queued_at
        ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (
            item["key"],
            item["url"],
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


def export_tables(db: sqlite3.Connection) -> None:
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
    if not member_area_code(info.edition_code):
        return False
    key = extra_key or canonical_url(url)
    return enqueue(
        db,
        {
            "key": key,
            "url": url,
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
    while True:
        status, data = api_get(client, f"{API}/events?type=competition&limit=100&offset={offset}")
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
            code = event.get("code") or ""
            if not member_area_code(code):
                continue
            skills.event_ids[code] = event["id"]
            skills.event_names[event["id"]] = text_of(event.get("name")) or edition_name(code)
        if len(items) < 100:
            break
        offset += 100
    extras = EDITION_EVENT_IDS
    for code, event_id in extras.items():
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


def iter_resources(client: httpx.Client, type_id: int, tag: str):
    offset = 0
    limit = 50
    while True:
        status, data = api_get(
            client,
            f"{API}/resources?type={type_id}&tags={tag}&limit={limit}&offset={offset}",
        )
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
    for code in MEMBER_AREA_CODES:
        for type_id, doc_key in RESOURCE_TYPE_TO_DOC.items():
            for row in iter_resources(client, type_id, code):
                name = text_of(row.get("name"))
                tags = row.get("tags") or []
                filename = name or f"resource-{row['id']}"
                url = f"{API}/resources/download/{row['id']}"
                info = classify(
                    filename=filename,
                    tags=tags,
                    doc_key=doc_key,
                    edition_hint=code,
                )
                if info.skill_number:
                    info.skill_name = skills.name(code, info.skill_number)
                info.edition_code = code
                info.edition_name = edition_name(code)
                info.doc_key = doc_key
                if queue_item(db, url=url, info=info, filename=filename, source="resources"):
                    added += 1
        db.commit()
        print(f"资源目录 {code} 已加入队列")
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


def discover_skillman(db: sqlite3.Connection, client: httpx.Client, skills: SkillIndex) -> int:
    added = 0
    for code, event_id in skills.event_ids.items():
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
        code = next((item for item, stored in skills.event_ids.items() if stored == event_id), None)
        if not code:
            name = text_of(event.get("name"))
            code = next((item for item, title in EDITION_NAMES.items() if title == name), None)
        if not code or not member_area_code(code):
            continue
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


def discover_cms(db: sqlite3.Connection, client: httpx.Client, skills: SkillIndex) -> int:
    added = 0
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
                url = href if href.startswith("http") else CMS + href
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
                if queue_item(db, url=canonical_url(url), info=info, filename=filename, source="cms"):
                    added += 1
            time.sleep(0.2)
        db.commit()
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
                url = canonical_url(download)
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
    db.commit()
    print(f"新加入队列 {added} 个文件。当前队列 {count_state(db, 'queued')}。")
    refresh_tables(ROOT, client, db.execute("SELECT * FROM items").fetchall())


def is_binary_ok(response: httpx.Response) -> bool:
    ctype = content_type_of(response)
    if response.status_code != 200:
        return False
    if "json" in ctype or "text/html" in ctype:
        return False
    return True


def process_one(db: sqlite3.Connection, client: httpx.Client, row: sqlite3.Row, max_bytes: int) -> None:
    url = row["url"]
    response = None
    last_error = None
    for attempt in range(3):
        try:
            response = client.get(url)
            break
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    if response is None:
        mark(db, row["key"], state="error", error=str(last_error)[:500])
        print(f"出错  {row['filename']}  {last_error}")
        return
    if looks_like_login(response) or response.status_code == 401:
        db.execute("UPDATE items SET state = 'queued' WHERE key = ?", (row["key"],))
        db.commit()
        raise LoginRequired(url)
    if response.status_code in {400, 403}:
        body = ""
        try:
            body = (response.json() or {}).get("user_msg") or ""
        except Exception:
            body = ""
        mark(db, row["key"], state="forbidden", http_status=response.status_code, error=body or "没有权限")
        print(f"无权限  {row['filename']}  {row['edition_name']}")
        return
    if response.status_code == 404:
        mark(db, row["key"], state="missing", http_status=404, error="找不到")
        print(f"找不到  {row['filename']}")
        return
    if response.status_code >= 400 or not is_binary_ok(response):
        mark(
            db,
            row["key"],
            state="error",
            http_status=response.status_code,
            error=f"HTTP {response.status_code} {content_type_of(response)}",
        )
        print(f"失败 {response.status_code}  {row['filename']}")
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


def download(args: argparse.Namespace) -> None:
    db = connect()
    db.execute("UPDATE items SET state = 'queued' WHERE state = 'working'")
    if args.retry_errors:
        db.execute("UPDATE items SET state = 'queued' WHERE state IN ('error', 'missing')")
    db.commit()
    client = make_client()
    try:
        if args.sample:
            discover_sample(db, client)
            sample_download(db, client, args.max_bytes)
            export_tables(db)
            return
        if args.refresh or (count_state(db, "queued") + count_state(db, "done") < 50):
            discover(db, client)
        else:
            print("继续未完成的队列。若要重新扫描网站，请加 --refresh")
        queued = count_state(db, "queued")
        print(f"队列 {queued}。保存位置：{STORE}")
        seen = 0
        while True:
            if args.max and seen >= args.max:
                print(f"已达到本次上限 {args.max}")
                break
            row = db.execute("SELECT * FROM items WHERE state = 'queued' ORDER BY queued_at, key LIMIT 1").fetchone()
            if row is None:
                break
            db.execute("UPDATE items SET state = 'working' WHERE key = ?", (row["key"],))
            db.commit()
            try:
                process_one(db, client, row, args.max_bytes)
            except LoginRequired:
                db.execute("UPDATE items SET state = 'queued' WHERE state = 'working'")
                db.commit()
                raise
            except Exception as exc:
                mark(db, row["key"], state="error", error=str(exc)[:500])
                print(f"出错  {row['filename']}  {exc}")
            seen += 1
            if seen % 20 == 0:
                export_tables(db)
                print(f"进度 已保存 {count_state(db, 'done')}，队列 {count_state(db, 'queued')}")
            time.sleep(args.delay)
    finally:
        db.execute("UPDATE items SET state = 'queued' WHERE state = 'working'")
        db.commit()
        try:
            refresh_tables(ROOT, client, db.execute("SELECT * FROM items").fetchall())
        except Exception as exc:
            print(f"名单表更新失败：{exc}")
        export_tables(db)
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
    print("即将打开浏览器。请用你自己的账号登录会员区。")
    print("看到 Member Area 后，脚本会继续打开技能管理和基础设施清单并保存会话。")
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
    print("下一步：python sync.py download --sample")


def show_status() -> None:
    if not DB_PATH.exists():
        print("还没有下载记录。")
        return
    db = connect()
    labels = (
        ("done", "已保存"),
        ("queued", "队列中"),
        ("forbidden", "无权限"),
        ("error", "失败"),
        ("missing", "找不到"),
        ("skipped", "已跳过"),
    )
    for state, label in labels:
        print(f"{label}：{count_state(db, state)}")
    if STORE.exists():
        print(f"正文库：{STORE}")
    if (ROOT / "indexes" / "summary.json").exists():
        print(f"索引：{ROOT / 'indexes' / 'summary.json'}")


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 WorldSkills 会员区竞赛资料下载到正文库，并用索引按技能、届次、语言查阅")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="打开浏览器登录并保存会话")
    download_parser = sub.add_parser("download", help="发现并下载文件")
    download_parser.add_argument("--delay", type=float, default=1.0, help="每次下载间隔秒数，默认 1")
    download_parser.add_argument("--max", type=int, default=0, help="本次最多下载多少个文件，0 表示不限")
    download_parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    download_parser.add_argument("--sample", action="store_true", help="先各下一份试题、技术描述、基础设施清单")
    download_parser.add_argument("--refresh", action="store_true", help="重新扫描目录，已下载的文件仍会跳过")
    download_parser.add_argument("--retry-errors", action="store_true")
    sub.add_parser("discover", help="只扫描目录，不下载")
    sub.add_parser("status", help="查看进度")
    sub.add_parser("data", help="刷新届次、项目、成员、成绩等名单表")
    reindex_parser = sub.add_parser("reindex", help="把已下载文件迁到正文库并生成索引")
    reindex_parser.add_argument("--dry-run", action="store_true", help="只预览新路径，不移动文件")
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
    assert kind_code("视频") == "VID"
    assert kind_code("video") == "VID"
    assert RESOURCE_TYPE_TO_DOC[3] == "video"
    assert RESOURCE_TYPE_TO_DOC[19] == "wsss"
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
