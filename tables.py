"""把届次、项目、成员、IL 目录、成绩表写成可进 git 的名单。"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

import httpx

from classify import EDITION_EVENT_IDS, EDITION_NAMES, MEMBER_AREA_CODES, pad_skill
from layout import record_from_row

API = "https://api.worldskills.org"


def text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if value.get("text"):
            return str(value["text"]).strip()
        if "name" in value:
            named = text_of(value.get("name"))
            if named:
                return named
        if value.get("code"):
            return str(value["code"]).strip()
        return ""
    return str(value).strip()


def json_get(client: httpx.Client, path: str) -> tuple[int, Any]:
    response = client.get(f"{API}{path}")
    if response.status_code == 401:
        return 401, None
    try:
        return response.status_code, response.json()
    except Exception:
        return response.status_code, None


def paginate(client: httpx.Client, path: str, list_key: str, *, limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        glue = "&" if "?" in path else "?"
        status, data = json_get(client, f"{path}{glue}limit={limit}&offset={offset}")
        if status != 200 or not isinstance(data, dict):
            break
        chunk = data.get(list_key) or []
        if not chunk:
            break
        rows.extend(item for item in chunk if isinstance(item, dict))
        if len(chunk) < limit:
            break
        offset += limit
        time.sleep(0.1)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def fetch_editions(client: httpx.Client) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in paginate(client, "/events?type=competition", "events", limit=100):
        code = item.get("code") or ""
        if code in MEMBER_AREA_CODES:
            found[code] = {
                "edition": code,
                "event_id": item.get("id") or "",
                "name": text_of(item.get("name")) or EDITION_NAMES.get(code, code),
                "start_date": item.get("start_date") or "",
                "end_date": item.get("end_date") or "",
                "town": item.get("town") or "",
                "venue": item.get("venue") or "",
            }
    rows = []
    for code in MEMBER_AREA_CODES:
        row = found.get(code) or {
            "edition": code,
            "event_id": EDITION_EVENT_IDS.get(code, ""),
            "name": EDITION_NAMES.get(code, code),
            "start_date": "",
            "end_date": "",
            "town": "",
            "venue": "",
        }
        if not row.get("event_id"):
            row["event_id"] = EDITION_EVENT_IDS.get(code, "")
        rows.append(row)
    return rows


def fetch_members(client: httpx.Client) -> list[dict[str, Any]]:
    rows = []
    for item in paginate(client, "/org/members", "members", limit=50):
        org = item.get("organization") if isinstance(item.get("organization"), dict) else {}
        country = ""
        if isinstance(org, dict):
            country = text_of(org.get("country")) or text_of((org.get("wsEntity") or {}).get("name") if isinstance(org.get("wsEntity"), dict) else "")
        ws_entity = item.get("ws_entity") if isinstance(item.get("ws_entity"), dict) else {}
        rows.append(
            {
                "member_id": item.get("id") or "",
                "code": item.get("code") or "",
                "name": text_of(item.get("name")),
                "name_1058": text_of(item.get("name_1058")),
                "country": country or text_of(ws_entity.get("name") if isinstance(ws_entity, dict) else ""),
            }
        )
    rows.sort(key=lambda item: (str(item.get("code") or ""), str(item.get("member_id") or "")))
    return rows


def fetch_skills(client: httpx.Client, editions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for edition in editions:
        event_id = edition.get("event_id")
        code = edition["edition"]
        if not event_id:
            continue
        status, data = json_get(client, f"/events/{event_id}/skills?limit=200")
        skills = (data or {}).get("skills") if status == 200 and isinstance(data, dict) else []
        if not skills:
            status, data = json_get(client, f"/skillman/skills?event={event_id}")
            skills = (data or {}).get("skills") if status == 200 and isinstance(data, dict) else []
        for skill in skills or []:
            if not isinstance(skill, dict):
                continue
            inner = skill.get("skill") if isinstance(skill.get("skill"), dict) else skill
            number = pad_skill(str(inner.get("number") or skill.get("number") or ""))
            name = text_of(inner.get("name") or skill.get("name"))
            if not number and not name:
                continue
            rows.append(
                {
                    "edition": code,
                    "skill": number or "",
                    "name": name,
                    "status": inner.get("status") or skill.get("status") or "",
                    "skill_id": inner.get("id") or skill.get("id") or "",
                    "type": inner.get("type") or skill.get("type") or "",
                }
            )
        time.sleep(0.1)
    rows.sort(key=lambda item: (item["edition"], item["skill"], item["name"]))
    return rows


def competitor_names(result: dict[str, Any]) -> tuple[str, str]:
    names: list[str] = []
    ids: list[str] = []
    for person in result.get("competitors") or []:
        if not isinstance(person, dict):
            continue
        label = text_of(person.get("public_display_full_name")) or " ".join(
            part for part in [text_of(person.get("first_name")), text_of(person.get("last_name"))] if part
        )
        if label:
            names.append(label)
        if person.get("person_id"):
            ids.append(str(person["person_id"]))
    return "; ".join(names), "; ".join(ids)


def fetch_results(client: httpx.Client) -> list[dict[str, Any]]:
    status, data = json_get(client, "/results/events")
    events = (data or {}).get("events") if status == 200 and isinstance(data, dict) else []
    rows: list[dict[str, Any]] = []
    for event in events or []:
        code = event.get("code") or ""
        if code not in MEMBER_AREA_CODES:
            continue
        event_id = event.get("id")
        status, payload = json_get(client, f"/results/events/{event_id}")
        if status != 200 or not isinstance(payload, dict):
            print(f"成绩表 {code} 无法读取 HTTP {status}")
            continue
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            skill = item.get("skill") if isinstance(item.get("skill"), dict) else {}
            member = item.get("member") if isinstance(item.get("member"), dict) else {}
            names, person_ids = competitor_names(item)
            rows.append(
                {
                    "edition": code,
                    "skill": pad_skill(str(skill.get("number") or "")) or "",
                    "skill_name": text_of(skill.get("name")),
                    "member_code": member.get("code") or "",
                    "member_name": text_of(member.get("name")),
                    "position": item.get("position") if item.get("position") is not None else "",
                    "medal": text_of(item.get("medal")),
                    "mark": item.get("mark") if item.get("mark") is not None else "",
                    "best_of_nation": item.get("best_of_nation") or "",
                    "albert_vidal_award": item.get("albert_vidal_award") or "",
                    "competitor_names": names,
                    "person_ids": person_ids,
                    "published": item.get("published") if item.get("published") is not None else "",
                    "result_id": item.get("id") or "",
                }
            )
        print(f"成绩表 {code} {len(payload.get('results') or [])} 条")
        time.sleep(0.2)
    rows.sort(key=lambda item: (item["edition"], item["skill"], str(item["position"]), item["member_code"]))
    return rows


def fetch_il_index(client: httpx.Client, editions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    status, data = json_get(client, "/il/events")
    if status == 401:
        print("基础设施清单目录需要登录，已跳过。")
        return rows
    if status != 200 or not isinstance(data, dict):
        return rows
    id_to_code: dict[int, str] = {}
    for item in editions:
        try:
            event_id = int(item.get("event_id"))
        except (TypeError, ValueError):
            continue
        id_to_code[event_id] = item["edition"]
    for event in data.get("events") or []:
        event_id = event.get("id")
        code = id_to_code.get(event_id)
        if not code:
            continue
        list_status, lists = json_get(client, f"/il/events/{event_id}/lists")
        if list_status != 200 or not isinstance(lists, dict):
            continue
        for item in lists.get("lists") or []:
            title = text_of(item.get("name"))
            number = ""
            skill_name = title
            parts = title.split(" ", 1)
            if parts and parts[0].isdigit():
                number = pad_skill(parts[0]) or parts[0]
                skill_name = parts[1].strip() if len(parts) > 1 else title
            rows.append(
                {
                    "edition": code,
                    "list_id": item.get("id") or "",
                    "skill": number,
                    "skill_name": skill_name,
                    "name": title,
                }
            )
        time.sleep(0.1)
    return rows


RESOURCE_FIELDS = [
    "edition",
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
    "state",
    "release",
    "asset",
]


def resource_catalog_rows(db_rows: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for row in db_rows:
        record = record_from_row(row)
        rows.append(
            {
                "edition": record.get("edition") or "",
                "kind": record.get("kind") or "",
                "skill": record.get("skill") or "",
                "skill_name": record.get("skill_name") or "",
                "stage": record.get("stage") or "",
                "lang": record.get("lang") or "",
                "filename": record.get("filename") or "",
                "bytes": record.get("bytes") or 0,
                "sha256": record.get("sha256") or "",
                "store_path": record.get("store_path") or "",
                "url": record.get("url") or "",
                "state": record.get("state") or "",
                "release": record.get("release") or "",
                "asset": record.get("asset") or "",
            }
        )
    rows.sort(key=lambda item: (item["edition"], item["kind"], item["skill"], item["filename"]))
    return rows


def split_by_edition(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("edition") or "UNKNOWN"), []).append(row)
    return grouped


def write_data_dir(
    root: Path,
    *,
    editions: list[dict[str, Any]],
    members: list[dict[str, Any]],
    skills: list[dict[str, Any]],
    results: list[dict[str, Any]],
    il_index: list[dict[str, Any]],
    resources: list[dict[str, Any]],
) -> dict[str, int]:
    data_dir = root / "data"
    write_csv(
        data_dir / "editions.csv",
        editions,
        ["edition", "event_id", "name", "start_date", "end_date", "town", "venue"],
    )
    write_csv(data_dir / "members.csv", members, ["member_id", "code", "name", "name_1058", "country"])
    skill_fields = ["edition", "skill", "name", "status", "skill_id", "type"]
    write_csv(data_dir / "skills.csv", skills, skill_fields)
    result_fields = [
        "edition",
        "skill",
        "skill_name",
        "member_code",
        "member_name",
        "position",
        "medal",
        "mark",
        "best_of_nation",
        "albert_vidal_award",
        "competitor_names",
        "person_ids",
        "published",
        "result_id",
    ]
    write_csv(data_dir / "results.csv", results, result_fields)
    il_fields = ["edition", "list_id", "skill", "skill_name", "name"]
    write_csv(data_dir / "il-index.csv", il_index, il_fields)
    write_csv(data_dir / "resources.csv", resources, RESOURCE_FIELDS)

    for code, group in split_by_edition(skills).items():
        write_csv(data_dir / "by-edition" / code / "skills.csv", group, skill_fields)
    for code, group in split_by_edition(results).items():
        write_csv(data_dir / "by-edition" / code / "results.csv", group, result_fields)
    for code, group in split_by_edition(il_index).items():
        write_csv(data_dir / "by-edition" / code / "il-index.csv", group, il_fields)

    counts = {
        "editions": len(editions),
        "members": len(members),
        "skills": len(skills),
        "results": len(results),
        "il_index": len(il_index),
        "resources": len(resources),
    }
    (data_dir / "summary.json").write_text(json.dumps(counts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return counts


def update_resource_catalog(root: Path, db_rows: list[Any]) -> None:
    rows = resource_catalog_rows(db_rows)
    write_csv(root / "data" / "resources.csv", rows, RESOURCE_FIELDS)
    summary_path = root / "data" / "summary.json"
    counts: dict[str, Any] = {}
    if summary_path.exists():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                counts = loaded
        except json.JSONDecodeError:
            counts = {}
    counts["resources"] = len(rows)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(counts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def refresh_tables(root: Path, client: httpx.Client, db_rows: list[Any] | None = None) -> dict[str, int]:
    print("正在写入名单表…")
    editions = fetch_editions(client)
    members = fetch_members(client)
    skills = fetch_skills(client, editions)
    results = fetch_results(client)
    il_index = fetch_il_index(client, editions)
    resources = resource_catalog_rows(db_rows or [])
    counts = write_data_dir(
        root,
        editions=editions,
        members=members,
        skills=skills,
        results=results,
        il_index=il_index,
        resources=resources,
    )
    print(
        "名单："
        f"届次 {counts['editions']}，"
        f"成员 {counts['members']}，"
        f"项目 {counts['skills']}，"
        f"成绩 {counts['results']}，"
        f"IL目录 {counts['il_index']}，"
        f"资源目录 {counts['resources']}"
    )
    return counts
