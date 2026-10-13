"""进度页用的 GitHub 分包上传探测：本地 dist zip、gh 进程、速度。"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from layout import KIND_ASSET

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
_KIND_NAMES = sorted({*KIND_ASSET.values(), "other"}, key=len, reverse=True)
_KIND_TAIL = re.compile(
    rf"-({'|'.join(re.escape(name) for name in _KIND_NAMES)})(?:-part\d+)?$",
    re.I,
)

_lock = threading.Lock()
_sample: dict[str, Any] = {}
_started = False


def fmt_bytes(n: float) -> str:
    value = float(max(n, 0))
    if value < 1024:
        return f"{int(value)} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    return f"{value / (1024 * 1024 * 1024):.2f} GB"


def fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "—"
    return f"{fmt_bytes(bps)}/s"


def eta_from_seconds(raw: float) -> str:
    if raw <= 0:
        return "—"
    sec = int(raw)
    hours, sec = divmod(sec, 3600)
    minutes, sec = divmod(sec, 60)
    if hours:
        return f"大约还要 {hours} 小时 {minutes} 分"
    if minutes:
        return f"大约还要 {minutes} 分 {sec} 秒"
    return f"大约还要 {sec} 秒"


def tag_from_pack_name(name: str) -> str:
    stem = name.removesuffix(".zip")
    return _KIND_TAIL.sub("", stem)


def local_packs() -> list[dict[str, Any]]:
    if not DIST.exists():
        return []
    return [
        {"name": path.name, "bytes": path.stat().st_size, "path": str(path)}
        for path in sorted(DIST.glob("*.zip"))
        if path.is_file()
    ]


def github_repo() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", (result.stdout or "").strip())
    return match.group(1) if match else None


def etime_seconds(text: str) -> float:
    raw = text.strip()
    days = 0
    if "-" in raw:
        day_text, raw = raw.split("-", 1)
        days = int(day_text)
    parts = [int(part) for part in raw.split(":") if part]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    elif parts:
        hours, minutes, seconds = 0, 0, parts[0]
    else:
        return 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def running_upload() -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        parsed = _parse_upload_ps(line)
        if parsed:
            return parsed
    return None


def _parse_upload_ps(line: str) -> dict[str, Any] | None:
    match = re.match(r"\s*(\d+)\s+(\S+)\s+(.*)$", line)
    if not match:
        return None
    pieces = match.group(3).split()
    index = _gh_upload_index(pieces)
    if index is None:
        return None
    try:
        tag = pieces[index + 3].strip("\"'")
        raw_path = pieces[index + 4].strip("\"'")
    except IndexError:
        return None
    if "$" in tag or "$" in raw_path or tag.startswith("-") or raw_path.startswith("-"):
        return None
    path = Path(raw_path)
    if not path.name.endswith(".zip"):
        return None
    return {
        "pid": int(match.group(1)),
        "elapsed": etime_seconds(match.group(2)),
        "tag": tag,
        "name": path.name,
        "bytes": path.stat().st_size if path.is_file() else 0,
    }


def _gh_upload_index(pieces: list[str]) -> int | None:
    for index, piece in enumerate(pieces):
        if Path(piece).name != "gh":
            continue
        if index + 4 < len(pieces) and pieces[index + 1] == "release" and pieces[index + 2] == "upload":
            return index
    return None


def nettop_bytes_out(pid: int) -> int | None:
    try:
        result = subprocess.run(
            ["nettop", "-n", "-p", str(pid), "-l", "1", "-s", "1", "-x"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sent = 0
    for line in result.stdout.splitlines():
        sent = max(sent, _line_bytes_out(line, pid))
    return sent or None


def _line_bytes_out(line: str, pid: int) -> int:
    parts = line.split()
    if "Established" in parts:
        try:
            here = parts.index("Established")
            return int(parts[here + 2])
        except (ValueError, IndexError):
            return 0
    if f".{pid}" not in line:
        return 0
    nums = [int(part) for part in parts if part.isdigit()]
    return nums[1] if len(nums) >= 2 else 0


def release_assets(tag: str) -> dict[str, int]:
    repo = github_repo()
    if not repo or not tag:
        return {}
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/releases/tags/{tag}", "--jq", ".assets[] | [.name,.size] | @tsv"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    assets: dict[str, int] = {}
    for line in result.stdout.splitlines():
        name, _, size = line.partition("\t")
        if name and size.isdigit():
            assets[name] = int(size)
    return assets


def refresh_sample() -> None:
    running = running_upload()
    packs = local_packs()
    tag = (running or {}).get("tag") or (tag_from_pack_name(packs[0]["name"]) if packs else "")
    with _lock:
        prev = dict(_sample)
    sent, speed = _current_speed(running, prev)
    remote, remote_at = _cached_remote(prev, tag)
    sample = {
        "sampled_at": time.time(),
        "pid": (running or {}).get("pid"),
        "tag": tag,
        "name": (running or {}).get("name"),
        "file_bytes": (running or {}).get("bytes") or 0,
        "elapsed": (running or {}).get("elapsed") or 0,
        "sent": sent,
        "speed": speed,
        "remote": remote,
        "remote_at": remote_at,
        "running": bool(running),
        "packs": packs,
    }
    with _lock:
        _sample.clear()
        _sample.update(sample)


def _cached_remote(prev: dict[str, Any], tag: str) -> tuple[dict[str, int], float]:
    fresh = prev.get("tag") == tag and time.time() - float(prev.get("remote_at") or 0) < 20
    if fresh:
        return prev.get("remote") or {}, float(prev.get("remote_at") or 0)
    if not tag:
        return {}, 0.0
    return release_assets(tag), time.time()


def _current_speed(running: dict[str, Any] | None, prev: dict[str, Any]) -> tuple[int | None, float]:
    if not running:
        return None, 0.0
    sent = nettop_bytes_out(int(running["pid"])) or 0
    average = int(sent) / running["elapsed"] if sent and running["elapsed"] > 1 else 0.0
    if prev.get("pid") != running["pid"] or prev.get("sent") is None or not prev.get("sampled_at"):
        return sent, average
    dt = max(time.time() - float(prev["sampled_at"]), 0.2)
    instant = (int(sent) - int(prev["sent"])) / dt
    return sent, instant if instant > 0 else average


def start_probe() -> None:
    global _started
    if _started:
        return
    _started = True

    def loop() -> None:
        while True:
            try:
                refresh_sample()
            except Exception:
                pass
            time.sleep(2)

    threading.Thread(target=loop, daemon=True).start()


def upload_status() -> dict[str, Any]:
    with _lock:
        sample = dict(_sample)
    packs = sample.get("packs") or local_packs()
    items, uploaded_bytes, uploaded_n = _pack_marks(packs, sample)
    total = sum(int(pack["bytes"]) for pack in packs)
    progress_bytes = _progress_bytes(sample, packs, uploaded_bytes)
    remain = max(total - progress_bytes, 0)
    speed = float(sample["speed"]) if sample.get("speed") is not None else 0.0
    return {
        "status": _upload_label(sample, packs, uploaded_n),
        "pct": 100.0 * progress_bytes / total if total else 0.0,
        "now": _upload_now(sample),
        "eta": eta_from_seconds(remain / speed) if speed > 0 and remain else "—",
        "speed": fmt_speed(speed),
        "uploaded_n": uploaded_n,
        "pack_n": len(packs),
        "uploaded_bytes": fmt_bytes(uploaded_bytes),
        "total_bytes": fmt_bytes(total),
        "tag": sample.get("tag") or "—",
        "items": items,
    }


def _pack_marks(packs: list[dict[str, Any]], sample: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    remote = sample.get("remote") or {}
    current_name = sample.get("name") if sample.get("running") else ""
    items = []
    uploaded_bytes = 0
    uploaded_n = 0
    for pack in packs:
        name = pack["name"]
        size = int(pack["bytes"])
        if remote.get(name) == size:
            uploaded_bytes += size
            uploaded_n += 1
            mark = "已上传"
        elif name == current_name:
            mark = "正在传"
        else:
            mark = "等待"
        items.append({"name": name, "bytes": size, "mark": mark})
    return items, uploaded_bytes, uploaded_n


def _progress_bytes(sample: dict[str, Any], packs: list[dict[str, Any]], uploaded_bytes: int) -> int:
    if not sample.get("running"):
        return uploaded_bytes
    current_name = sample.get("name")
    file_bytes = int(sample.get("file_bytes") or 0)
    if current_name:
        file_bytes = next((int(pack["bytes"]) for pack in packs if pack["name"] == current_name), file_bytes)
    sent = min(int(sample.get("sent") or 0), file_bytes)
    return uploaded_bytes + sent


def _upload_label(sample: dict[str, Any], packs: list[dict[str, Any]], uploaded_n: int) -> str:
    if sample.get("running"):
        return "正在上传"
    if packs and uploaded_n == len(packs):
        return "上传已完成"
    if packs and uploaded_n:
        return "上传未完成"
    if packs:
        return "待上传"
    return "没有待上传分包"


def _upload_now(sample: dict[str, Any]) -> str:
    if not sample.get("running") or not sample.get("name"):
        return "没有正在传的文件"
    sent = min(int(sample.get("sent") or 0), int(sample.get("file_bytes") or 0))
    return f"{sample['name']}  {fmt_bytes(sent)} / {fmt_bytes(sample.get('file_bytes') or 0)}"


def _self_test() -> None:
    assert tag_from_pack_name("wsc-2015-sao-paulo-skl-part06.zip") == "wsc-2015-sao-paulo"
    assert tag_from_pack_name("wsc-2015-sao-paulo-tp-part02.zip") == "wsc-2015-sao-paulo"
    assert tag_from_pack_name("wsc-2022-special-edition-td.zip") == "wsc-2022-special-edition"
    parsed = _parse_upload_ps(
        "99776  17:02 gh release upload wsc-2015-sao-paulo dist/wsc-2015-sao-paulo-skl-part06.zip --clobber"
    )
    assert parsed is not None
    assert parsed["tag"] == "wsc-2015-sao-paulo"
    assert parsed["name"] == "wsc-2015-sao-paulo-skl-part06.zip"
    wrapper = _parse_upload_ps('99601  17:17 /bin/zsh -c gh release upload "$TAG" "$f" --clobber')
    assert wrapper is None
    assert fmt_speed(0) == "—"
    assert "MB" in fmt_bytes(1_500_000)
    sample = "10:09:19.551676    tcp4 198.18.0.1:56644<->198.18.1.204:443  utun1024   Established           58250        35312472"
    assert _line_bytes_out(sample, 6275) == 35312472
    summary = "10:09:19.551803 gh.6275                                                                                                                66475        35314899       122         0         0"
    assert _line_bytes_out(summary, 6275) == 35314899
