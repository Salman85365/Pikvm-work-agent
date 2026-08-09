from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, UnidentifiedImageError

from work_agent.vision.coordinates import bounding_box_to_pixels, normalized_to_pixel
from work_agent.vision.errors import VisionImageError
from work_agent.vision.models import ScreenAnalysis, UIElement

_ELEMENT_COLOR = (0, 140, 255)
_TARGET_COLOR = (255, 60, 60)
_TEXT_COLOR = (255, 255, 255)


def save_analysis_overlay(
    screenshot: bytes,
    analysis: ScreenAnalysis,
    output: Path,
) -> Path:
    """Save an explicitly requested local overlay without changing the source image."""
    destination = output.expanduser()
    if destination.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise VisionImageError("The overlay output must be a JPEG or PNG file.")

    try:
        with Image.open(BytesIO(screenshot)) as source:
            image = source.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise VisionImageError("The screenshot could not be opened for the debug overlay.") from exc

    if image.width != analysis.screenshot_width or image.height != analysis.screenshot_height:
        raise VisionImageError(
            "The analysis dimensions do not match the screenshot used for the overlay."
        )

    draw = ImageDraw.Draw(image)
    target_id = analysis.target.id if analysis.target is not None else None
    for element in analysis.relevant_elements:
        if element.id != target_id:
            _draw_element(draw, element, image.width, image.height, _ELEMENT_COLOR)
    if analysis.target is not None:
        _draw_element(draw, analysis.target, image.width, image.height, _TARGET_COLOR)
        if analysis.target.click_point is not None:
            point = normalized_to_pixel(
                analysis.target.click_point,
                width=image.width,
                height=image.height,
            )
            radius = max(5, round(min(image.width, image.height) * 0.008))
            draw.ellipse(
                (point.x - radius, point.y - radius, point.x + radius, point.y + radius),
                outline=_TARGET_COLOR,
                width=3,
            )
            draw.line(
                (point.x - radius, point.y, point.x + radius, point.y),
                fill=_TARGET_COLOR,
                width=2,
            )
            draw.line(
                (point.x, point.y - radius, point.x, point.y + radius),
                fill=_TARGET_COLOR,
                width=2,
            )

    try:
        image.save(destination)
    except OSError as exc:
        raise VisionImageError("Could not save the requested debug overlay.") from exc
    return destination


def _draw_element(
    draw: ImageDraw.ImageDraw,
    element: UIElement,
    width: int,
    height: int,
    color: tuple[int, int, int],
) -> None:
    if element.bounding_box is None:
        return
    box = bounding_box_to_pixels(element.bounding_box, width=width, height=height)
    draw.rectangle((box.x1, box.y1, box.x2, box.y2), outline=color, width=3)

    label = f"{element.label} ({element.confidence:.2f})"
    text_box = draw.textbbox((box.x1, box.y1), label)
    label_width = text_box[2] - text_box[0] + 6
    label_height = text_box[3] - text_box[1] + 6
    label_y = max(0, box.y1 - label_height)
    draw.rectangle(
        (box.x1, label_y, min(width - 1, box.x1 + label_width), label_y + label_height),
        fill=color,
    )
    draw.text((box.x1 + 3, label_y + 3), label, fill=_TEXT_COLOR)
