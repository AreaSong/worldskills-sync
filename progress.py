#!/usr/bin/env python3
"""生成本地同步进度页。运行：python progress.py --open"""

from __future__ import annotations

import argparse
import html
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from progress_upload import eta_from_seconds, fmt_bytes, start_probe, upload_status

ROOT = Path(__file__).resolve().parent
DB = ROOT / "downloads" / "catalog.sqlite"
LOG = ROOT / "downloads" / "download.log"
STORE = ROOT / "store"
OUT = ROOT / "downloads" / "progress.html"
HOST = "127.0.0.1"
PORT = 8765


def counts() -> dict:
    empty = {"states": {}, "total": 0, "working": [], "by_edition": [], "min_done": None}
    if not DB.exists():
        return empty
    db = None
    try:
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        states = {row["state"]: row["n"] for row in db.execute("SELECT state, COUNT(*) AS n FROM items GROUP BY state")}
        working = db.execute(
            "SELECT edition_code, doc_type, filename FROM items WHERE state='working' LIMIT 8"
        ).fetchall()
        by_edition = list(
            db.execute(
                "SELECT edition_code, COUNT(*) AS n FROM items WHERE state='done' GROUP BY edition_code ORDER BY n DESC"
            )
        )
        min_done = db.execute("SELECT MIN(finished_at) AS t FROM items WHERE state='done'").fetchone()["t"]
        return {
            "states": states,
            "total": sum(states.values()),
            "working": [dict(row) for row in working],
            "by_edition": [(row["edition_code"], row["n"]) for row in by_edition],
            "min_done": min_done,
        }
    except sqlite3.OperationalError:
        return empty
    finally:
        if db is not None:
            db.close()


def log_tail(n: int = 80) -> str:
    if not LOG.exists():
        return "还没有日志。下载开始后会出现 downloads/download.log。"
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:]) or "日志还是空的。"


def eta_text(done: int, queued: int, min_done: float | None) -> str:
    if done <= 0 or not min_done or queued <= 0:
        return "—"
    elapsed = max(time.time() - float(min_done), 1)
    rate = done / elapsed
    if rate <= 0:
        return "—"
    return eta_from_seconds(queued / rate)


def _status_label(done: int, queued: int, working: int) -> str:
    if working:
        return "正在下载"
    if done == 0 and queued:
        return "未开始"
    if queued:
        return "已停止"
    return "已完成"


def _now_label(current: list | dict | None, queued: int, working: int) -> str:
    rows = [current] if isinstance(current, dict) else (current or [])
    if not rows:
        if working:
            return "等待下一个文件…"
        return "还没有开始下载" if queued else "队列已空"
    first = rows[0]
    label = "  ".join(
        part for part in (first.get("edition_code"), first.get("doc_type"), first.get("filename")) if part
    )
    return f"{label}  等 {len(rows)} 个" if len(rows) > 1 else label


def _edition_items(rows: list[tuple[str | None, int]]) -> str:
    if not rows:
        return "<li>还没有保存的文件</li>"
    return "".join(
        f"<li><span>{html.escape(code or '未知')}</span><b>{n}</b></li>" for code, n in rows[:12]
    )


_STORE_COUNT: tuple[float, int] = (0.0, 0)


def _store_file_count() -> int:
    global _STORE_COUNT
    cached_at, cached_n = _STORE_COUNT
    if time.time() - cached_at < 10:
        return cached_n
    if not STORE.exists():
        _STORE_COUNT = (time.time(), 0)
        return 0
    count = sum(1 for path in STORE.rglob("*") if path.is_file() and path.name != ".gitkeep")
    _STORE_COUNT = (time.time(), count)
    return count


def _pack_items(items: list[dict]) -> str:
    if not items:
        return "<li>dist/ 里还没有 zip</li>"
    rows = [
        f"<li><span>{html.escape(item['mark'])} · {html.escape(item['name'])}</span>"
        f"<b>{html.escape(fmt_bytes(item['bytes']))}</b></li>"
        for item in items[:16]
    ]
    if len(items) > 16:
        rows.append(f"<li><span>另外 {len(items) - 16} 个分包</span><b></b></li>")
    return "".join(rows)


