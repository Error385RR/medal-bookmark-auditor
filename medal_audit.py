#!/usr/bin/env python3
"""Read-only inspector and auditor for Medal's local SQLite contents table."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sqlite3
import sys
import tempfile
from contextlib import closing
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

UTC = dt.timezone.utc
DEFAULT_CUTOFF = dt.date(2026, 8, 15)
GAME_RULES = {
    "valorant": ("valorant",),
    "counter-strike 2": ("counter-strike 2", "counter strike 2", "cs2", "cs:go"),
    "roblox": ("roblox",),
    "tekken": ("tekken",),
}
CLIP_COLUMNS = ["local_content_id", "created_at_utc", "game", "video_path", "video_exists",
    "clip_duration_seconds", "trigger_type", "capture_type", "clip_type", "clip_start_timestamp",
    "export_stats_timestamp", "recorder_event_timestamp", "export_to_start_offset_seconds", "bookmark_count", "in_range_count",
    "out_of_range_count", "missing_timing_count", "metadata_status"]
BOOKMARK_COLUMNS = ["local_content_id", "created_at_utc", "game", "video_path", "clip_duration_seconds",
    "trigger_type", "capture_type", "clip_type", "bookmark_name", "stored_bookmark_time_seconds",
    "bookmark_timestamp", "clip_start_timestamp", "export_stats_timestamp", "recorder_event_timestamp", "export_to_start_offset_seconds",
    "candidate_recovered_position_seconds", "timestamp_derived_position_seconds",
    "candidate_position_difference_seconds", "export_relative_seconds", "formula_consistency_difference_seconds",
    "position_status", "video_exists", "manual_verification_status", "event_name_missing", "unknown_game",
    "unknown_capture_type", "raw_bookmark_json"]


def parse_metadata(raw: Any, sqlite_json: Any = None) -> dict[str, Any]:
    # SQLite may understand JSON stored in a BLOB even when Python cannot
    # decode that BLOB directly. Prefer SQLite's normalized JSON text when
    # available, then fall back to cautious byte decoding.
    if isinstance(sqlite_json, str):
        try:
            value = json.loads(sqlite_json)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    candidates: list[str] = []
    if isinstance(raw, bytes):
        for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
            try:
                candidates.append(raw.decode(encoding))
            except (UnicodeDecodeError, LookupError):
                pass
    elif isinstance(raw, str):
        candidates.append(raw)
    else:
        raise ValueError("metadata is neither text nor bytes")
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except (json.JSONDecodeError, UnicodeError):
            continue
    raise ValueError("metadata could not be decoded as a JSON object")


def nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_timestamp(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if abs(n) > 100_000_000_000:
            n /= 1000
        try:
            return dt.datetime.fromtimestamp(n, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            return None
    return None


def iso(value: dt.datetime | None) -> str:
    return "" if value is None else value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def infer_game(path: Any, rules: dict[str, tuple[str, ...]] | None = None) -> str:
    text = str(path or "").casefold().replace("\\", "/")
    for game, tokens in (rules or GAME_RULES).items():
        if any(token in text for token in tokens):
            return game
    return "Unknown"


def canonical_game(value: str) -> str:
    key = value.casefold().strip()
    aliases = {"valorant": "valorant", "cs2": "counter-strike 2", "counter strike 2": "counter-strike 2",
               "counter-strike 2": "counter-strike 2", "roblox": "roblox", "tekken": "tekken", "unknown": "unknown"}
    return aliases.get(key, key)


def parse_game_rules(items: list[str]) -> dict[str, tuple[str, ...]]:
    rules = dict(GAME_RULES)
    for item in items:
        if "=" not in item:
            raise ValueError(f"invalid --game-match {item!r}; expected GAME=TOKEN[,TOKEN]")
        game, tokens = item.split("=", 1)
        aliases = tuple(token.strip().casefold().replace("\\", "/") for token in tokens.split(",") if token.strip())
        if not game.strip() or not aliases:
            raise ValueError(f"invalid --game-match {item!r}; provide a game and at least one token")
        rules[canonical_game(game)] = aliases
    return rules


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"database file does not exist: {path}")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def iter_rows(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Iterator[sqlite3.Row]:
    cursor = connection.execute(sql, params)
    while batch := cursor.fetchmany(500):
        yield from batch


def sqlite_json_mode(connection: sqlite3.Connection) -> int:
    """Return 2 for JSONB-aware SQLite JSON functions, 1 for text-only, else 0."""
    try:
        connection.execute("SELECT json_valid('{}', 9)").fetchone()
        return 2
    except sqlite3.Error:
        try:
            connection.execute("SELECT json_valid('{}')").fetchone()
            return 1
        except sqlite3.Error:
            return 0


def inspect_db(db: Path) -> None:
    with closing(open_readonly(db)) as con:
        try:
            json_mode = sqlite_json_mode(con)
            if json_mode == 2:
                json_test = "json_valid(metadata, 9)"
                json_value = "json(metadata)"
            elif json_mode == 1:
                json_test = "json_valid(CAST(metadata AS TEXT))"
                json_value = "json(CAST(metadata AS TEXT))"
            else:
                json_test = json_value = "NULL"
            has_json = json_mode > 0
            json_column = f", typeof(metadata) AS metadata_storage_type, length(metadata) AS metadata_length, hex(substr(metadata,1,8)) AS metadata_prefix_hex, CASE WHEN {json_test} THEN {json_value} ELSE NULL END AS metadata_json" if has_json else ""
            rows = iter_rows(con, f"SELECT created_at, video_path, metadata{json_column} FROM contents")
            dates: list[int] = []
            paths: Counter[str] = Counter()
            triggers: Counter[str] = Counter()
            captures: Counter[str] = Counter()
            bookmarks = clips = bad_metadata = 0
            sqlite_json_rows = 0
            sample_storage = None
            for row in rows:
                clips += 1
                if row["created_at"] is not None:
                    dates.append(int(row["created_at"]))
                paths[str(row["video_path"] or "(empty)")] += 1
                if has_json:
                    sqlite_json_rows += int(row["metadata_json"] is not None)
                    if sample_storage is None:
                        sample_storage = (row["metadata_storage_type"], row["metadata_length"], row["metadata_prefix_hex"])
                try:
                    meta = parse_metadata(row["metadata"], row["metadata_json"] if "metadata_json" in row.keys() else None)
                except ValueError:
                    bad_metadata += 1
                    continue
                triggers[str(meta.get("triggerType") or "(missing)")] += 1
                captures[str(meta.get("captureType") or "(missing)")] += 1
                value = meta.get("bookmarks")
                if isinstance(value, list):
                    bookmarks += len(value)
            print(f"Clips: {clips}; bookmarks: {bookmarks}; metadata parse failures: {bad_metadata}")
            if has_json:
                kind = "JSON text or JSONB" if json_mode == 2 else "JSON text after BLOB cast"
                print(f"Metadata recognized as {kind} by SQLite: {sqlite_json_rows}/{clips}")
                if sample_storage:
                    print(f"First metadata storage sample (type, bytes, first 8 bytes hex): {sample_storage}")
            else:
                print("SQLite JSON functions unavailable; using Python metadata decoding only")
            if dates:
                print("Created (UTC): " + iso(dt.datetime.fromtimestamp(min(dates), UTC)) + " to " + iso(dt.datetime.fromtimestamp(max(dates), UTC)))
            for title, counter in (("Video paths", paths), ("Trigger types", triggers), ("Capture types", captures)):
                print(f"\n{title}:")
                for value, count in counter.most_common():
                    print(f"  {count:>6}  {value}")
        except sqlite3.Error as exc:
            raise RuntimeError(f"cannot inspect contents table: {exc}") from exc


def extract(db: Path, out_dir: Path, start: dt.date | None, end: dt.date | None,
            games: list[str] | None, valorant_after: dt.date | None, triggers: set[str],
            captures: set[str], names: set[str], include_no_bookmarks: bool,
            include_out_of_range: bool, include_missing_video: bool,
            tolerance: float, overwrite: bool, game_rules: dict[str, tuple[str, ...]] | None = None) -> tuple[int, int]:
    if start and end and start > end:
        raise ValueError("start date must be on or before end date")
    out_dir.mkdir(parents=True, exist_ok=True)
    clip_path, bookmark_path = out_dir / "clips.csv", out_dir / "bookmarks.csv"
    for path in (clip_path, bookmark_path):
        if path.exists() and not overwrite:
            raise FileExistsError(f"report already exists: {path} (use --overwrite to replace it)")
    selected = {canonical_game(g) for g in games} if games else None
    clips_written = bookmarks_written = 0
    with closing(open_readonly(db)) as con, clip_path.open("w", newline="", encoding="utf-8-sig") as clip_f, bookmark_path.open("w", newline="", encoding="utf-8-sig") as mark_f:
        clip_writer, mark_writer = csv.DictWriter(clip_f, CLIP_COLUMNS), csv.DictWriter(mark_f, BOOKMARK_COLUMNS)
        clip_writer.writeheader(); mark_writer.writeheader()
        try:
            json_mode = sqlite_json_mode(con)
            if json_mode == 2:
                json_column = ", CASE WHEN json_valid(metadata, 9) THEN json(metadata) ELSE NULL END AS metadata_json"
            elif json_mode == 1:
                json_column = ", CASE WHEN json_valid(CAST(metadata AS TEXT)) THEN json(CAST(metadata AS TEXT)) ELSE NULL END AS metadata_json"
            else:
                json_column = ""
            rows = iter_rows(con, f"SELECT created_at, video_path, metadata{json_column}, local_content_id FROM contents ORDER BY created_at")
            for row in rows:
                created = parse_timestamp(row["created_at"])
                if created is None:
                    continue
                day = created.date()
                if start and day < start or end and day > end:
                    continue
                video = str(row["video_path"] or "")
                game = infer_game(video, game_rules)
                if selected and game.casefold() not in selected:
                    continue
                if game.casefold() == "valorant" and valorant_after and day < valorant_after:
                    continue
                exists = bool(video and Path(video).is_file())
                if not exists and not include_missing_video:
                    continue
                try:
                    meta = parse_metadata(row["metadata"], row["metadata_json"] if "metadata_json" in row.keys() else None)
                    meta_status = "PARSED"
                except ValueError as exc:
                    meta, meta_status = {}, f"FAILED: {exc}"
                trigger = str(meta.get("triggerType") or "")
                capture = str(meta.get("captureType") or "")
                clip_type = str(meta.get("clipType") or "")
                if triggers and trigger.casefold() not in triggers or captures and capture.casefold() not in captures:
                    continue
                duration = number(meta.get("clipDuration"))
                start_raw = nested(meta, "recorder", "clipStartTime")
                export_raw = nested(meta, "recorder", "exportStatsTimestamp")
                event_raw = nested(meta, "recorder", "eventobj", "timestamp")
                start_dt, export_dt = parse_timestamp(start_raw), parse_timestamp(export_raw)
                offset = (export_dt - start_dt).total_seconds() if start_dt and export_dt else None
                raw_marks = meta.get("bookmarks")
                bookmarks = raw_marks if isinstance(raw_marks, list) else []
                if meta_status != "PARSED": bookmarks = []
                evaluated = [evaluate_bookmark(b, duration, start_dt, export_dt, tolerance) for b in bookmarks if isinstance(b, dict)]
                if names:
                    evaluated = [x for x in evaluated if str(x["bookmark"].get("name") or "").casefold() in names]
                if not evaluated and not include_no_bookmarks:
                    continue
                counts = Counter(x["position_status"] for x in evaluated)
                clip_writer.writerow({"local_content_id": row["local_content_id"], "created_at_utc": iso(created), "game": game,
                    "video_path": video, "video_exists": exists, "clip_duration_seconds": duration or "", "trigger_type": trigger,
                    "capture_type": capture, "clip_type": clip_type, "clip_start_timestamp": iso(start_dt) or start_raw or "",
                    "export_stats_timestamp": iso(export_dt) or export_raw or "", "recorder_event_timestamp": event_raw or "",
                    "export_to_start_offset_seconds": offset if offset is not None else "",
                    "bookmark_count": len(evaluated), "in_range_count": counts["IN_RANGE"], "out_of_range_count": counts["OUT_OF_RANGE"],
                    "missing_timing_count": counts["MISSING_TIMING_DATA"], "metadata_status": meta_status})
                clips_written += 1
                report_bookmarks = evaluated if include_out_of_range else [x for x in evaluated if x["position_status"] != "OUT_OF_RANGE"]
                for item in report_bookmarks:
                    b = item["bookmark"]
                    mark_writer.writerow({"local_content_id": row["local_content_id"], "created_at_utc": iso(created), "game": game,
                        "video_path": video, "clip_duration_seconds": duration if duration is not None else "", "trigger_type": trigger,
                        "capture_type": capture, "clip_type": clip_type, "bookmark_name": b.get("name") or "",
                        "stored_bookmark_time_seconds": number(b.get("time")) if number(b.get("time")) is not None else "",
                        "bookmark_timestamp": b.get("timestamp") if b.get("timestamp") is not None else "",
                        "clip_start_timestamp": iso(start_dt) or start_raw or "", "export_stats_timestamp": iso(export_dt) or export_raw or "",
                        "recorder_event_timestamp": event_raw or "",
                        "export_to_start_offset_seconds": offset if offset is not None else "",
                        "candidate_recovered_position_seconds": fmt(item["recovered"]), "timestamp_derived_position_seconds": fmt(item["timestamp_position"]),
                        "candidate_position_difference_seconds": fmt(item["candidate_difference"]), "export_relative_seconds": fmt(item["export_relative"]),
                        "formula_consistency_difference_seconds": fmt(item["consistency_difference"]), "position_status": item["position_status"],
                        "video_exists": exists, "manual_verification_status": "UNVERIFIED", "event_name_missing": not bool(b.get("name")),
                        "unknown_game": game == "Unknown", "unknown_capture_type": capture.casefold() not in {"injectioncapture", "windowcapture"},
                        "raw_bookmark_json": json.dumps(b, ensure_ascii=False, separators=(",", ":"))})
                    bookmarks_written += 1
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not read contents: {exc}") from exc
    return clips_written, bookmarks_written


def evaluate_bookmark(bookmark: dict[str, Any], duration: float | None, start: dt.datetime | None,
                      export: dt.datetime | None, tolerance: float) -> dict[str, Any]:
    stored = number(bookmark.get("time"))
    stamp_value = number(bookmark.get("timestamp"))
    try:
        stamp = dt.datetime.fromtimestamp(stamp_value / 1000, UTC) if stamp_value is not None else None
    except (OverflowError, OSError, ValueError):
        stamp = None
    offset = (export - start).total_seconds() if export and start else None
    recovered = stored + offset if stored is not None and offset is not None else None
    timestamp_position = (stamp - start).total_seconds() if stamp and start else None
    export_relative = (stamp - export).total_seconds() if stamp and export else None
    candidate_diff = recovered - timestamp_position if recovered is not None and timestamp_position is not None else None
    consistency = stored - export_relative if stored is not None and export_relative is not None else None
    if stored is None or duration is None or duration < 0 or start is None or export is None or stamp is None:
        status = "MISSING_TIMING_DATA"
    elif recovered is None:
        status = "MISSING_TIMING_DATA"
    elif recovered < -tolerance or recovered > duration + tolerance:
        status = "OUT_OF_RANGE"
    else:
        status = "IN_RANGE"
    return {"bookmark": bookmark, "recovered": recovered, "timestamp_position": timestamp_position,
        "candidate_difference": candidate_diff, "export_relative": export_relative,
        "consistency_difference": consistency, "position_status": status}


def fmt(value: float | None) -> float | str:
    return "" if value is None else round(value, 6)


def date_arg(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    summary = subs.add_parser("summary", help="Inspect paths, dates, triggers, capture types, and bookmark counts")
    summary.add_argument("--db", required=True, type=Path)
    run = subs.add_parser("extract", help="Write clip-level and bookmark-level CSV reports")
    run.add_argument("--db", required=True, type=Path)
    run.add_argument("--start", help="Inclusive UTC calendar date, YYYY-MM-DD")
    run.add_argument("--end", help="Inclusive UTC calendar date, YYYY-MM-DD")
    run.add_argument("--games", nargs="+", help="Game names (omit to include all, including Unknown)")
    run.add_argument("--game-match", action="append", default=[], metavar="GAME=TOKEN[,TOKEN]", help="Add/replace path tokens used to infer a game")
    run.add_argument("--valorant-after", default=DEFAULT_CUTOFF.isoformat(), help="Inclusive Valorant cutoff date; use 'none' to disable")
    run.add_argument("--trigger", action="append", default=[])
    run.add_argument("--capture", action="append", default=[])
    run.add_argument("--bookmark-name", action="append", default=[])
    run.add_argument("--include-no-bookmarks", action="store_true")
    run.add_argument("--include-out-of-range", action="store_true")
    run.add_argument("--include-missing-video", action="store_true")
    run.add_argument("--tolerance", type=float, default=0.001, help="Boundary tolerance in seconds (default 0.001)")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--overwrite", action="store_true")
    run.add_argument("--preview", action="store_true", help="Count matching rows without keeping or replacing reports")
    args = parser.parse_args(argv)
    try:
        if args.command == "summary":
            inspect_db(args.db)
        else:
            cutoff = None if args.valorant_after.casefold() == "none" else date_arg(args.valorant_after)
            options = (date_arg(args.start), date_arg(args.end), args.games, cutoff,
                {x.casefold() for x in args.trigger}, {x.casefold() for x in args.capture},
                {x.casefold() for x in args.bookmark_name}, args.include_no_bookmarks,
                args.include_out_of_range, args.include_missing_video, args.tolerance)
            rules = parse_game_rules(args.game_match)
            if args.preview:
                with tempfile.TemporaryDirectory(prefix="medal-audit-preview-") as temporary:
                    clips, marks = extract(args.db, Path(temporary), *options, True, rules)
                print(f"Preview: {clips} matching clip row(s), {marks} matching bookmark row(s). No reports retained.")
            else:
                clips, marks = extract(args.db, args.output, *options, args.overwrite, rules)
                print(f"Wrote {clips} clip row(s) and {marks} bookmark row(s) to {args.output}")
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"medal_audit: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
