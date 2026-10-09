"""按需解压：正文库里的 zip 原件不动，解开的内容放到 work/。"""

from __future__ import annotations

import html
import json
import zipfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "store"
WORK = ROOT / "work"
MEMBERS_PATH = ROOT / "indexes" / "zip-members.json"


def safe_under(root: Path, rel: str) -> Path | None:
    relative = Path(rel)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    if not root.exists():
        return None
    target = (root / relative).resolve()
    base = root.resolve()
    if target != base and base not in target.parents:
        return None
    return target


def extract_zip(src: Path, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with zipfile.ZipFile(src) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            target = (dest / relative).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as incoming, target.open("wb") as outgoing:
                outgoing.write(incoming.read())
            written.append(relative.as_posix())
    return written


def work_dir_for(store_path: str) -> Path:
    return WORK / Path(store_path).with_suffix("")


def extract_store_zip(store_path: str) -> tuple[Path, list[str]]:
    src = safe_under(STORE, store_path)
    if src is None or not src.is_file():
        raise FileNotFoundError(store_path)
    if src.suffix.lower() != ".zip":
        raise ValueError("只能解压 zip")
    dest = work_dir_for(store_path)
    files = extract_zip(src, dest)
    return dest, files


def unpack_release(zip_path: Path, dest: Path = STORE) -> list[str]:
    if not zip_path.is_file():
        raise FileNotFoundError(zip_path)
    return extract_zip(zip_path, dest)


def list_zip_members(src: Path) -> list[str]:
    names: list[str] = []
    try:
        with zipfile.ZipFile(src) as archive:
            for raw in archive.namelist():
                name = raw.replace("\\", "/")
                if not name or name.endswith("/"):
                    continue
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                names.append(name)
    except (OSError, zipfile.BadZipFile):
        return []
    return names


def build_zip_members(store: Path = STORE) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    if store.exists():
        for path in store.rglob("*.zip"):
            if not path.is_file() or path.stat().st_size < 22:
                continue
            mapping[path.relative_to(store).as_posix()] = list_zip_members(path)
    MEMBERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMBERS_PATH.write_text(json.dumps(mapping, ensure_ascii=False) + "\n", encoding="utf-8")
    return mapping


def listing_html(dest: Path, store_path: str, files: list[str]) -> bytes:
    rel = dest.relative_to(WORK).as_posix()
    items = "".join(
        f'<li><a href="/work/{quote(rel + "/" + name, safe="/")}">{html.escape(name)}</a></li>'
        for name in files
    ) or "<li>压缩包是空的</li>"
    return f"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>已解压 {html.escape(Path(store_path).name)}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin: 0; font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; background: #111; color: #eee; }}
    main {{ max-width: 860px; margin: 0 auto; padding: 28px 24px 48px; }}
    a {{ color: #6ee7a8; }}
    .meta {{ color: #888; }}
    ul {{ padding-left: 1.1em; }}
    li {{ margin: 6px 0; word-break: break-all; }}
  </style>
</head>
<body>
<main>
  <p><a href="/">← 搜索</a></p>
  <h1>{html.escape(Path(store_path).name)}</h1>
  <p class="meta">原件仍在 store/，解开的文件在 work/{html.escape(rel)}/，共 {len(files)} 个。</p>
  <ul>{items}</ul>
</main>
</body>
</html>
""".encode("utf-8")


def _self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        folder = Path(raw)
        sample = folder / "ok.txt"
        sample.write_text("hello", encoding="utf-8")
        zip_path = folder / "a.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.write(sample, "ok.txt")
            archive.writestr("../escape.txt", "nope")
        dest = folder / "out"
        files = extract_zip(zip_path, dest)
        assert files == ["ok.txt"]
        assert (dest / "ok.txt").read_text(encoding="utf-8") == "hello"
        assert not (folder / "escape.txt").exists()
        assert safe_under(dest, "../x") is None
        assert list_zip_members(zip_path) == ["ok.txt"]
    print("extract self-test ok")