def page() -> bytes:
    data = counts()
    states = data["states"]
    done = int(states.get("done", 0))
    queued = int(states.get("queued", 0))
    working = int(states.get("working", 0))
    remaining = max(done + queued + working, 1)
    upload = upload_status()
    return _html(
        {
            "status": html.escape(_status_label(done, queued, working)),
            "pct": 100.0 * done / remaining,
            "now": html.escape(_now_label(data["working"], queued, working)),
            "eta": html.escape(eta_text(done, queued, data["min_done"])),
            "done": done,
            "queued": queued,
            "skipped": int(states.get("skipped", 0)),
            "error": int(states.get("error", 0)) + int(states.get("missing", 0)),
            "store_n": _store_file_count(),
            "total": int(data["total"]),
            "editions": _edition_items(data["by_edition"]),
            "log": html.escape(log_tail()),
            "up_status": html.escape(upload["status"]),
            "up_pct": upload["pct"],
            "up_now": html.escape(str(upload["now"])),
            "up_eta": html.escape(str(upload["eta"])),
            "up_speed": html.escape(str(upload["speed"])),
            "up_count": f"{upload['uploaded_n']} / {upload['pack_n']}",
            "up_bytes": f"{upload['uploaded_bytes']} / {upload['total_bytes']}",
            "up_tag": html.escape(str(upload["tag"])),
            "up_items": _pack_items(upload["items"]),
        }
    )


