#!/usr/bin/env python3
"""本机搜索资料和成绩。运行：python search.py --open"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import sqlite3
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from classify import DOC_TYPES
from extract import WORK, extract_store_zip, listing_html, safe_under
from layout import KIND_CODES, record_from_row

ROOT = Path(__file__).resolve().parent
DB = ROOT / "downloads" / "catalog.sqlite"
CATALOG = ROOT / "indexes" / "catalog.csv"
RESULTS = ROOT / "data" / "results.csv"
STORE = ROOT / "store"
PAGE = ROOT / "index.html"
HOST = "127.0.0.1"
PORT = 8766
LIMIT = 80

KIND_LABEL = {KIND_CODES[key]: label for key, label in DOC_TYPES.items() if key in KIND_CODES}
_cache: dict = {"built": 0.0, "files": [], "results": []}


def tokenize(query: str) -> list[str]:
    return [part.lower() for part in query.split() if part.strip()]


def _join(*parts: object) -> str:
    return " ".join(str(part) for part in parts if part).lower()


def attach_file(item: dict) -> dict:
    skill = item.get("skill") or ""
    item["doc_label"] = item.get("doc_label") or KIND_LABEL.get(item.get("kind") or "", item.get("kind") or "")
    item["_type"] = "file"
    item["_hay"] = _join(
        item.get("edition"),
        item.get("edition_name"),
        skill,
        skill.lstrip("0"),
        item.get("skill_name"),
        item.get("kind"),
        item.get("doc_label"),
        item.get("stage"),
        item.get("lang"),
        item.get("filename"),
        item.get("store_path"),
    )
    return item


def attach_result(item: dict) -> dict:
    skill = item.get("skill") or ""
    item["_type"] = "result"
    item["_hay"] = _join(
        item.get("edition"),
        skill,
        skill.lstrip("0"),
        item.get("skill_name"),
        item.get("member_code"),
        item.get("member_name"),
        item.get("competitor_names"),
        item.get("medal"),
        item.get("position"),
    )
    return item


def skill_num_token(token: str) -> bool:
    return token.isdigit() and 1 <= len(token) <= 2


def skill_matches(item: dict, token: str) -> bool:
    skill = str(item.get("skill") or "")
    want = token.lstrip("0") or token
    got = skill.lstrip("0") or skill
    return skill == token or got == want or skill == token.zfill(2)


def file_tokens_match(item: dict, tokens: list[str]) -> bool:
    hay = item["_hay"]
    for token in tokens:
        if skill_num_token(token):
            if not skill_matches(item, token):
                return False
        elif token not in hay:
            return False
    return True


def result_tokens_match(item: dict, tokens: list[str]) -> bool:
    hay = item["_hay"]
    for token in tokens:
        if skill_num_token(token):
            if not skill_matches(item, token):
                return False
        elif token not in hay:
            return False
    return True


def score_file(item: dict, tokens: list[str]) -> int | None:
    if not file_tokens_match(item, tokens):
        return None
    skill = (item.get("skill") or "").lower()
    skill_n = skill.lstrip("0") or skill
    name = (item.get("skill_name") or "").lower()
    filename = (item.get("filename") or "").lower()
    if any(token == skill or token == skill_n for token in tokens):
        return 0
    if any(token in name for token in tokens):
        return 1
    inner = " ".join(item.get("inner") or []).lower()
    if any(token in inner for token in tokens):
        return 1
    if any(token in filename for token in tokens):
        return 2
    return 3


def score_result(item: dict, tokens: list[str]) -> int | None:
    if not result_tokens_match(item, tokens):
        return None
    names = (item.get("competitor_names") or "").lower()
    skill = (item.get("skill") or "").lower()
    skill_n = skill.lstrip("0") or skill
    if any(token in names for token in tokens):
        return 0
    if any(token == skill or token == skill_n for token in tokens):
        return 1
    return 2


def load_files_db() -> list[dict]:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT * FROM items").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()
    items = []
    for row in rows:
        rec = record_from_row(row)
        rec["doc_label"] = row["doc_type"] or rec.get("doc_label")
        rec["state"] = row["state"]
        items.append(attach_file(rec))
    return items


def load_files_csv() -> list[dict]:
    if not CATALOG.exists():
        return []
    items = []
    with CATALOG.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            items.append(
                attach_file(
                    {
                        "edition": row.get("edition") or "",
                        "edition_name": row.get("edition_name") or "",
                        "kind": row.get("kind") or "",
                        "skill": row.get("skill") or "",
                        "skill_name": row.get("skill_name") or "",
                        "stage": row.get("stage") or "",
                        "lang": row.get("lang") or "",
                        "filename": row.get("filename") or "",
                        "bytes": int(row.get("bytes") or 0),
                        "store_path": row.get("store_path") or "",
                        "state": row.get("state") or "done",
                        "release": row.get("release") or "",
                        "asset": row.get("asset") or "",
                        "url": row.get("url") or "",
                    }
                )
            )
    return items


def load_results() -> list[dict]:
    if not RESULTS.exists():
        return []
    items = []
    with RESULTS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            items.append(
                attach_result(
                    {
                        "edition": row.get("edition") or "",
                        "skill": row.get("skill") or "",
                        "skill_name": row.get("skill_name") or "",
                        "member_code": row.get("member_code") or "",
                        "member_name": row.get("member_name") or "",
                        "position": row.get("position") or "",
                        "medal": row.get("medal") or "",
                        "mark": row.get("mark") or "",
                        "competitor_names": row.get("competitor_names") or "",
                    }
                )
            )
    return items


def load_zip_members() -> dict[str, list[str]]:
    path = ROOT / "indexes" / "zip-members.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def with_inner_names(files: list[dict]) -> list[dict]:
    members = load_zip_members()
    for item in files:
        inner = members.get(item.get("store_path") or "") or []
        item["inner"] = inner
        if inner:
            item["_hay"] += " " + " ".join(inner).lower()
    return files


def catalog() -> dict:
    now = time.time()
    if _cache["files"] and now - _cache["built"] < 8:
        return _cache
    _cache["files"] = with_inner_names(load_files_db() if DB.exists() else load_files_csv())
    _cache["results"] = load_results()
    _cache["built"] = now
    return _cache


def public_file(item: dict) -> dict:
    return {
        "type": "file",
        "edition": item.get("edition") or "",
        "edition_name": item.get("edition_name") or "",
        "skill": item.get("skill") or "",
        "skill_name": item.get("skill_name") or "",
        "kind": item.get("kind") or "",
        "kind_label": item.get("doc_label") or "",
        "stage": item.get("stage") or "",
        "lang": item.get("lang") or "",
        "filename": item.get("filename") or "",
        "bytes": int(item.get("bytes") or 0),
        "state": item.get("state") or "",
        "store_path": item.get("store_path") or "",
        "release": item.get("release") or "",
        "asset": item.get("asset") or "",
        "url": item.get("url") or "",
        "inner": (item.get("inner") or [])[:12],
        "inner_count": len(item.get("inner") or []),
    }


def public_result(item: dict) -> dict:
    return {
        "type": "result",
        "edition": item.get("edition") or "",
        "skill": item.get("skill") or "",
        "skill_name": item.get("skill_name") or "",
        "member_code": item.get("member_code") or "",
        "member_name": item.get("member_name") or "",
        "position": item.get("position") or "",
        "medal": item.get("medal") or "",
        "mark": item.get("mark") or "",
        "competitor_names": item.get("competitor_names") or "",
    }


def year_key(code: str) -> int:
    digits = "".join(ch for ch in code if ch.isdigit())
    return int(digits[:4]) if len(digits) >= 4 else 0


def search(query: str, scope: str = "all", edition: str = "", kind: str = "") -> dict:
    tokens = tokenize(query)
    data = catalog()
    found: list[tuple[int, dict]] = []
    if scope in {"all", "files"}:
        for item in data["files"]:
            if edition and item.get("edition") != edition:
                continue
            if kind and item.get("kind") != kind:
                continue
            if not tokens and not edition and not kind:
                continue
            scored = score_file(item, tokens) if tokens else 4
            if scored is None:
                continue
            found.append((scored, item))
    if scope in {"all", "results"} and not kind:
        for item in data["results"]:
            if edition and item.get("edition") != edition:
                continue
            if not tokens:
                continue
            scored = score_result(item, tokens)
            if scored is None:
                continue
            found.append((scored, item))
    found.sort(
        key=lambda pair: (
            pair[0],
            0 if pair[1]["_type"] == "result" or pair[1].get("state") == "done" else 1,
            -year_key(pair[1].get("edition") or ""),
            pair[1].get("skill") or "",
        )
    )
    items = []
    for scored, item in found[:LIMIT]:
        payload = public_file(item) if item["_type"] == "file" else public_result(item)
        payload["score"] = scored
        items.append(payload)
    editions = sorted({item.get("edition") or "" for item in data["files"] if item.get("edition")})
    kinds = sorted({item.get("kind") or "" for item in data["files"] if item.get("kind")})
    return {
        "query": query,
        "total": len(found),
        "shown": len(items),
        "items": items,
        "editions": editions,
        "kinds": [{"code": code, "label": KIND_LABEL.get(code, code)} for code in kinds],
        "files": len(data["files"]),
        "results": len(data["results"]),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/search", "/index.html", "/search.html"}:
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/search":
            query = parse_qs(parsed.query)
            payload = search(
                (query.get("q") or [""])[0],
                (query.get("scope") or ["all"])[0],
                (query.get("edition") or [""])[0],
                (query.get("kind") or [""])[0],
            )
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path.startswith("/file/"):
            self._send_file(STORE, parsed.path[6:], download=True)
            return
        if parsed.path.startswith("/extract/"):
            self._extract(parsed.path[9:])
            return
        if parsed.path.startswith("/work/"):
            self._send_file(WORK, parsed.path[6:], download=False)
            return
        self.send_error(404)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _extract(self, rel: str) -> None:
        store_path = unquote(rel).lstrip("/")
        try:
            dest, files = extract_store_zip(store_path)
        except FileNotFoundError:
            self.send_error(404, "本地还没有这个文件")
            return
        except ValueError:
            self.send_error(400, "只能解压 zip")
            return
        except zipfile.BadZipFile:
            self.send_error(400, "不是有效的 zip")
            return
        self._send(200, listing_html(dest, store_path, files), "text/html; charset=utf-8")

    def _send_file(self, root: Path, rel: str, *, download: bool) -> None:
        target = safe_under(root, unquote(rel).lstrip("/"))
        if target is None or not target.is_file():
            self.send_error(404, "本地还没有这个文件")
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        suffix = target.suffix.lower()
        inline = suffix in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".html", ".svg"}
        disposition = "inline" if inline or not download else "attachment"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'{disposition}; filename="{target.name}"')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(*, host: str = HOST, port: int = PORT, open_browser: bool = False) -> None:
    if not PAGE.exists():
        raise SystemExit(f"找不到页面：{PAGE}")
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"搜索页：{url}")
    print("搜技能编号、项目名、文件名或选手姓名。只搜目录和成绩名单，不拆 PDF 正文。")
    if open_browser:
        webbrowser.open(url)
    server.serve_forever()


def _self_test() -> None:
    item = attach_file(
        {
            "edition": "WSC2026",
            "edition_name": "WorldSkills Shanghai 2026",
            "kind": "TP",
            "skill": "39",
            "skill_name": "Cooking",
            "stage": "actual",
            "lang": "en",
            "filename": "WSC2026_TP39_actual_en.zip",
            "store_path": "WSC2026/TP/39/actual/en/WSC2026_TP39_actual_en.zip",
            "state": "done",
            "bytes": 10,
        }
    )
    assert score_file(item, tokenize("39")) == 0
    assert score_file(item, tokenize("Cooking")) == 1
    item["inner"] = ["Marking_Scheme.xlsx"]
    item["_hay"] += " marking_scheme.xlsx"
    assert score_file(item, tokenize("marking")) == 1
    assert score_file(item, tokenize("tp39")) == 2
    assert score_file(item, tokenize("welding")) is None
    other = attach_file(
        {
            "edition": "WSC2007",
            "kind": "DOC",
            "skill": "01",
            "skill_name": "Polymechanics",
            "filename": "IL01_39JP_OC.pdf",
            "store_path": "WSC2007/DOC/01/und/IL01_39JP_OC.pdf",
            "state": "done",
        }
    )
    assert score_file(other, tokenize("39")) is None
    person = attach_result(
        {
            "edition": "WSC2024",
            "skill": "39",
            "skill_name": "Cooking",
            "member_name": "Korea",
            "competitor_names": "JANE DOE",
            "medal": "Gold",
        }
    )
    assert score_result(person, tokenize("jane")) == 0
    assert score_result(person, tokenize("39")) == 1
    assert safe_under(STORE, "../secret") is None
    listed = public_file({**item, "url": "https://api.worldskills.org/resources/download/1"})
    assert listed["url"].endswith("/download/1")
    print("search self-test ok")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="本机搜索已归档的资料和成绩")
    parser.add_argument("--open", action="store_true", help="用系统浏览器打开")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", default=HOST)
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port, open_browser=args.open)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--self-test"]:
        _self_test()
    else:
        main()
