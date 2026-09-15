"""Pure integer geometry for EL image normalization and cell lookup.

The EL camera may be mounted in four orientations on the production line, so a
raw image can be rotated 0/90/180/270 degrees clockwise relative to the upright
module. All functions here operate on discrete pixel indices with exact integer
arithmetic; the origin is the top-left corner of the image.

Cell regions follow a half-open convention: a cell owns its left/top grid line
while the canvas' rightmost and bottommost edges are closed, so a pixel sitting
exactly on an internal grid line belongs to the cell on its right / below it.
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import List, Sequence, Tuple

VALID_ROTATIONS = (0, 90, 180, 270)

# Event tuple: (parameter numerator, parameter denominator, axis, cell step).
# The denominator is always positive; axis is one of _ROW / _COL.
ROW = 0  # crossing a horizontal grid line changes the row
COL = 1  # crossing a vertical grid line changes the column


def normalize_point(x: int, y: int, width: int, height: int, rotation: int) -> tuple[int, int]:
    """Map a raw camera pixel ``(x, y)`` to the upright (normalized) canvas.

    Clockwise mapping rules (origin top-left):

    * 0:   ``(x, y)``
    * 90:  ``(height - 1 - y, x)``
    * 180: ``(width - 1 - x, height - 1 - y)``
    * 270: ``(y, width - 1 - x)``
    """
    if rotation == 0:
        return x, y
    if rotation == 90:
        return height - 1 - y, x
    if rotation == 180:
        return width - 1 - x, height - 1 - y
    if rotation == 270:
        return y, width - 1 - x
    raise ValueError(f"unsupported rotation: {rotation!r} (expected one of {VALID_ROTATIONS})")


def normalized_canvas(width: int, height: int, rotation: int) -> tuple[int, int]:
    """Canvas ``(width, height)`` after normalization.

    90 and 270 degrees swap the axes; 0 and 180 keep them.
    """
    if rotation in (90, 270):
        return height, width
    if rotation in (0, 180):
        return width, height
    raise ValueError(f"unsupported rotation: {rotation!r} (expected one of {VALID_ROTATIONS})")


def locate_cell(u: int, v: int, canvas_width: int, canvas_height: int, rows: int, cols: int) -> tuple[int, int]:
    """1-based ``(row, col)`` of the cell containing normalized pixel ``(u, v)``.

    ``row = floor(v * rows / canvas_height) + 1`` and likewise for ``col``.
    A pixel exactly on a grid line therefore belongs to the cell on the
    right / bottom side of the line, as required by the QC workflow.
    """
    row = (v * rows) // canvas_height + 1
    col = (u * cols) // canvas_width + 1
    return row, col


def _compare_crossings(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> int:
    """Order two crossing parameters ``num/den`` by integer cross multiplication.

    No floating-point division is performed, so non-divisible grid layouts keep
    a total, rounding-free order. Both denominators are strictly positive.
    """
    lhs = left[0] * right[1]
    rhs = right[0] * left[1]
    if lhs < rhs:
        return -1
    if lhs > rhs:
        return 1
    return 0


def _same_parameter(event: tuple[int, int, int, int], num: int, den: int) -> bool:
    return event[0] * den == num * event[1]


def trace_cells(
    vertices: Sequence[Tuple[int, int]],
    canvas_width: int,
    canvas_height: int,
    rows: int,
    cols: int,
) -> List[Tuple[int, int]]:
    """Ordered 1-based ``(row, col)`` cells entered by the normalized polyline.

    ``vertices`` are the normalized dark-spot centers; the crack is the chain
    of straight segments joining consecutive vertices. For one segment
    ``P0 + t*(P1 - P0)`` the parameter at which it meets grid line ``k`` (the
    boundary at ``k * canvas_size / count``) is an exact rational, e.g. for a
    vertical line::

        t = (k * canvas_width - u0 * cols) / (dx * cols)

    Parameters are compared exclusively through integer cross products, so the
    result is independent of float rounding even when grid lines fall on
    fractional pixel positions.

    Ownership rules:

    * cells are closed on the left/top and open on the right/bottom, while the
      canvas' right and bottom edges are closed -- a segment running along a
      grid line is traced on its right / lower side;
    * when a segment crosses a horizontal and a vertical grid line at the very
      same parameter (a grid corner), it enters the diagonal cell directly and
      the two cells that only touch the corner are never recorded;
    * consecutive equal cells (segment joints, zero-length segments) are
      collapsed, but cells revisited after leaving are kept.
    """
    first_u, first_v = vertices[0]
    row = (first_v * rows) // canvas_height + 1
    col = (first_u * cols) // canvas_width + 1
    path: List[Tuple[int, int]] = [(row, col)]

    for (u0, v0), (u1, v1) in zip(vertices, vertices[1:]):
        dx = u1 - u0
        dy = v1 - v0
        if dx == 0 and dy == 0:
            # Duplicate vertex: a zero-length segment enters no new cell.
            continue

        c0 = (u0 * cols) // canvas_width + 1
        c1 = (u1 * cols) // canvas_width + 1
        r0 = (v0 * rows) // canvas_height + 1
        r1 = (v1 * rows) // canvas_height + 1

        events: List[Tuple[int, int, int, int]] = []
        # Vertical grid line k sits at u = k * canvas_width / cols and separates
        # cell k from cell k+1. Only lines strictly between the endpoint cells
        # can be crossed; parameters are normalized to a positive denominator.
        if dx > 0:
            for k in range(c0, c1):
                events.append((k * canvas_width - u0 * cols, dx * cols, COL, 1))
        elif dx < 0:
            for k in range(c1, c0):
                events.append((u0 * cols - k * canvas_width, (-dx) * cols, COL, -1))
        # Horizontal grid line j sits at v = j * canvas_height / rows.
        if dy > 0:
            for j in range(r0, r1):
                events.append((j * canvas_height - v0 * rows, dy * rows, ROW, 1))
        elif dy < 0:
            for j in range(r1, r0):
                events.append((v0 * rows - j * canvas_height, (-dy) * rows, ROW, -1))

        events.sort(key=cmp_to_key(_compare_crossings))

        i = 0
        while i < len(events):
            num, den, _, _ = events[i]
            j = i + 1
            # Group every crossing at exactly the same parameter: at most one
            # horizontal and one vertical event, i.e. one grid corner.
            while j < len(events) and _same_parameter(events[j], num, den):
                j += 1
            for event in events[i:j]:
                if event[2] == ROW:
                    row += event[3]
                else:
                    col += event[3]
            cell = (row, col)
            if path[-1] != cell:
                path.append(cell)
            i = j

    return path
