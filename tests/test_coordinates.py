from __future__ import annotations

import pytest

from work_agent.vision import (
    BoundingBox,
    NormalizedPoint,
    PixelPoint,
    VisionCoordinateError,
    bounding_box_to_pixels,
    normalized_to_pixel,
)


def test_normalized_coordinates_map_to_pixel_extents() -> None:
    assert normalized_to_pixel(NormalizedPoint(x=0, y=0), width=1920, height=1080) == PixelPoint(
        x=0,
        y=0,
    )
    bottom_right = normalized_to_pixel(NormalizedPoint(x=1000, y=1000), width=1920, height=1080)
    assert (bottom_right.x, bottom_right.y) == (1919, 1079)


def test_bounding_box_conversion_preserves_order() -> None:
    pixels = bounding_box_to_pixels(
        BoundingBox(x1=250, y1=250, x2=750, y2=750),
        width=1001,
        height=1001,
    )

    assert (pixels.x1, pixels.y1, pixels.x2, pixels.y2) == (250, 250, 750, 750)


def test_coordinate_conversion_rejects_invalid_dimensions() -> None:
    with pytest.raises(VisionCoordinateError):
        normalized_to_pixel(NormalizedPoint(x=500, y=500), width=1, height=1080)
