from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from work_agent.vision.errors import VisionImageError

_MEDIA_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


@dataclass(frozen=True, slots=True)
class DecodedImage:
    content: bytes
    width: int
    height: int
    media_type: str


def decode_image(content: bytes) -> DecodedImage:
    if not content:
        raise VisionImageError("The screenshot is empty.")
    try:
        with Image.open(BytesIO(content)) as image:
            image_format = image.format or ""
            width = image.width
            height = image.height
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise VisionImageError("The screenshot is not a valid supported image.") from exc

    media_type = _MEDIA_TYPES.get(image_format.upper())
    if media_type is None:
        raise VisionImageError("The screenshot must be a JPEG, PNG, or WebP image.")
    if width <= 1 or height <= 1:
        raise VisionImageError("The screenshot dimensions must be greater than one pixel.")
    return DecodedImage(
        content=content,
        width=width,
        height=height,
        media_type=media_type,
    )


def load_image(path: Path) -> DecodedImage:
    try:
        content = path.expanduser().read_bytes()
    except OSError as exc:
        raise VisionImageError("Could not read the requested image file.") from exc
    return decode_image(content)
