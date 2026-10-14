# Medal Bookmark Auditor

A small, read-only Python CLI for inspecting Medal's local SQLite database and exporting clip and bookmark metadata for review. It compares a candidate bookmark-position formula with timestamp-derived positions while preserving the original metadata for auditing.

## Requirements

- Python 3.11 or newer
- No third-party runtime dependencies
- A local Medal SQLite database (a copy is recommended)

## Install

From the project directory:

```text
python -m pip install .
```

This installs the `medal-audit` command. You can also run the source directly with `python medal_audit.py`.

## Inspect the database

```text
medal-audit summary --db "C:\\path\\to\\copy\\medal.db"
```

The summary reports creation-date coverage, video paths, trigger types, capture types, and bookmark totals. Use the paths to learn how games are represented on your computer. By default the tool looks for common game-name tokens and keeps unrecognized paths as `Unknown` when no game filter is given.

To customize a path match, repeat `--game-match`:

```text
medal-audit summary --db "C:\\path\\to\\copy\\medal.db"
medal-audit extract --db "C:\\path\\to\\copy\\medal.db" --game-match "Counter-Strike 2=CS2,Counter-Strike 2" --games "Counter-Strike 2" --output .\\reports
```

`CS2` is accepted as an alias for `Counter-Strike 2` in `--games`.

## Preview and export

Preview the number of records a filter selects:

```text
medal-audit extract --db "C:\\path\\to\\copy\\medal.db" --start 2026-08-01 --end 2026-10-21 --games Valorant "Counter-Strike 2" Roblox Tekken --output .\\reports --preview
```

Then run the same command without `--preview` to write `clips.csv` and `bookmarks.csv` into the output folder. Start and end dates are inclusive UTC calendar dates. Valorant has an independent default cutoff of August 15, 2026 inclusive. Override it with `--valorant-after YYYY-MM-DD`, or disable it with `--valorant-after none`; it does not limit the other games.

Useful optional filters include `--trigger ICYMI`, `--capture InjectionCapture`, and `--bookmark-name 4K`. Repeat them to select multiple values. With no `--games` filter, all inferred games and `Unknown` are eligible.

By default, clips with no matching bookmarks, out-of-range bookmark detail rows, and missing video files are omitted. Use `--include-no-bookmarks`, `--include-out-of-range`, or `--include-missing-video` to include them. Clip-level counts still show out-of-range bookmarks when those detail rows are omitted. Existing reports are not overwritten unless you add `--overwrite`.

## Position calculations

```text
export_to_start_offset = exportStatsTimestamp - clipStartTime
candidate_recovered_position = bookmark.time + export_to_start_offset
timestamp_derived_position = bookmark.timestamp / 1000 - clipStartTime
export_relative_position = bookmark.timestamp / 1000 - exportStatsTimestamp
formula_consistency_difference = bookmark.time - export_relative_position
```

The timestamps are parsed before subtraction. Epoch values are interpreted as seconds or milliseconds based on magnitude; timestamp strings without a timezone are treated as UTC. The candidate position is numerical evidence, not proof of the bookmark's true location. All exported bookmarks are marked `UNVERIFIED` until checked against the actual clip. `IN_RANGE`, `OUT_OF_RANGE`, and `MISSING_TIMING_DATA` describe calculation/range status separately from manual verification. The default boundary tolerance is one millisecond and can be changed with `--tolerance`.

## Reports

- `clips.csv` has one row per selected clip, including UTC creation time, inferred game, video existence, trigger/capture/clip types, timing fields, bookmark counts, and metadata parsing status.
- `bookmarks.csv` has one row per selected bookmark, including original timestamp values, both candidate positions, comparison differences, verification state, and the raw bookmark JSON.

The `examples/` folder contains sample CSVs generated from synthetic records. Paths there are examples only; no real user database or video is included.

## Tests

```text
python -m unittest discover -s tests -v
```

The tests use a temporary synthetic SQLite database. They cover formula arithmetic, negative bookmark times, clip boundaries, missing timing fields, precision, malformed metadata, date filters, game cutoff behavior, and missing video files. No private Medal data is required.

## Database safety and limitations

The database is opened with SQLite read-only mode and `query_only`; the tool performs reads only. Where supported, SQLite's `json_valid` and `json` functions normalize JSON text or JSONB BLOB metadata before Python parses it, with direct byte decoding as a fallback. If Medal has the database locked, retry against a copy. Video files are only checked for existence and are never opened. Game detection is path-token matching and may require a local `--game-match` rule.

The `unknown_capture_type` flag compares values with the observed `InjectionCapture` and `WindowCapture` examples. Medal may have additional values; treat the flag as a review cue, not an error.

This project is licensed under the MIT License. The copyright notice uses the project author's chosen handle.
