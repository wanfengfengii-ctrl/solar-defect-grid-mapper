"""One-shot acceptance suite for the EL cell-locator API.

Runs as the ``verify`` service in docker-compose.yml: it waits for the API to
become healthy, executes real HTTP checks covering all four camera rotations,
grid-line boundary ownership, ordering and batch rejection, then exits 0 when
every check passed and 1 otherwise.
"""

from __future__ import annotations

import os
import sys
import time
from fractions import Fraction
from typing import Any, Dict, List, Sequence, Tuple

import httpx

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
STARTUP_TIMEOUT = float(os.environ.get("VERIFY_STARTUP_TIMEOUT", "60"))

WIDTH, HEIGHT, ROWS, COLS = 600, 400, 4, 10

passed = 0
failed: List[str] = []


def wait_for_api() -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while True:
        try:
            resp = httpx.get(f"{BASE_URL}/health", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            print(f"FAIL  api not healthy at {BASE_URL} within {STARTUP_TIMEOUT:.0f}s")
            sys.exit(1)
        time.sleep(1.0)


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed.append(name)
        suffix = f"  -- {detail}" if detail else ""
        print(f"FAIL  {name}{suffix}")


def inspect(points: List[Dict[str, int]], rotation: int = 0, **overrides: Any) -> httpx.Response:
    payload: Dict[str, Any] = {
        "width": WIDTH,
        "height": HEIGHT,
        "rows": ROWS,
        "cols": COLS,
        "rotation": rotation,
        "points": points,
    }
    payload.update(overrides)
    return httpx.post(f"{BASE_URL}/inspect", json=payload, timeout=10.0)


# ---------------------------------------------------------------------------
# /trace: ordered crack-cell path
# ---------------------------------------------------------------------------


def reference_trace(vertices: Sequence[Tuple[int, int]], width: int, height: int,
                    rows: int, cols: int) -> List[Tuple[int, int]]:
    """Independent per-cell intersection reference (exact rationals).

    Cell of a continuous point is (floor(v*rows/H)+1, floor(u*cols/W)+1); each
    segment is cut at every exact grid-line parameter and cells are sampled on
    the open intervals (so a corner crossing has no side cells), while every
    joint/endpoint contributes its own floor-owned cell. Nothing here is shared
    with the production implementation.
    """
    def cell_at(u: Fraction, v: Fraction) -> Tuple[int, int]:
        return int((v * rows) // height) + 1, int((u * cols) // width) + 1

    path: List[Tuple[int, int]] = []

    def emit(cell: Tuple[int, int]) -> None:
        if not path or path[-1] != cell:
            path.append(cell)

    emit(cell_at(Fraction(vertices[0][0]), Fraction(vertices[0][1])))
    for (u0, v0), (u1, v1) in zip(vertices, vertices[1:]):
        if (u0, v0) == (u1, v1):
            continue
        pu0, pv0, pu1, pv1 = map(Fraction, (u0, v0, u1, v1))
        du, dv = pu1 - pu0, pv1 - pv0
        params: set[Fraction] = set()
        for k in range(1, cols):
            if du != 0:
                t = (Fraction(k * width, cols) - pu0) / du
                if 0 < t < 1:
                    params.add(t)
        for j in range(1, rows):
            if dv != 0:
                t = (Fraction(j * height, rows) - pv0) / dv
                if 0 < t < 1:
                    params.add(t)
        cuts = [Fraction(0), *sorted(params), Fraction(1)]
        for left, right in zip(cuts, cuts[1:]):
            mid = (left + right) / 2
            emit(cell_at(pu0 + du * mid, pv0 + dv * mid))
        emit(cell_at(pu1, pv1))
    return path


def trace(vertices: List[Dict[str, int]], rotation: int = 0,
          **overrides: Any) -> httpx.Response:
    payload: Dict[str, Any] = {
        "width": WIDTH,
        "height": HEIGHT,
        "rows": ROWS,
        "cols": COLS,
        "rotation": rotation,
        "vertices": vertices,
    }
    payload.update(overrides)
    return httpx.post(f"{BASE_URL}/trace", json=payload, timeout=10.0)


def to_raw(u: int, v: int, rotation: int, width: int, height: int) -> Tuple[int, int]:
    """Inverse of the documented normalization: upright pixel -> raw pixel."""
    if rotation == 0:
        return u, v
    if rotation == 90:
        return v, height - 1 - u
    if rotation == 180:
        return width - 1 - u, height - 1 - v
    return width - 1 - v, u  # 270


def check_trace_fixed_cases() -> None:
    # Square cell grid (cols=6 on width=600 -> 100x100 cells, corner 100,100).
    corner = trace([{"x": 50, "y": 50}, {"x": 150, "y": 150}], cols=6)
    check("corner crossing enters diagonal directly",
          [(s["row"], s["col"]) for s in corner.json()["path"]] == [(1, 1), (2, 2)],
          corner.text)
    along_v = trace([{"x": 100, "y": 50}, {"x": 100, "y": 350}], cols=6)
    check("segment along vertical line owned by right cell",
          [(s["row"], s["col"]) for s in along_v.json()["path"]]
          == [(1, 2), (2, 2), (3, 2), (4, 2)], along_v.text)
    along_h = trace([{"x": 50, "y": 100}, {"x": 550, "y": 100}], cols=6)
    check("segment along horizontal line owned by lower cell",
          [(s["row"], s["col"]) for s in along_h.json()["path"]]
          == [(2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (2, 6)], along_h.text)
    back = trace([{"x": 50, "y": 50}, {"x": 150, "y": 150}, {"x": 50, "y": 50}], cols=6)
    check("backtrack revisit kept, adjacent duplicates collapsed",
          [(s["row"], s["col"]) for s in back.json()["path"]]
          == [(1, 1), (2, 2), (1, 1)], back.text)
    single = trace([{"x": 60, "y": 100}])
    check("single vertex on a grid line returns its owned cell",
          [(s["row"], s["col"]) for s in single.json()["path"]] == [(2, 2)],
          single.text)
    dups = trace([{"x": 60, "y": 100}, {"x": 60, "y": 100}, {"x": 60, "y": 100}])
    check("duplicate vertices create no extra records",
          [(s["row"], s["col"]) for s in dups.json()["path"]] == [(2, 2)], dups.text)


def check_trace_rotation_equivalence() -> None:
    # Square raw canvas so the upright canvas (and grid) is identical in all
    # four orientations; the same upright polyline must give the same path.
    size = 400
    rows, cols = 7, 5
    upright = [(10, 10), (67, 100), (300, 250), (399, 399), (67, 100), (10, 10),
               (200, 50), (50, 350)]
    expected = reference_trace(upright, size, size, rows, cols)
    paths: Dict[int, List[Tuple[int, int]]] = {}
    for rotation in (0, 90, 180, 270):
        raw = [{"x": x, "y": y}
               for (x, y) in (to_raw(u, v, rotation, size, size) for u, v in upright)]
        resp = trace(raw, rotation=rotation, width=size, height=size,
                     rows=rows, cols=cols)
        if resp.status_code != 200:
            check(f"trace rotation {rotation} accepted", False, resp.text[:160])
            continue
        body = resp.json()
        check(f"trace rotation {rotation} normalizes vertices identically",
              [(p["x"], p["y"]) for p in body["vertices"]] == upright)
        paths[rotation] = [(s["row"], s["col"]) for s in body["path"]]
    check("four orientations yield one equivalent path matching the reference",
          paths.get(0) == paths.get(90) == paths.get(180) == paths.get(270)
          == expected, f"got {paths.get(0)} expected {expected}")


def check_trace_uneven_grid_vs_reference() -> None:
    # Non-divisible 3x3 grid over a 10x10 canvas: lines at 10/3 and 20/3.
    upright = [(0, 0), (4, 9), (9, 3), (0, 0), (9, 9)]
    resp = trace([{"x": u, "y": v} for u, v in upright],
                 width=10, height=10, rows=3, cols=3)
    got = [(s["row"], s["col"]) for s in resp.json()["path"]]
    check("non-divisible grid matches per-cell reference",
          got == reference_trace(upright, 10, 10, 3, 3), f"got {got}")


def check_trace_rejection() -> None:
    bad = [
        {"x": 600, "y": 0},   # 0 out of bounds
        {"x": 1.5, "y": 0},   # 1 fractional
        {"x": 0, "y": 400},   # 2 out of bounds
        {"x": "1", "y": 0},   # 3 string
        {"x": 0, "y": 0},     # 4 legal
    ]
    resp = trace(bad)
    body = resp.json()
    check("multiple illegal vertices -> 422", resp.status_code == 422,
          f"status {resp.status_code}")
    check("all offending positions returned",
          sorted(e.get("index") for e in body.get("detail", [])
                 if e.get("index") is not None) == [0, 1, 2, 3],
          str(body.get("detail")))
    check("rejected trace leaks no partial result",
          "path" not in body and "vertices" not in body)
    mid = trace([{"x": 1, "y": 1}, {"x": 10, "y": 1}, {"x": 5, "y": 5}],
                width=10, height=10, rows=2, cols=2)
    check("illegal middle vertex -> 422 with its index",
          mid.status_code == 422
          and any(e.get("index") == 1 for e in mid.json().get("detail", [])),
          mid.text)
    check("empty vertices -> 422", trace([]).status_code == 422)


def check_rotation_table() -> None:
    # (rotation, raw point, normalized point, row, col) on the 600x400 / 4x10 grid
    table: List[Tuple[int, Tuple[int, int], Tuple[int, int], int, int]] = [
        (0, (0, 0), (0, 0), 1, 1),
        (0, (60, 100), (60, 100), 2, 2),
        (0, (599, 399), (599, 399), 4, 10),
        (90, (0, 0), (399, 0), 1, 10),
        (90, (599, 399), (0, 599), 4, 1),
        (90, (150, 359), (40, 150), 2, 2),
        (180, (0, 0), (599, 399), 4, 10),
        (180, (599, 399), (0, 0), 1, 1),
        (180, (539, 299), (60, 100), 2, 2),
        (270, (0, 0), (0, 599), 4, 1),
        (270, (599, 399), (399, 0), 1, 10),
        (270, (449, 40), (40, 150), 2, 2),
    ]
    for rotation, (x, y), (ex, ey), erow, ecol in table:
        resp = inspect([{"x": x, "y": y}], rotation=rotation)
        name = f"rotation {rotation}: ({x},{y}) -> ({ex},{ey}) cell ({erow},{ecol})"
        if resp.status_code != 200:
            check(name, False, f"status {resp.status_code}: {resp.text[:120]}")
            continue
        (result,) = resp.json()["results"]
        got = (result["x"], result["y"], result["row"], result["col"])
        check(name, got == (ex, ey, erow, ecol), f"got {got}")


def check_canvas_swap() -> None:
    for rotation, expected in [(0, (600, 400)), (90, (400, 600)), (180, (600, 400)), (270, (400, 600))]:
        resp = inspect([{"x": 0, "y": 0}], rotation=rotation)
        canvas = resp.json().get("canvas", {})
        got = (canvas.get("width"), canvas.get("height"))
        check(f"rotation {rotation}: normalized canvas {expected}", got == expected, f"got {got}")


def check_boundaries() -> None:
    # Vertical grid lines at x = 60k belong to the right-hand cell.
    for k in (1, 5, 9):
        col_on = inspect([{"x": 60 * k, "y": 0}]).json()["results"][0]["col"]
        col_left = inspect([{"x": 60 * k - 1, "y": 0}]).json()["results"][0]["col"]
        check(f"grid line x={60 * k} -> right cell", (col_on, col_left) == (k + 1, k),
              f"got on={col_on} left={col_left}")
    # Horizontal grid lines at y = 100k belong to the bottom cell.
    for k in (1, 2, 3):
        row_on = inspect([{"x": 0, "y": 100 * k}]).json()["results"][0]["row"]
        row_above = inspect([{"x": 0, "y": 100 * k - 1}]).json()["results"][0]["row"]
        check(f"grid line y={100 * k} -> bottom cell", (row_on, row_above) == (k + 1, k),
              f"got on={row_on} above={row_above}")


def check_ordering() -> None:
    points = [{"x": 599, "y": 399}, {"x": 0, "y": 0}, {"x": 300, "y": 200}, {"x": 60, "y": 100}]
    resp = inspect(points, rotation=90)
    results = resp.json()["results"]
    check(
        "results preserve input order",
        [r["index"] for r in results] == [0, 1, 2, 3],
        f"got {[r['index'] for r in results]}",
    )


def check_batch_rejection() -> None:
    resp = inspect([{"x": 0, "y": 0}, {"x": WIDTH, "y": 5}, {"x": 1, "y": 1}])
    body = resp.json()
    check("out-of-bounds batch -> 422", resp.status_code == 422, f"status {resp.status_code}")
    check("rejected batch leaks no partial results", "results" not in body)
    detail = body.get("detail", [])
    check(
        "422 names the offending array index",
        any(err.get("index") == 1 for err in detail),
        f"detail={detail}",
    )
    check("invalid rotation -> 422", inspect([{"x": 0, "y": 0}], rotation=45).status_code == 422)
    check("empty points -> 422", inspect([]).status_code == 422)
    check("non-positive width -> 422", inspect([{"x": 0, "y": 0}], width=0).status_code == 422)
    check("fractional pixel -> 422", inspect([{"x": 1.5, "y": 0}]).status_code == 422)


def main() -> int:
    wait_for_api()
    check_rotation_table()
    check_canvas_swap()
    check_boundaries()
    check_ordering()
    check_batch_rejection()
    check_trace_fixed_cases()
    check_trace_rotation_equivalence()
    check_trace_uneven_grid_vs_reference()
    check_trace_rejection()
    total = passed + len(failed)
    print(f"\n{passed}/{total} acceptance checks passed against {BASE_URL}")
    if failed:
        print("failed checks:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("ACCEPTANCE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
