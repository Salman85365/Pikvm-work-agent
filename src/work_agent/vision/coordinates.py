from __future__ import annotations

from dataclasses import dataclass

from work_agent.vision.errors import VisionCoordinateError
from work_agent.vision.models import BoundingBox, NormalizedPoint


@dataclass(frozen=True, slots=True)
class PixelPoint:
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class PixelBoundingBox:
    x1: int
    y1: int
    x2: int
    y2: int


def normalized_to_pixel(point: NormalizedPoint, *, width: int, height: int) -> PixelPoint:
    if width <= 1 or height <= 1:
        raise VisionCoordinateError("Screen width and height must both be greater than one pixel.")
    return PixelPoint(
        x=round(point.x / 1000 * (width - 1)),
        y=round(point.y / 1000 * (height - 1)),
    )


def bounding_box_to_pixels(
    box: BoundingBox,
    *,
    width: int,
    height: int,
) -> PixelBoundingBox:
    top_left = normalized_to_pixel(
        NormalizedPoint(x=box.x1, y=box.y1),
        width=width,
        height=height,
    )
    bottom_right = normalized_to_pixel(
        NormalizedPoint(x=box.x2, y=box.y2),
        width=width,
        height=height,
    )
    return PixelBoundingBox(
        x1=top_left.x,
        y1=top_left.y,
        x2=bottom_right.x,
        y2=bottom_right.y,
    )