def _html(ctx: dict) -> bytes:
    return PAGE_TEMPLATE.format(**ctx).encode("utf-8")


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>下载 {pct:.0f}% · 上传 {up_pct:.0f}%</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{ margin: 0; font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; background: #111; color: #eee; overflow: hidden; }}
    .layout {{ display: grid; grid-template-columns: 1fr 1fr; height: 100vh; }}
    .pane {{ min-width: 0; min-height: 0; }}
    .stats {{ overflow: auto; padding: 28px 28px 36px; }}
    h1 {{ font-size: 22px; font-weight: 650; margin: 0 0 8px; }}
    h2 {{ font-size: 15px; font-weight: 650; margin: 18px 0 10px; }}
    .meta {{ color: #aaa; margin: 0 0 18px; }}
    .tracks {{ display: grid; gap: 14px; margin: 0 0 18px; }}
    .track {{ background: #1b1b1b; border: 1px solid #2e2e2e; border-radius: 12px; padding: 12px 14px 14px; }}
    .track .label {{ display: flex; justify-content: space-between; gap: 12px; margin: 0 0 8px; font-size: 13px; }}
    .track .label b {{ font-variant-numeric: tabular-nums; }}
    .bar {{ height: 14px; background: #2a2a2a; border-radius: 999px; overflow: hidden; }}
    .bar > i {{ display: block; height: 100%; width: 0; background: #6ee7a8; }}
    .bar.upload > i {{ background: #93c5fd; }}
    .track .meta {{ margin: 8px 0 0; font-size: 12px; }}
    .pct {{ font-size: 32px; font-variant-numeric: tabular-nums; margin: 14px 0 4px; letter-spacing: -0.03em; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 18px 0 8px; }}
    .card {{ background: #1b1b1b; border: 1px solid #2e2e2e; border-radius: 12px; padding: 12px 14px; }}
    .card span {{ display: block; color: #888; font-size: 12px; }}
    .card b {{ font-size: 18px; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    ul {{ list-style: none; padding: 0; margin: 0; }}
    li {{ display: flex; justify-content: space-between; gap: 12px; padding: 6px 0; border-bottom: 1px solid #2a2a2a; }}
    li b {{ font-variant-numeric: tabular-nums; }}
    .log {{ display: flex; flex-direction: column; background: #0b0b0b; border-left: 1px solid #2e2e2e; }}
    .log header {{ flex: 0 0 auto; padding: 20px 22px 8px; }}
    .log header .meta {{ margin: 0; }}
    .log pre {{ flex: 1; min-height: 0; margin: 0; padding: 8px 22px 24px; overflow: auto; background: transparent; border: 0; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; word-break: break-all; }}
    @media (max-width: 860px) {{
      body {{ overflow: auto; }}
      .layout {{ grid-template-columns: 1fr; height: auto; }}
      .log {{ min-height: 50vh; border-left: 0; border-top: 1px solid #2e2e2e; }}
    }}
  </style>
</head>
<body>
<div class="layout">
  <section class="pane stats">
    <h1>{status} · {up_status}</h1>
    <p class="meta">保存到 store/ · 每 2 秒更新 · <a href="http://127.0.0.1:8766/" style="color:#6ee7a8">搜索</a></p>
    <div class="tracks">
      <div class="track">
        <div class="label"><span>下载</span><b>{pct:.1f}%</b></div>
        <div class="bar download"><i style="width:{pct:.2f}%"></i></div>
        <p class="meta">{now} · {eta}</p>
      </div>
      <div class="track">
        <div class="label"><span>上传 {up_tag} · 速度 {up_speed}</span><b>{up_pct:.1f}%</b></div>
        <div class="bar upload"><i style="width:{up_pct:.2f}%"></i></div>
        <p class="meta">{up_now} · {up_eta}</p>
      </div>
    </div>
    <div class="grid">
      <div class="card"><span>已保存</span><b>{done}</b></div>
      <div class="card"><span>队列中</span><b>{queued}</b></div>
      <div class="card"><span>已跳过</span><b>{skipped}</b></div>
      <div class="card"><span>失败 / 找不到</span><b>{error}</b></div>
      <div class="card"><span>正文库文件</span><b>{store_n}</b></div>
      <div class="card"><span>合计条目</span><b>{total}</b></div>
      <div class="card"><span>已上传分包</span><b>{up_count}</b></div>
      <div class="card"><span>体积</span><b>{up_bytes}</b></div>
    </div>
    <ul>{up_items}</ul>
    <h2>已保存的届次</h2>
    <ul>{editions}</ul>
  </section>
  <section class="pane log">
    <header>
      <h2>日志</h2>
      <p class="meta">downloads/download.log</p>
    </header>
    <pre id="log">{log}</pre>
  </section>
</div>
<script>
(function () {{
  const stats = document.querySelector(".stats");
  const log = document.getElementById("log");
  const key = "ws-progress-scroll";

  function read() {{
    try {{
      return JSON.parse(sessionStorage.getItem(key) || "{{}}");
    }} catch (err) {{
      return {{}};
    }}
  }}

  function save() {{
    const remain = log ? log.scrollHeight - log.clientHeight - log.scrollTop : 0;
    sessionStorage.setItem(key, JSON.stringify({{
      stats: stats ? stats.scrollTop : 0,
      log: log ? log.scrollTop : 0,
      win: window.scrollY,
      stick: remain < 48
    }}));
  }}

  function restore(saved) {{
    saved = saved || read();
    if (stats) stats.scrollTop = saved.stats || 0;
    window.scrollTo(0, saved.win || 0);
    if (!log) return;
    log.scrollTop = saved.stick !== false ? log.scrollHeight : (saved.log || 0);
  }}

  if (stats) stats.addEventListener("scroll", save);
  if (log) log.addEventListener("scroll", save);
  window.addEventListener("scroll", save);
  restore();

  async function tick() {{
    const saved = read();
    saved.stats = stats ? stats.scrollTop : 0;
    saved.log = log ? log.scrollTop : 0;
    saved.win = window.scrollY;
    if (log) saved.stick = log.scrollHeight - log.clientHeight - log.scrollTop < 48;
    try {{
      const res = await fetch(location.href, {{ cache: "no-store" }});
      const next = new DOMParser().parseFromString(await res.text(), "text/html");
      const nextStats = next.querySelector(".stats");
      const nextLog = next.querySelector("#log");
      document.title = next.title;
      if (stats && nextStats) stats.innerHTML = nextStats.innerHTML;
      if (log && nextLog) log.innerHTML = nextLog.innerHTML;
      restore(saved);
    }} catch (err) {{
      sessionStorage.setItem(key, JSON.stringify(saved));
      location.reload();
    }}
  }}

  setInterval(tick, 2000);
}})();
</script>
</body>
</html>
"""


def write_file() -> Path:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(page())
    return OUT


def watch(*, interval: float = 2.0) -> None:
    start_probe()
    threading.Thread(target=_try_serve, daemon=True).start()
    while True:
        write_file()
        time.sleep(interval)


def _try_serve() -> None:
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        return
    print(f"本机页面：http://{HOST}:{PORT}/")
    server.serve_forever()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/", "/index.html", "/progress"}:
            self.send_error(404)
            return
        payload = page()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve() -> None:
    start_probe()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"打开进度页：http://{HOST}:{PORT}/")
    server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="查看本地下载和上传进度")
    parser.add_argument("--open", action="store_true", help="用系统浏览器打开进度页")
    parser.add_argument("--once", action="store_true", help="只写一次 HTML，不循环更新")
    parser.add_argument("--http", action="store_true", help="在本机 8765 端口提供页面")
    args = parser.parse_args(argv)
    if not args.once:
        start_probe()
        time.sleep(0.2)
    path = write_file()
    print(f"进度页：{path}")
    if args.open:
        webbrowser.open(path.as_uri())
    if args.http:
        serve()
        return
    if args.once:
        return
    print("页面会持续更新，用 Ctrl+C 停止。")
    watch()


if __name__ == "__main__":
    main()
