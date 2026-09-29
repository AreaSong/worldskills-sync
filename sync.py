#!/usr/bin/env python3
"""把你自己账号能打开的 WorldSkills 会员区页面和文件保存到本地。

密码不会写入磁盘。先在弹出的浏览器里登录，脚本只复用这次会话。
文件按网站原来的网址路径存放，另外生成目录，之后用关键词搜索即可。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse, unquote

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SESSION_PATH = ROOT / ".session" / "storage_state.json"
DOWNLOADS = ROOT / "downloads"
MIRROR = DOWNLOADS / "mirror"
TEXT_DIR = DOWNLOADS / "text"
DB_PATH = DOWNLOADS / "catalog.sqlite"
CATALOG_CSV = DOWNLOADS / "catalog.csv"

ORIGIN = "https://worldskills.org"
PAGE_HOSTS = {"worldskills.org"}
FILE_HOSTS = {"worldskills.org", "api.worldskills.org"}
DEFAULT_START = "https://worldskills.org/internal/"
DEFAULT_PREFIXES = ["/internal"]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

SKIP_PARTS = (
    "/logout",
    "/login",
    "/ccm/system/authentication",
    "/ccm/system/captcha",
    "/password/forgot",
    "/registration/guests",
)
ASSET_PREFIXES = (
    "/application/themes/",
    "/node_modules/",
    "/css/",
    "/js/",
    "/fonts/",
    "/img/",
)
DROP_QUERY_KEYS = {"ccm_token", "ctask", "_token", "fbclid"}
FILE_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".docm",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".ppt",
    ".pptx",
    ".pps",
    ".ppsx",
    ".zip",
    ".rar",
    ".7z",
    ".csv",
    ".tsv",
    ".txt",
    ".rtf",
    ".odt",
    ".ods",
    ".odp",
    ".epub",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".mp4",
    ".mov",
    ".mp3",
    ".wav",
}
EXT_BY_TYPE = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/zip": ".zip",
    "application/vnd.rar": ".rar",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "application/rtf": ".rtf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_LIMIT = 1_500_000


class LoginRequired(RuntimeError):
    pass


def normalize_url(raw: str, base: str | None = None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith(("mailto:", "javascript:", "tel:", "data:")):
        return None
    absolute = urljoin(base or ORIGIN, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in FILE_HOSTS:
        return None
    path = unquote(parsed.path or "/")
    if not path.startswith("/"):
        path = "/" + path
    suffix = Path(path).suffix.lower()
    if suffix not in FILE_EXTENSIONS and path != "/":
        path = path.rstrip("/") or "/"
    pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in DROP_QUERY_KEYS or key.startswith("utm_"):
            continue
        pairs.append((key, value))
    pairs.sort()
    query = urlencode(pairs, doseq=True)
    return urlunparse(("https", host, path, "", query, ""))


def path_in_prefixes(path: str, prefixes: list[str]) -> bool:
    path = path or "/"
    for prefix in prefixes:
        item = prefix if prefix.startswith("/") else "/" + prefix
        item = item.rstrip("/") or "/"
        if path.rstrip("/") == item or path.startswith(item + "/"):
            return True
    return False


def classify_url(url: str, prefixes: list[str]) -> str | None:
    parsed = urlparse(url)
    path = parsed.path or "/"
    lower = path.lower()
    host = parsed.netloc.lower()
    if host == "api.worldskills.org":
        if "/resources/download/" in lower or Path(lower).suffix in FILE_EXTENSIONS:
            return "file"
        return None
    if host not in PAGE_HOSTS:
        return None
    if any(part in lower for part in SKIP_PARTS):
        return None
    if any(lower.startswith(prefix) for prefix in ASSET_PREFIXES):
        return None
    if "/application/files/" in lower or "/download_file/" in lower:
        return "file"
    suffix = Path(lower).suffix
    if suffix in FILE_EXTENSIONS and suffix not in {".html", ".htm"}:
        return "file"
    if path_in_prefixes(path, prefixes):
        return "page"
    return None


def safe_component(name: str) -> str:
    name = unquote(name).replace("\x00", "")
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"|?*]', "_", name).strip(" .")
    if name in {"", ".", ".."}:
        name = "_"
    encoded = name.encode("utf-8", errors="ignore")
    if len(encoded) > 160:
        digest = hashlib.sha1(encoded).hexdigest()[:8]
        trimmed = encoded[:120].decode("utf-8", errors="ignore").rstrip()
        name = f"{trimmed}-{digest}"
    return name


def local_path_for(url: str, content_type: str, disposition_name: str | None, is_html: bool) -> Path:
    parsed = urlparse(url)
    parts = [safe_component(part) for part in parsed.path.split("/") if part not in {"", ".", ".."}]
    if not parts:
        parts = ["index.html"]
    if parsed.netloc and parsed.netloc != "worldskills.org":
        parts.insert(0, safe_component(parsed.netloc))
    suffix = Path(parts[-1]).suffix.lower()
    if is_html and suffix not in {".html", ".htm"}:
        parts[-1] = parts[-1] + ".html"
        suffix = ".html"
    if not is_html and disposition_name:
        wanted = safe_component(disposition_name)
        if Path(wanted).suffix:
            parts[-1] = wanted
            suffix = Path(wanted).suffix.lower()
    if not suffix:
        extra = EXT_BY_TYPE.get(content_type, "")
        if extra:
            parts[-1] = parts[-1] + extra
    if parsed.query:
        digest = hashlib.sha1(parsed.query.encode()).hexdigest()[:10]
        stem = Path(parts[-1]).stem
        ext = Path(parts[-1]).suffix
        parts[-1] = f"{stem}-{digest}{ext}"
    return MIRROR.joinpath(*parts)


def filename_from_disposition(header: str | None) -> str | None:
    if not header:
        return None
    message = EmailMessage()
    message["content-disposition"] = header
    name = message.get_filename()
    return name or None


def content_type_of(response: httpx.Response) -> str:
    raw = response.headers.get("content-type", "")
    return raw.split(";", 1)[0].strip().lower()


def is_html_type(content_type: str) -> bool:
    return content_type in HTML_TYPES or content_type.startswith("text/html")


def looks_like_login(response: httpx.Response) -> bool:
    checked = [response.url, *[item.url for item in response.history]]
    for item in checked:
        parsed = urlparse(str(item))
        host = parsed.netloc.lower()
        if host.endswith("auth.worldskills.org"):
            return True
        if parsed.path.rstrip("/") == "/login":
            return True
    return False


def extract_links(html: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_tag = soup.find("base", href=True)
    base = urljoin(page_url, base_tag["href"]) if base_tag else page_url
    found: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all(True):
        for attr in ("href", "src", "data-href", "data-url", "data-file-url", "data-src"):
            value = tag.get(attr)
            if not isinstance(value, str):
                continue
            normalized = normalize_url(value, base)
            if normalized and normalized not in seen:
                seen.add(normalized)
                found.append(normalized)
    for match in re.findall(r"""(?:https://worldskills\.org)?(/application/files/[^"'\\\s<>]+)""", html):
        normalized = normalize_url(match, page_url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    for match in re.findall(r"https://api\.worldskills\.org/resources/download/[0-9/]+[^\"'\s<>]*", html):
        normalized = normalize_url(match, page_url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


def html_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return re.sub(r"\s+", " ", soup.title.string).strip()
    heading = soup.find(["h1", "h2"])
    if heading:
        return re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()
    return ""


def html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:TEXT_LIMIT]


def ooxml_text(path: Path) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.endswith(".xml")
            and (
                name.startswith("word/")
                or name.startswith("ppt/slides/")
                or name == "xl/sharedStrings.xml"
                or name.startswith("xl/worksheets/")
            )
        ]
        for name in names:
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError:
                continue
            for node in root.iter():
                if node.text and node.text.strip():
                    chunks.append(node.text.strip())
            if sum(len(item) for item in chunks) > TEXT_LIMIT:
                break
    return "\n".join(chunks)[:TEXT_LIMIT]


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    chunks: list[str] = []
    total = 0
    for page in reader.pages:
        piece = page.extract_text() or ""
        if not piece:
            continue
        chunks.append(piece)
        total += len(piece)
        if total >= TEXT_LIMIT:
            break
    return "\n".join(chunks)[:TEXT_LIMIT]


def write_text_sidecar(file_path: Path, content_type: str, html: str | None) -> Path | None:
    try:
        if html is not None:
            body = html_text(html)
        elif content_type == "application/pdf" or file_path.suffix.lower() == ".pdf":
            body = pdf_text(file_path)
        elif file_path.suffix.lower() in {".docx", ".xlsx", ".pptx"}:
            body = ooxml_text(file_path)
        else:
            return None
    except Exception:
        return None
    if not body.strip():
        return None
    relative = file_path.relative_to(MIRROR)
    target = TEXT_DIR / relative
    target = target.with_suffix(target.suffix + ".txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def connect() -> sqlite3.Connection:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            url TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            kind TEXT,
            discovered_from TEXT,
            http_status INTEGER,
            content_type TEXT,
            local_path TEXT,
            text_path TEXT,
            title TEXT,
            bytes INTEGER,
            etag TEXT,
            last_modified TEXT,
            error TEXT,
            queued_at REAL,
            finished_at REAL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_items_state ON items(state)")
    return db


def enqueue(db: sqlite3.Connection, url: str, kind: str, discovered_from: str | None) -> bool:
    now = time.time()
    cursor = db.execute(
        """
        INSERT INTO items (url, state, kind, discovered_from, queued_at)
        VALUES (?, 'queued', ?, ?, ?)
        ON CONFLICT(url) DO NOTHING
        """,
        (url, kind, discovered_from, now),
    )
    return cursor.rowcount > 0


def load_client() -> httpx.Client:
    if not SESSION_PATH.exists():
        raise SystemExit("还没有登录会话。请先运行：python sync.py login")
    state = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    cookies = httpx.Cookies()
    for item in state.get("cookies", []):
        domain = item.get("domain") or "worldskills.org"
        cookies.set(item["name"], item["value"], domain=domain, path=item.get("path") or "/")
    return httpx.Client(
        cookies=cookies,
        follow_redirects=True,
        timeout=httpx.Timeout(60.0, connect=20.0),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )


def fetch(client: httpx.Client, url: str, etag: str | None, last_modified: str | None) -> httpx.Response:
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.get(url, headers=headers)
            if response.status_code >= 500:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                time.sleep(delay)
                delay *= 2
                continue
            return response
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(str(last_error) if last_error else "请求失败")


def remember_links(db: sqlite3.Connection, links: list[str], page_url: str, prefixes: list[str]) -> int:
    added = 0
    for link in links:
        kind = classify_url(link, prefixes)
        if kind and enqueue(db, link, kind, page_url):
            added += 1
    db.commit()
    return added


def export_catalog(db: sqlite3.Connection) -> None:
    rows = db.execute(
        """
        SELECT url, title, kind, bytes, local_path, content_type, state
        FROM items
        WHERE state != 'queued'
        ORDER BY url
        """
    ).fetchall()
    with CATALOG_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["网址", "标题", "类型", "大小字节", "本地路径", "内容类型", "状态"])
        for row in rows:
            writer.writerow(
                [
                    row["url"],
                    row["title"] or "",
                    row["kind"] or "",
                    row["bytes"] or 0,
                    row["local_path"] or "",
                    row["content_type"] or "",
                    row["state"],
                ]
            )


def count_state(db: sqlite3.Connection, state: str) -> int:
    row = db.execute("SELECT COUNT(*) AS n FROM items WHERE state = ?", (state,)).fetchone()
    return int(row["n"])


def mark(db: sqlite3.Connection, url: str, **fields: object) -> None:
    fields["finished_at"] = time.time()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    db.execute(f"UPDATE items SET {assignments} WHERE url = ?", (*fields.values(), url))
    db.commit()


def process_one(
    db: sqlite3.Connection,
    client: httpx.Client,
    row: sqlite3.Row,
    prefixes: list[str],
    max_bytes: int,
) -> None:
    url = row["url"]
    response = fetch(client, url, row["etag"], row["last_modified"])
    if looks_like_login(response):
        db.execute("UPDATE items SET state = 'queued' WHERE url = ?", (url,))
        db.commit()
        raise LoginRequired(url)
    if response.status_code == 304 and row["local_path"]:
        saved = ROOT / row["local_path"]
        added = 0
        if row["kind"] == "page" and saved.exists():
            added = remember_links(db, extract_links(saved.read_text(encoding="utf-8", errors="replace"), url), url, prefixes)
        mark(db, url, state="done", http_status=304)
        print(f"未变化  新链接 {added}  {url}")
        return
    if response.status_code == 404:
        mark(db, url, state="missing", http_status=404, error="找不到")
        print(f"找不到  {url}")
        return
    if response.status_code >= 400:
        mark(db, url, state="error", http_status=response.status_code, error=f"HTTP {response.status_code}")
        print(f"失败 {response.status_code}  {url}")
        return

    content_type = content_type_of(response)
    html_page = is_html_type(content_type)
    body = response.content
    if not html_page and len(body) > max_bytes:
        mark(
            db,
            url,
            state="skipped",
            http_status=response.status_code,
            content_type=content_type,
            bytes=len(body),
            error=f"大于 {max_bytes} 字节，已跳过",
        )
        print(f"跳过过大文件 {len(body)} 字节  {url}")
        return

    disposition_name = filename_from_disposition(response.headers.get("content-disposition"))
    target = local_path_for(url, content_type, disposition_name, html_page)
    target.parent.mkdir(parents=True, exist_ok=True)
    if html_page:
        text = response.text
        target.write_text(text, encoding="utf-8")
        title = html_title(text)
        text_path = write_text_sidecar(target, content_type, text)
        added = remember_links(db, extract_links(text, str(response.url)), url, prefixes)
    else:
        target.write_bytes(body)
        title = Path(disposition_name).stem if disposition_name else target.stem
        text_path = write_text_sidecar(target, content_type, None)
        added = 0

    relative = target.relative_to(ROOT).as_posix()
    text_relative = text_path.relative_to(ROOT).as_posix() if text_path else None
    mark(
        db,
        url,
        state="done",
        kind="page" if html_page else "file",
        http_status=response.status_code,
        content_type=content_type,
        local_path=relative,
        text_path=text_relative,
        title=title,
        bytes=len(body),
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
        error=None,
    )
    label = "页面" if html_page else "文件"
    print(f"{label} {response.status_code}  {len(body)} 字节  新链接 {added}  {url}")


def seed(db: sqlite3.Connection, starts: list[str], prefixes: list[str]) -> None:
    for start in starts:
        normalized = normalize_url(start)
        if not normalized:
            raise SystemExit(f"无法识别起始网址：{start}")
        kind = classify_url(normalized, prefixes) or "page"
        enqueue(db, normalized, kind, None)
    db.commit()


def download(args: argparse.Namespace) -> None:
    prefixes = args.prefix or DEFAULT_PREFIXES
    starts = args.start or [DEFAULT_START]
    db = connect()
    if args.retry_errors:
        db.execute("UPDATE items SET state = 'queued' WHERE state = 'error'")
    if args.refresh_pages:
        db.execute("UPDATE items SET state = 'queued' WHERE state = 'done' AND kind = 'page'")
        db.commit()
    seed(db, starts, prefixes)
    queued = count_state(db, "queued")
    print(f"起始页面：{', '.join(starts)}")
    print(f"只沿着这些路径继续翻页：{', '.join(prefixes)}")
    print(f"保存位置：{MIRROR}")
    print(f"当前队列：{queued}。已保存过的地址会跳过，中断后可再次运行接着下。")
    client = load_client()
    seen_this_run = 0
    try:
        while True:
            if args.max and seen_this_run >= args.max:
                print(f"已达到本次上限 {args.max}，停止。再次运行会从队列继续。")
                break
            row = db.execute(
                "SELECT * FROM items WHERE state = 'queued' ORDER BY queued_at, url LIMIT 1"
            ).fetchone()
            if row is None:
                break
            db.execute("UPDATE items SET state = 'working' WHERE url = ?", (row["url"],))
            db.commit()
            try:
                process_one(db, client, row, prefixes, args.max_bytes)
            except LoginRequired:
                db.execute("UPDATE items SET state = 'queued' WHERE state = 'working'")
                db.commit()
                raise
            except Exception as exc:
                mark(db, row["url"], state="error", error=str(exc)[:500])
                print(f"出错  {row['url']}  {exc}")
            else:
                seen_this_run += 1
            time.sleep(args.delay)
    finally:
        db.execute("UPDATE items SET state = 'queued' WHERE state = 'working'")
        db.commit()
        export_catalog(db)
        client.close()
        print(
            "完成统计："
            f"页面/文件 {count_state(db, 'done')}，"
            f"队列剩余 {count_state(db, 'queued')}，"
            f"失败 {count_state(db, 'error')}，"
            f"找不到 {count_state(db, 'missing')}，"
            f"跳过 {count_state(db, 'skipped')}"
        )
        print(f"目录：{CATALOG_CSV}")
        print("搜索示例：python sync.py search 基础设施")


def login() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "缺少浏览器组件。请先执行：\n"
            "  python3 -m pip install -r requirements.txt\n"
            "  python3 -m playwright install chromium"
        ) from exc

    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    print("即将打开浏览器。请用你自己的 WorldSkills 账号登录。")
    print("进入会员页面后，回到这个终端按回车。脚本只保存登录会话，不保存密码。")
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=False, channel="chrome")
        except Exception:
            browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(DEFAULT_START, wait_until="domcontentloaded")
        input("登录完成后按回车继续… ")
        context.storage_state(path=str(SESSION_PATH))
        browser.close()
    os.chmod(SESSION_PATH, 0o600)
    client = load_client()
    try:
        response = client.get(DEFAULT_START)
    finally:
        client.close()
    if looks_like_login(response) or response.status_code >= 400:
        SESSION_PATH.unlink(missing_ok=True)
        raise SystemExit("这次登录没有进入会员区。请再运行一次 python sync.py login。")
    title = html_title(response.text)
    print(f"登录有效。当前页面：{title or response.url}")
    print("下一步：python sync.py download")


