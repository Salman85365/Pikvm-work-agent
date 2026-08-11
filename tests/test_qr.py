from __future__ import annotations

from pathlib import Path

import pytest
import zxingcpp
from PIL import Image

from work_agent.pikvm import PiKVMQrError, decode_totp_qr

_SECRET = "JBSWY3DPEHPK3PXP"


def _qr_image(payload: str, *, scale: int = 8) -> Image.Image:
    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode)
    bitmap = zxingcpp.write_barcode_to_image(barcode, scale=scale)
    return Image.fromarray(bitmap).convert("RGB")


@pytest.mark.parametrize(("suffix", "image_format"), [(".png", "PNG"), (".jpg", "JPEG")])
def test_decodes_synthetic_totp_qr_locally(
    tmp_path: Path,
    suffix: str,
    image_format: str,
) -> None:
    image_path = tmp_path / f"fixture{suffix}"
    _qr_image(
        f"otpauth://totp/PiKVM:operator?secret={_SECRET}&issuer=PiKVM"
        "&algorithm=SHA1&digits=6&period=30"
    ).save(image_path, format=image_format, quality=95)

    assert decode_totp_qr(image_path) == _SECRET


def test_rejects_image_without_qr(tmp_path: Path) -> None:
    image_path = tmp_path / "blank.png"
    Image.new("RGB", (300, 200), "white").save(image_path)

    with pytest.raises(PiKVMQrError, match="No QR code"):
        decode_totp_qr(image_path)


def test_qr_payload_is_never_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = f"otpauth://totp/private-label?secret={_SECRET}&issuer=private-issuer"
    image_path = tmp_path / "private.png"
    _qr_image(payload).save(image_path)

    assert decode_totp_qr(image_path) == _SECRET
    assert payload not in caplog.text
    assert _SECRET not in caplog.text


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("https://example.com/not-a-totp", "does not contain a TOTP"),
        ("otpauth://steam/test?secret=PRIVATEVALUE", "does not contain a TOTP"),
        (f"otpauth://hotp/test?secret={_SECRET}&counter=0", "HOTP"),
        ("otpauth://totp/PiKVM:operator?issuer=PiKVM", "exactly one non-empty secret"),
        ("otpauth://totp/test?secret=not-base32!", "not valid Base32"),
        (
            f"otpauth://totp/test?secret={_SECRET}&algorithm=SHA256",
            "six-digit, 30-second, SHA-1",
        ),
        (f"otpauth://totp/test?secret={_SECRET}&digits=8", "six-digit, 30-second, SHA-1"),
        (f"otpauth://totp/test?secret={_SECRET}&period=60", "six-digit, 30-second, SHA-1"),
    ],
)
def test_rejects_nonstandard_or_unrelated_qr(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    image_path = tmp_path / "invalid.png"
    _qr_image(payload).save(image_path)

    with pytest.raises(PiKVMQrError, match=message) as error:
        decode_totp_qr(image_path)

    assert payload not in str(error.value)
    assert _SECRET not in str(error.value)


def test_rejects_multiple_qr_codes_as_ambiguous(tmp_path: Path) -> None:
    first = _qr_image(f"otpauth://totp/first?secret={_SECRET}", scale=6)
    second = _qr_image("https://example.com/unrelated", scale=6)
    combined = Image.new(
        "RGB",
        (first.width + second.width + 80, max(first.height, second.height) + 40),
        "white",
    )
    combined.paste(first, (20, 20))
    combined.paste(second, (first.width + 60, 20))
    image_path = tmp_path / "multiple.png"
    combined.save(image_path)

    with pytest.raises(PiKVMQrError, match="Multiple QR codes"):
        decode_totp_qr(image_path)


def test_rejects_missing_or_unsupported_image(tmp_path: Path) -> None:
    missing = tmp_path / "missing.png"
    with pytest.raises(PiKVMQrError, match="does not exist"):
        decode_totp_qr(missing)

    text_file = tmp_path / "not-an-image.png"
    text_file.write_text("not image data")
    with pytest.raises(PiKVMQrError, match="could not be decoded"):
        decode_totp_qr(text_file)

    gif_path = tmp_path / "unsupported.gif"
    Image.new("RGB", (10, 10), "white").save(gif_path)
    with pytest.raises(PiKVMQrError, match="PNG or JPEG"):
        decode_totp_qr(gif_path)
