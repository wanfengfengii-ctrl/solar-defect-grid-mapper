"""Request/response schemas with strict, batch-wide validation.

Any illegal item rejects the whole batch with HTTP 422; out-of-bounds points
report their array index via a custom error whose ``ctx['index']`` is surfaced
by the exception handler in :mod:`app.main`.
"""

from __future__ import annotations

from typing import Annotated, List, Literal

from pydantic import (
    BaseModel,
    Field,
    StrictInt,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

# Positive, strictly-typed integer: rejects floats, strings and booleans so
# that e.g. width=3.5 or "10" never passes validation silently.
PositiveInt = Annotated[StrictInt, Field(gt=0)]


class Point(BaseModel):
    """One dark-spot pixel in the raw camera image (origin top-left)."""

    x: StrictInt
    y: StrictInt


class _CanvasGridRequest(BaseModel):
    """Canvas, grid and orientation fields shared by every endpoint."""

    width: PositiveInt
    height: PositiveInt
    rows: PositiveInt
    cols: PositiveInt
    rotation: Literal[0, 90, 180, 270]


class InspectRequest(_CanvasGridRequest):
    """Batch of dark spots on one EL image."""

    points: List[Point] = Field(min_length=1)

    @model_validator(mode="after")
    def _points_within_image(self) -> "InspectRequest":
        """Enforce 0 <= x < width and 0 <= y < height for every point.

        The first offending point aborts the whole batch; its index travels in
        the error context so the 422 response can name the exact array element.
        """
        for index, point in enumerate(self.points):
            if not (0 <= point.x < self.width and 0 <= point.y < self.height):
                raise PydanticCustomError(
                    "point_out_of_bounds",
                    "points[{index}] = ({x}, {y}) violates 0 <= x < {width} and 0 <= y < {height}",
                    {
                        "index": index,
                        "x": point.x,
                        "y": point.y,
                        "width": self.width,
                        "height": self.height,
                    },
                )
        return self


class TraceRequest(_CanvasGridRequest):
    """Ordered polyline of dark-spot centers marking one crack."""

    vertices: List[Point] = Field(min_length=1)

    @field_validator("vertices", mode="before")
    @classmethod
    def _vertices_well_typed_and_in_bounds(cls, value: object, info: ValidationInfo) -> object:
        """Validate every vertex independently and collect *all* failures.

        Type errors (floats, strings, booleans, missing coordinates, malformed
        items) and out-of-bounds errors are gathered across the whole array, so
        a 422 names each offending input position instead of only the first.
        Width/height are already validated by the time this field runs.
        """
        if not isinstance(value, list):
            # Let pydantic-core emit the standard list_type / too_short error.
            return value

        width = (info.data or {}).get("width")
        height = (info.data or {}).get("height")
        line_errors: List[dict] = []

        for index, item in enumerate(value):
            try:
                point = Point.model_validate(item)
            except ValidationError as exc:
                # Relative locs are prefixed with the field name by pydantic,
                # yielding ("vertices", index, ...) in the final error list.
                for error in exc.errors():
                    entry = {"type": error["type"], "loc": (index, *error.get("loc", ()))}
                    if "input" in error:
                        entry["input"] = error["input"]
                    if error.get("ctx"):
                        entry["ctx"] = error["ctx"]
                    line_errors.append(entry)
                continue

            if isinstance(width, int) and isinstance(height, int) and not (
                0 <= point.x < width and 0 <= point.y < height
            ):
                line_errors.append(
                    {
                        "type": PydanticCustomError(
                            "vertex_out_of_bounds",
                            "vertices[{index}] = ({x}, {y}) violates 0 <= x < {width} and 0 <= y < {height}",
                            {
                                "index": index,
                                "x": point.x,
                                "y": point.y,
                                "width": width,
                                "height": height,
                            },
                        ),
                        "loc": (index,),
                        "input": {"x": point.x, "y": point.y},
                    }
                )

        if line_errors:
            raise ValidationError.from_exception_data(cls.__name__, line_errors)
        # Rebuild from the raw list so declared field types still run normally.
        return value


class SpotResult(BaseModel):
    """Normalized coordinates and 1-based cell address of one dark spot."""

    index: int
    x: int
    y: int
    row: int
    col: int


class Vertex(BaseModel):
    """One normalized polyline vertex (1:1 with the input order)."""

    index: int
    x: int
    y: int


class CellStep(BaseModel):
    """One ordered cell on the crack path."""

    row: int
    col: int


class Canvas(BaseModel):
    """Canvas size after rotation normalization."""

    width: int
    height: int


class Grid(BaseModel):
    """Cell grid of the module."""

    rows: int
    cols: int


class InspectResponse(BaseModel):
    rotation: int
    canvas: Canvas
    grid: Grid
    results: List[SpotResult]


class TraceResponse(BaseModel):
    rotation: int
    canvas: Canvas
    grid: Grid
    vertices: List[Vertex]
    path: List[CellStep]
