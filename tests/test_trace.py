"""Tests for ``POST /trace``: ordered crack-cell path with exact integer geometry.

Expectations are not hardcoded API answers: an independent per-cell
intersection reference built on :class:`fractions.Fraction` recomputes the
expected path, and randomized polylines are checked through the live HTTP API
in all four camera orientations. Fixed cases only pin down the boundary
ownership corner cases stated in the contract.
"""

from __future__ import annotations

import random
from fractions import Fraction
from typing import List, Sequence, Tuple

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

Cell = Tuple[int, int]
Point = Tuple[int, int]


# ---------------------------------------------------------------------------
# Independent reference implementation (deliberately mirrors no production code)
# ---------------------------------------------------------------------------


def reference_trace(vertices: Sequence[Point], width: int, height: int,
                    rows: int, cols: int) -> List[Cell]:
    """Trace the polyline through half-open cells using exact rationals.

    The cell owning a continuous point ``(u, v)`` is
    ``(floor(v*rows/H)+1, floor(u*cols/W)+1)``; floor at a boundary yields the
    right/lower owner, and the closed right/bottom canvas edges follow for free.

    Every segment is cut at the exact parameters where it meets internal grid
    lines. Ownership is constant on each open parameter interval (sampled at its
    midpoint), so a horizontal and vertical line met at one parameter (a grid
    corner) contributes no side cells. Every joint/endpoint additionally
    contributes its own floor-owned cell; adjacent duplicates collapse while
    non-adjacent revisits are preserved.
    """
    def cell_at(u: Fraction, v: Fraction) -> Cell:
        return int((v * rows) // height) + 1, int((u * cols) // width) + 1

    path: List[Cell] = []

    def emit(cell: Cell) -> None:
        if not path or path[-1] != cell:
            path.append(cell)

    emit(cell_at(Fraction(vertices[0][0]), Fraction(vertices[0][1])))
    for (u0, v0), (u1, v1) in zip(vertices, vertices[1:]):
        if (u0, v0) == (u1, v1):  # zero-length segment enters nothing
            continue
        pu0, pv0, pu1, pv1 = map(Fraction, (u0, v0, u1, v1))
        du, dv = pu1 - pu0, pv1 - pv0
        parameters: set[Fraction] = set()
        for k in range(1, cols):
            if du != 0:
                t = (Fraction(k * width, cols) - pu0) / du
                if 0 < t < 1:
                    parameters.add(t)
        for j in range(1, rows):
            if dv != 0:
                t = (Fraction(j * height, rows) - pv0) / dv
                if 0 < t < 1:
                    parameters.add(t)
        cuts = [Fraction(0), *sorted(parameters), Fraction(1)]
        for left, right in zip(cuts, cuts[1:]):
            mid = (left + right) / 2
            emit(cell_at(pu0 + du * mid, pv0 + dv * mid))
        emit(cell_at(pu1, pv1))
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def trace(vertices, rotation=0, width=600, height=400, rows=4, cols=10):
    return client.post(
        "/trace",
        json={
            "width": width,
            "height": height,
            "rows": rows,
            "cols": cols,
            "rotation": rotation,
            "vertices": vertices,
        },
    )


def to_raw(u: int, v: int, rotation: int, width: int, height: int) -> Point:
    """Inverse of transform.normalize_point: normalized pixel -> raw pixel."""
    if rotation == 0:
        return u, v
    if rotation == 90:  # u = height-1-y, v = x
        return v, height - 1 - u
    if rotation == 180:
        return width - 1 - u, height - 1 - v
    if rotation == 270:  # u = y, v = width-1-x
        return width - 1 - v, u
    raise AssertionError(rotation)


def path_of(response) -> List[Cell]:
    return [(step["row"], step["col"]) for step in response.json()["path"]]


# ---------------------------------------------------------------------------
# Fixed contract cases
# ---------------------------------------------------------------------------


def test_single_vertex_returns_owned_cell():
    resp = trace([{"x": 60, "y": 100}])  # sits on an internal grid corner
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vertices"] == [{"index": 0, "x": 60, "y": 100}]
    assert path_of(resp) == [(2, 2)]  # boundary pixel -> lower-right owner


def test_duplicate_vertices_are_zero_length_and_add_no_records():
    resp = trace([{"x": 60, "y": 100}, {"x": 60, "y": 100}, {"x": 60, "y": 100}])
    assert path_of(resp) == [(2, 2)]


def test_corner_crossing_enters_diagonal_cell_directly():
    # 6x4 square-ish grid on 600x400: cell 100x100, corner at (100, 100).
    resp = trace([{"x": 50, "y": 50}, {"x": 150, "y": 150}], rows=4, cols=6)
    assert path_of(resp) == [(1, 1), (2, 2)]  # side cells (1,2)/(2,1) skipped


def test_corner_crossing_reverse_direction():
    resp = trace([{"x": 150, "y": 150}, {"x": 50, "y": 50}], rows=4, cols=6)
    assert path_of(resp) == [(2, 2), (1, 1)]


def test_segment_running_along_vertical_grid_line_owned_by_right_cell():
    resp = trace([{"x": 100, "y": 50}, {"x": 100, "y": 350}], rows=4, cols=6)
    assert path_of(resp) == [(1, 2), (2, 2), (3, 2), (4, 2)]


def test_segment_running_along_horizontal_grid_line_owned_by_lower_cell():
    resp = trace([{"x": 50, "y": 100}, {"x": 550, "y": 100}], rows=4, cols=6)
    assert path_of(resp) == [(2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (2, 6)]


def test_backtrack_revisit_is_kept_but_adjacent_duplicates_collapse():
    resp = trace([{"x": 50, "y": 50}, {"x": 150, "y": 150}, {"x": 50, "y": 50}],
                 rows=4, cols=6)
    assert path_of(resp) == [(1, 1), (2, 2), (1, 1)]  # non-adjacent revisit kept


def test_joint_on_grid_corner_uses_owned_cell_between_segments():
    # arrive at corner (100,100) diagonally, leave straight up along x=100
    resp = trace([{"x": 50, "y": 50}, {"x": 100, "y": 100}, {"x": 100, "y": 50}],
                 rows=4, cols=6)
    assert path_of(resp) == [(1, 1), (2, 2), (1, 2)]


def test_uneven_grid_crossing():
    # width=10, cols=3 -> internal lines at 10/3 and 20/3 (fractional pixels).
    resp = trace([{"x": 0, "y": 0}, {"x": 9, "y": 9}], width=10, height=10,
                 rows=3, cols=3)
    assert path_of(resp) == [(1, 1), (2, 2), (3, 3)]


def test_response_envelope_and_normalized_vertices():
    resp = trace(
        [{"x": 0, "y": 0}, {"x": 599, "y": 399}],
        rotation=270,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"rotation", "canvas", "grid", "vertices", "path"}
    assert body["rotation"] == 270
    assert body["canvas"] == {"width": 400, "height": 600}
    assert body["grid"] == {"rows": 4, "cols": 10}
    assert body["vertices"] == [
        {"index": 0, "x": 0, "y": 599},
        {"index": 1, "x": 399, "y": 0},
    ]
    for step in body["path"]:
        assert set(step) == {"row", "col"}
        assert 1 <= step["row"] <= 4 and 1 <= step["col"] <= 10


# ---------------------------------------------------------------------------
# Four-orientation equivalence vs. the independent reference
# ---------------------------------------------------------------------------


def _rotated_request(norm_points: Sequence[Point], rotation: int,
                     size: int, rows: int, cols: int):
    """POST normalized-space points on a square ``size`` canvas in one rotation.

    A square raw canvas keeps the normalized canvas identical for every
    orientation, which is what makes the four paths directly comparable.
    """
    raw = [
        {"x": x, "y": y}
        for (x, y) in (to_raw(u, v, rotation, size, size) for u, v in norm_points)
    ]
    return trace(raw, rotation=rotation, width=size, height=size,
                 rows=rows, cols=cols)


@pytest.mark.parametrize("seed", range(24))
def test_random_polylines_match_reference_in_all_rotations(seed):
    rng = random.Random(seed)
    # Square, often non-divisible sizes (7, 13 do not divide 400 etc.).
    size = rng.choice([1, 2, 3, 7, 13, 50, 100, 400])
    rows = rng.randint(1, 6)
    cols = rng.randint(1, 6)
    n = rng.randint(1, 7)
    norm_points = [
        (rng.randint(0, size - 1), rng.randint(0, size - 1)) for _ in range(n)
    ]
    # Sprinkle in duplicates so zero-length segments are exercised too.
    if len(norm_points) >= 2:
        norm_points.insert(rng.randint(1, len(norm_points) - 1), norm_points[0])

    expected = reference_trace(norm_points, size, size, rows, cols)
    paths = {}
    for rotation in (0, 90, 180, 270):
        resp = _rotated_request(norm_points, rotation, size, rows, cols)
        assert resp.status_code == 200, (rotation, resp.text)
        body = resp.json()
        assert [(p["x"], p["y"]) for p in body["vertices"]] == list(norm_points)
        assert (body["canvas"]["width"], body["canvas"]["height"]) == (size, size)
        paths[rotation] = path_of(resp)

    assert paths[0] == paths[90] == paths[180] == paths[270] == expected


@pytest.mark.parametrize("seed", range(24))
def test_random_rectangular_canvases_match_reference(seed):
    rng = random.Random(1000 + seed)
    width = rng.choice([3, 7, 13, 100, 400])
    height = rng.choice([2, 5, 11, 300, 400])
    rows = rng.randint(1, 6)
    cols = rng.randint(1, 6)
    n = rng.randint(1, 7)
    norm_points = [
        (rng.randint(0, width - 1), rng.randint(0, height - 1)) for _ in range(n)
    ]
    resp = trace(
        [{"x": u, "y": v} for u, v in norm_points],
        width=width, height=height, rows=rows, cols=cols,
    )
    assert resp.status_code == 200, resp.text
    assert path_of(resp) == reference_trace(norm_points, width, height, rows, cols)


def test_dense_small_canvas_matches_reference():
    # Exhaustive two-point segments on a small uneven grid.
    width, height, rows, cols = 5, 7, 3, 4
    for u0 in range(width):
        for v0 in range(height):
            for u1 in range(width):
                for v1 in range(height):
                    verts = [{"x": u0, "y": v0}, {"x": u1, "y": v1}]
                    resp = trace(verts, width=width, height=height,
                                 rows=rows, cols=cols)
                    assert path_of(resp) == reference_trace(
                        [(u0, v0), (u1, v1)], width, height, rows, cols
                    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_multiple_illegal_vertices_all_reported_by_position():
    vertices = [
        {"x": 600, "y": 0},    # 0: x == width
        {"x": 1.5, "y": 0},   # 1: fractional
        {"x": 0, "y": 400},   # 2: y == height
        {"x": "1", "y": 0},   # 3: string
        {"x": 0, "y": 0},     # 4: legal
    ]
    resp = trace(vertices)
    assert resp.status_code == 422
    body = resp.json()
    assert "path" not in body and "vertices" not in body  # no partial results
    assert sorted(e["index"] for e in body["detail"]) == [0, 1, 2, 3]
    types = {e["index"]: e["type"] for e in body["detail"]}
    assert types[0] == "vertex_out_of_bounds"
    assert types[1] == "int_type"
    assert types[2] == "vertex_out_of_bounds"
    assert types[3] == "int_type"


def test_illegal_middle_vertex_rejects_batch_with_index():
    vertices = [{"x": 1, "y": 1}, {"x": 10, "y": 1}, {"x": 5, "y": 5}]
    resp = trace(vertices, width=10, height=10, rows=2, cols=2)
    assert resp.status_code == 422
    assert any(e.get("index") == 1 for e in resp.json()["detail"])


@pytest.mark.parametrize(
    "bad,index",
    [
        ({"x": 1.5, "y": 0}, 0),
        ({"x": "1", "y": 0}, 0),
        ({"x": True, "y": 0}, 0),
        ({"y": 1}, 0),
        ({"x": 1}, 0),
        (None, 0),
        ("nope", 0),
        (42, 0),
        ({"x": -1, "y": 0}, 1),
    ],
)
def test_illegal_vertex_type_reports_its_index(bad, index):
    vertices = [{"x": 0, "y": 0}, bad] if index == 1 else [bad]
    resp = trace(vertices, width=10, height=10, rows=2, cols=2)
    assert resp.status_code == 422
    assert any(e.get("index") == index for e in resp.json()["detail"])


def test_empty_vertices_rejected():
    resp = trace([])
    assert resp.status_code == 422


def test_shared_canvas_fields_still_validated():
    for override in (
        {"width": 0}, {"height": -1}, {"rows": 0}, {"cols": -2},
        {"rotation": 45},
    ):
        resp = trace([{"x": 0, "y": 0}], **override)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_inspect_contract_unchanged():
    resp = client.post(
        "/inspect",
        json={
            "width": 600, "height": 400, "rows": 4, "cols": 10, "rotation": 90,
            "points": [{"x": 0, "y": 0}, {"x": 599, "y": 399}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["canvas"] == {"width": 400, "height": 600}
    assert body["results"][0] == {"index": 0, "x": 399, "y": 0, "row": 1, "col": 10}
    assert body["results"][1] == {"index": 1, "x": 0, "y": 599, "row": 4, "col": 1}


def test_health_unchanged():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