def search(keyword: str, limit: int) -> None:
    if not DB_PATH.exists():
        raise SystemExit("还没有目录。请先运行 python sync.py download。")
    db = connect()
    rows = db.execute(
        """
        SELECT url, title, local_path, text_path
        FROM items
        WHERE state = 'done'
        ORDER BY url
        """
    ).fetchall()
    needle = keyword.casefold()
    hits = 0
    for row in rows:
        title = row["title"] or ""
        snippet = ""
        matched = needle in title.casefold()
        text_path = row["text_path"]
        if text_path:
            path = ROOT / text_path
            if path.exists():
                body = path.read_text(encoding="utf-8", errors="replace")
                lowered = body.casefold()
                index = lowered.find(needle)
                if index >= 0:
                    matched = True
                    start = max(0, index - 40)
                    stop = min(len(body), index + len(keyword) + 60)
                    snippet = re.sub(r"\s+", " ", body[start:stop]).strip()
        if not matched:
            continue
        hits += 1
        print(row["local_path"] or row["url"])
        if title:
            print(f"  标题：{title}")
        if snippet:
            print(f"  ……{snippet}……")
        print(f"  {row['url']}")
        if hits >= limit:
            break
    if hits == 0:
        print(f"没有找到「{keyword}」。")
    else:
        print(f"显示 {hits} 条。")


