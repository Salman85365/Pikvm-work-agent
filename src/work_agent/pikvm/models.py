from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class MouseButton(StrEnum):
    LEFT = "left"
    MIDDLE = "middle"
    RIGHT = "right"


@dataclass(frozen=True, slots=True)
class ScreenSize:
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 1 or self.height <= 1:
            raise ValueError("Screen width and height must both be greater than one pixel.")


@dataclass(frozen=True, slots=True)
class Screenshot:
    content: bytes
    size: ScreenSize
    captured_at: datetime
    media_type: str = "image/jpeg"

    def save(self, destination: str | Path) -> Path:
        path = Path(destination).expanduser()
        path.write_bytes(self.content)
        return path
