import argparse
import json
import os
from pathlib import Path

import jedi


def list_json_files(folder_path):
    python_files = sorted(Path(folder_path).rglob("*.json"))
    return python_files


def build_position_map(source_path):
    """Map (name, line_number) -> 1-indexed col_offset for every definition
    and reference in the source. Scalpel's runner doesn't emit col_offset, so
    we recover it by parsing the source with Jedi."""
    positions = {}
    try:
        script = jedi.Script(path=str(source_path))
        for n in script.get_names(all_scopes=True, definitions=True, references=True):
            positions.setdefault((n.name, n.line), n.column + 1)
    except Exception:
        pass
    return positions


def _lookup_name(entry):
    """Return the source-level name to look up for this entry's position."""
    if "variable" in entry:
        name = entry["variable"]
        for sep in ("[", "."):
            if sep in name:
                name = name.split(sep, 1)[0]
                break
        return name
    if "parameter" in entry:
        return entry["parameter"]
    if "function" in entry:
        return entry["function"].rsplit(".", 1)[-1]
    return None


def enrich_with_col_offsets(source_path, entries):
    """Augment entries with col_offset by looking up the position of each
    entry's identifying name in the source file."""
    positions = build_position_map(source_path)
    for entry in entries:
        if "col_offset" in entry:
            continue
        name = _lookup_name(entry)
        if name is None:
            continue
        col = positions.get((name, entry["line_number"]))
        if col is not None:
            entry["col_offset"] = col
    return entries


def main_translator(args):
    json_files = list_json_files(args.bechmark_path)
    error_count = 0
    for file in json_files:
        try:
            # Run the inference here and gather results in /tmp/results
            pass

        except Exception as e:
            print(f"Command returned non-zero exit status: {e} for file: {file}")
            error_count += 1

    print(f"Runner finished with errors:{error_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bechmark_path",
        help="Specify the benchmark path",
        default="/tmp/micro-benchmark",
    )

    args = parser.parse_args()
    main_translator(args)