def show_status() -> None:
    if not DB_PATH.exists():
        print("还没有下载记录。")
        return
    db = connect()
    for state, label in (
        ("done", "已保存"),
        ("queued", "队列中"),
        ("error", "失败"),
        ("missing", "找不到"),
        ("skipped", "已跳过"),
    ):
        print(f"{label}：{count_state(db, state)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用你自己的会员登录，把 WorldSkills 会员区里能打开的页面和文件保存到本地，并按关键词搜索。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="打开浏览器，用你的账号登录一次")

    download_parser = sub.add_parser("download", help="下载会员区页面和其中的文件")
    download_parser.add_argument("--start", action="append", help="起始网址，可重复。默认是会员区首页")
    download_parser.add_argument(
        "--prefix",
        action="append",
        help="继续翻页的路径前缀，可重复。默认只跟 /internal，避免把整个公开网站都抓下来",
    )
    download_parser.add_argument("--delay", type=float, default=1.0, help="每次请求间隔秒数，默认 1")
    download_parser.add_argument("--max", type=int, default=0, help="本次最多处理多少个地址，0 表示不限")
    download_parser.add_argument(
        "--max-bytes",
        type=int,
        default=512 * 1024 * 1024,
        help="单个非网页文件的大小上限，默认 512MB",
    )
    download_parser.add_argument("--retry-errors", action="store_true", help="把上次失败的地址重新放回队列")
    download_parser.add_argument(
        "--refresh-pages",
        action="store_true",
        help="重新检查已保存的页面，用来发现后来新增的链接。文件本身仍会跳过",
    )

    search_parser = sub.add_parser("search", help="在已保存的标题和正文里搜索，不用事先分类")
    search_parser.add_argument("keyword")
    search_parser.add_argument("--limit", type=int, default=30)

    sub.add_parser("status", help="查看保存进度")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "login":
        login()
    elif args.command == "download":
        try:
            download(args)
        except LoginRequired:
            raise SystemExit("登录会话已失效。请重新运行：python sync.py login") from None
    elif args.command == "search":
        search(args.keyword, args.limit)
    elif args.command == "status":
        show_status()


def _self_test() -> None:
    internal = normalize_url("https://www.worldskills.org/internal/?utm_source=x&ccm_token=abc")
    assert internal == "https://worldskills.org/internal"
    skill = normalize_url("/skills/id/244/", "https://worldskills.org/internal/")
    assert skill == "https://worldskills.org/skills/id/244"
    assert classify_url("https://worldskills.org/internal/docs", ["/internal"]) == "page"
    assert classify_url("https://worldskills.org/skills/id/244", ["/internal"]) is None
    file_url = "https://worldskills.org/application/files/3616/1234/WS_TD.pdf"
    assert classify_url(file_url, ["/internal"]) == "file"
    assert classify_url("https://worldskills.org/logout", ["/internal"]) is None
    paging = normalize_url("https://worldskills.org/internal/library?ccm_paging_p=2&ccm_token=zz")
    assert paging == "https://worldskills.org/internal/library?ccm_paging_p=2"
    api = normalize_url("https://api.worldskills.org/resources/download/1/2/3?l=en")
    assert api == "https://api.worldskills.org/resources/download/1/2/3?l=en"
    assert classify_url(api, ["/internal"]) == "file"
    assert classify_url("https://api.worldskills.org/org", ["/internal"]) is None
    html = '<html><head><title>Automobile</title><base href="/internal/"></head><body><a href="library/">库</a><a href="/application/files/1/a.pdf">pdf</a></body></html>'
    links = extract_links(html, "https://worldskills.org/internal/home")
    assert "https://worldskills.org/internal/library" in links
    assert "https://worldskills.org/application/files/1/a.pdf" in links
    target = local_path_for(
        "https://worldskills.org/internal/library",
        "text/html",
        None,
        True,
    )
    assert target.name == "library.html"
    print("self-test ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        _self_test()
    else:
        main()
