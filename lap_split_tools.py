from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Dict


def _load_lap_utils():
    here = Path(__file__).resolve().parent
    lap_utils_path = here / "lap_split_tools" / "lap_utils.py"
    if not lap_utils_path.exists():
        raise FileNotFoundError(f"lap_utils.py not found: {lap_utils_path}")

    spec = importlib.util.spec_from_file_location("openlap_lap_utils", str(lap_utils_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to create import spec for lap_utils.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _line_from_track_json(json_path: str) -> Dict[str, Dict[str, float]]:
    with open(json_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    label_info = cfg.get("label_info", {}) or {}
    direction = label_info.get("direction", "CCW")
    line_lonlat = label_info.get("line_lonlat", [])
    if not isinstance(line_lonlat, list) or len(line_lonlat) < 2:
        raise ValueError("Invalid JSON: label_info.line_lonlat requires 2 points")

    p1 = line_lonlat[0]
    p2 = line_lonlat[1]
    if len(p1) < 2 or len(p2) < 2:
        raise ValueError("Invalid JSON: line_lonlat points must contain [lon, lat]")
    return {
        "direction": direction,
        "line": {
            "p1": {"lon": float(p1[0]), "lat": float(p1[1])},
            "p2": {"lon": float(p2[0]), "lat": float(p2[1])},
        },
    }


def auto_split_folder_with_track_json(
    json_path: str,
    input_dir: str,
    output_dir: str,
) -> dict:
    lap_utils = _load_lap_utils()
    line_cfg = _line_from_track_json(json_path)
    start_line = line_cfg["line"]
    direction = line_cfg["direction"]

    in_dir = Path(input_dir).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    processed = []
    skipped = []
    for entry in sorted(in_dir.iterdir()):
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        out_path = out_dir / entry.name
        try:
            if ext == ".gpx":
                points, tree, ns = lap_utils.load_gpx(str(entry))
                points = lap_utils.split_into_laps(points, start_line["p1"], start_line["p2"], direction)
                lap_utils.save_gpx(str(out_path), tree, points, ns)
                processed.append(str(out_path))
            elif ext == ".vbo":
                points, column_names, header_lines = lap_utils.load_vbo(str(entry))
                points = lap_utils.split_into_laps(points, start_line["p1"], start_line["p2"], direction)
                lap_utils.save_vbo(str(out_path), points, column_names, header_lines)
                processed.append(str(out_path))
            else:
                skipped.append(str(entry))
        except Exception:
            skipped.append(str(entry))

    return {
        "processed": processed,
        "skipped": skipped,
        "json_path": str(Path(json_path).resolve()),
        "input_dir": str(in_dir),
        "output_dir": str(out_dir),
        "direction": direction,
    }


def auto_split_file_with_track_json(
    json_path: str,
    input_file: str,
    output_file: str | None = None,
) -> dict:
    lap_utils = _load_lap_utils()
    line_cfg = _line_from_track_json(json_path)
    start_line = line_cfg["line"]
    direction = line_cfg["direction"]

    src = Path(input_file).resolve()
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")
    ext = src.suffix.lower()
    if ext not in {".gpx", ".vbo"}:
        raise ValueError("Only .gpx/.vbo files support lap splitting")

    dst = Path(output_file).resolve() if output_file else src

    if ext == ".gpx":
        points, tree, ns = lap_utils.load_gpx(str(src))
        points = lap_utils.split_into_laps(points, start_line["p1"], start_line["p2"], direction)
        lap_utils.save_gpx(str(dst), tree, points, ns)
    else:
        points, column_names, header_lines = lap_utils.load_vbo(str(src))
        points = lap_utils.split_into_laps(points, start_line["p1"], start_line["p2"], direction)
        lap_utils.save_vbo(str(dst), points, column_names, header_lines)

    return {
        "processed": [str(dst)],
        "skipped": [],
        "json_path": str(Path(json_path).resolve()),
        "input_file": str(src),
        "output_file": str(dst),
        "direction": direction,
    }


def launch_gui_split() -> dict:
    import subprocess
    import sys

    here = Path(__file__).resolve().parent
    gui_path = here / "lap_split_tools" / "gui_split.py"
    if not gui_path.exists():
        raise FileNotFoundError(f"gui_split.py not found: {gui_path}")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(gui_path.parent) + os.pathsep + env.get("PYTHONPATH", "")

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [sys.executable, str(gui_path)],
        cwd=str(gui_path.parent),
        env=env,
        creationflags=creationflags,
    )
    return {"started": True, "pid": proc.pid, "script": str(gui_path)}
