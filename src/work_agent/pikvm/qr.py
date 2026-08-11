from __future__ import annotations

from pathlib import Path
from typing import Protocol

import zxingcpp
from PIL import Image, UnidentifiedImageError

from work_agent.pikvm.errors import PiKVMQrError, PiKVMTotpSecretError
from work_agent.pikvm.totp import normalize_totp_uri

_SUPPORTED_IMAGE_FORMATS = frozenset({"JPEG", "PNG"})


class QrDecoder(Protocol):
    def decode(self, image_path: Path) -> tuple[str, ...]: ...


class ZxingQrDecoder:
    """Decode QR payloads from a local PNG or JPEG without external services."""

    def decode(self, image_path: Path) -> tuple[str, ...]:
        if not image_path.exists():
            raise PiKVMQrError("The TOTP QR image does not exist.")
        if not image_path.is_file():
            raise PiKVMQrError("The TOTP QR image path is not a regular file.")

        try:
            with Image.open(image_path) as source:
                if source.format not in _SUPPORTED_IMAGE_FORMATS:
                    raise PiKVMQrError("The TOTP QR image must be a PNG or JPEG file.")
                image = source.convert("RGB")
                image.load()
        except PiKVMQrError:
            raise
        except (Image.DecompressionBombError, OSError, SyntaxError, UnidentifiedImageError):
            raise PiKVMQrError("The TOTP QR image could not be decoded as PNG or JPEG.") from None

        try:
            barcodes = zxingcpp.read_barcodes(
                image,
                formats=zxingcpp.BarcodeFormats(zxingcpp.BarcodeFormat.QRCode),
            )
            payloads = tuple(barcode.text for barcode in barcodes if barcode.valid)
        except (RuntimeError, TypeError, UnicodeError, ValueError):
            raise PiKVMQrError("The local QR decoder could not process the image.") from None
        return payloads


def decode_totp_qr(image_path: Path, *, decoder: QrDecoder | None = None) -> str:
    payloads = (decoder or ZxingQrDecoder()).decode(image_path)
    if not payloads:
        raise PiKVMQrError("No QR code was found in the supplied image.")
    if len(payloads) != 1:
        raise PiKVMQrError(
            "Multiple QR codes were found. Supply an image containing only the PiKVM TOTP QR."
        )

    payload = payloads[0]
    try:
        return normalize_totp_uri(payload)
    except PiKVMTotpSecretError as exc:
        raise PiKVMQrError(str(exc)) from None
    finally:
        del payload
