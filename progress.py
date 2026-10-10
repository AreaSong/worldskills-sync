#!/usr/bin/env python3
"""生成本地下载进度页。运行：python progress.py --open"""

from __future__ import annotations

import argparse
import html
import sqlite3
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
    sec = int(queued / rate)
    hours, sec = divmod(sec, 3600)
    minutes, sec = divmod(sec, 60)
    if hours:
        return f"大约还要 {hours} 小时 {minutes} 分"
    if minutes:
        return f"大约还要 {minutes} 分 {sec} 秒"
    return f"大约还要 {sec} 秒"


def _status_label(done: int, queued: int, working: int) -> str:
    if working:
        return "正在下载"
    if done == 0 and queued:
        return "未开始"
    if queued:
        return "已停止"
    return "已完成"


def _now_label(current: list | dict | None, queued: int, working: int) -> str:
    if isinstance(current, dict):
        rows = [current]
    else:
        rows = current or []
    if not rows:
        if working:
            return "等待下一个文件…"
        return "还没有开始下载" if queued else "队列已空"
    first = rows[0]
    label = "  ".join(
        part for part in (first.get("edition_code"), first.get("doc_type"), first.get("filename")) if part
    )
    if len(rows) > 1:
        return f"{label}  等 {len(rows)} 个"
    return label


def _edition_items(rows: list[tuple[str | None, int]]) -> str:
    if not rows:
        return "<li>还没有保存的文件</li>"
    return "".join(
        f"<li><span>{html.escape(code or '未知')}</span><b>{n}</b></li>" for code, n in rows[:12]
    )


def _store_file_count() -> int:
    if not STORE.exists():
        return 0
    return sum(1 for path in STORE.rglob("*") if path.is_file() and path.name != ".gitkeep")


def page() -> bytes:
    data = counts()
    states = data["states"]
    done = int(states.get("done", 0))
    queued = int(states.get("queued", 0))
    working = int(states.get("working", 0))
    skipped = int(states.get("skipped", 0))
    remaining = max(done + queued + working, 1)
    pct = 100.0 * done / remaining
    return _html(
        {
            "status": html.escape(_status_label(done, queued, working)),
            "pct": pct,
            "now": html.escape(_now_label(data["working"], queued, working)),
            "eta": html.escape(eta_text(done, queued, data["min_done"])),
            "done": done,
            "queued": queued,
            "skipped": skipped,
            "error": int(states.get("error", 0)) + int(states.get("missing", 0)),
            "store_n": _store_file_count(),
            "total": int(data["total"]),
            "editions": _edition_items(data["by_edition"]),
            "log": html.escape(log_tail()),
        }
    )


def _html(ctx: dict) -> bytes:
    return f"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="2">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>下载进度 {ctx['pct']:.1f}%</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{ margin: 0; font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; background: #111; color: #eee; overflow: hidden; }}
    .layout {{ display: grid; grid-template-columns: 1fr 1fr; height: 100vh; }}
    .pane {{ min-width: 0; min-height: 0; }}
    .stats {{ overflow: auto; padding: 28px 28px 36px; }}
    h1 {{ font-size: 22px; font-weight: 650; margin: 0 0 8px; }}
    h2 {{ font-size: 15px; font-weight: 650; margin: 8px 0 10px; }}
    .meta {{ color: #aaa; margin: 0 0 18px; }}
    .bar {{ height: 14px; background: #2a2a2a; border-radius: 999px; overflow: hidden; }}
    .bar > i {{ display: block; height: 100%; width: {ctx['pct']:.2f}%; background: #6ee7a8; }}
    .pct {{ font-size: 32px; font-variant-numeric: tabular-nums; margin: 14px 0 4px; letter-spacing: -0.03em; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 18px 0 22px; }}
    .card {{ background: #1b1b1b; border: 1px solid #2e2e2e; border-radius: 12px; padding: 12px 14px; }}
    .card span {{ display: block; color: #888; font-size: 12px; }}
    .card b {{ font-size: 20px; font-variant-numeric: tabular-nums; }}
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
    <h1>{ctx['status']}</h1>
    <p class="meta">保存到 store/ · 每 2 秒刷新 · <a href="http://127.0.0.1:8766/" style="color:#6ee7a8">搜索</a></p>
    <div class="bar"><i></i></div>
    <div class="pct">{ctx['pct']:.1f}%</div>
    <p class="meta">{ctx['now']} · {ctx['eta']}</p>
    <div class="grid">
      <div class="card"><span>已保存</span><b>{ctx['done']}</b></div>
      <div class="card"><span>队列中</span><b>{ctx['queued']}</b></div>
      <div class="card"><span>已跳过</span><b>{ctx['skipped']}</b></div>
      <div class="card"><span>失败 / 找不到</span><b>{ctx['error']}</b></div>
      <div class="card"><span>正文库文件</span><b>{ctx['store_n']}</b></div>
      <div class="card"><span>合计条目</span><b>{ctx['total']}</b></div>
    </div>
    <h2>已保存的届次</h2>
    <ul>{ctx['editions']}</ul>
  </section>
  <section class="pane log">
    <header>
      <h2>日志</h2>
      <p class="meta">downloads/download.log</p>
    </header>
    <pre id="log">{ctx['log']}</pre>
  </section>
</div>
<script>
  const log = document.getElementById("log");
  if (log) log.scrollTop = log.scrollHeight;
</script>
</body>
</html>
""".encode("utf-8")


def write_file() -> Path:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(page())
    return OUT


def watch(*, interval: float = 2.0) -> None:
    while True:
        write_file()
        time.sleep(interval)


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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"打开进度页：http://{HOST}:{PORT}/")
    server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="查看本地下载进度")
    parser.add_argument("--open", action="store_true", help="用系统浏览器打开进度页")
    parser.add_argument("--once", action="store_true", help="只写一次 HTML，不循环更新")
    parser.add_argument("--http", action="store_true", help="在本机 8765 端口提供页面")
    args = parser.parse_args(argv)
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
