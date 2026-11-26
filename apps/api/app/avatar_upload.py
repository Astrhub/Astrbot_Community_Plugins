"""Server-side avatar normalization: strict decode checks, EXIF handling, 1:1 crop, WebP output."""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps

MAX_AVATAR_SOURCE_BYTES = 5 * 1024 * 1024
# Roughly 4000x4000; anything larger is refused before decoding to block decompression bombs.
MAX_AVATAR_SOURCE_PIXELS = 16_000_000
AVATAR_OUTPUT_EDGE = 512
AVATAR_WEBP_QUALITY = 82
AVATAR_CONTENT_TYPE = "image/webp"
AVATAR_FILE_EXTENSION = "webp"

_SOURCE_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"RIFF", "webp"),
)
_WEBP_RIFF_OFFSET = (8, 12)
_WEBP_RIFF_BRAND = b"WEBP"


class AvatarValidationError(Exception):
    """Raised when an uploaded avatar fails size, format, or decode checks."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class NormalizedAvatar:
    content: bytes
    content_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int


def detect_avatar_source_format(data: bytes) -> str | None:
    """Sniff PNG/JPEG/WebP magic bytes; rejects GIF, SVG, HEIC, and anything else."""
    for prefix, name in _SOURCE_MAGIC_PREFIXES:
        if data.startswith(prefix):
            if (
                name == "webp"
                and data[_WEBP_RIFF_OFFSET[0] : _WEBP_RIFF_OFFSET[1]] != _WEBP_RIFF_BRAND
            ):
                return None
            return name
    return None


def normalize_avatar(data: bytes) -> NormalizedAvatar:
    """Decode, orient, crop to 1:1, shrink to 512x512, strip metadata, and re-encode as WebP."""
    if not data:
        raise AvatarValidationError("avatar_empty", "头像文件为空", 400)
    if len(data) > MAX_AVATAR_SOURCE_BYTES:
        raise AvatarValidationError("avatar_too_large", "头像不能超过 5 MB", 413)
    source_format = detect_avatar_source_format(data)
    if source_format is None:
        raise AvatarValidationError(
            "avatar_format_unsupported",
            "仅支持 PNG、JPEG 或 WebP 图片",
            415,
        )

    previous_limit = Image.MAX_IMAGE_PIXELS
    # Pillow only raises DecompressionBombError above 2x this limit, so the exact budget is
    # checked explicitly after open(); the lowered limit still bounds the worst-case allocation.
    Image.MAX_IMAGE_PIXELS = MAX_AVATAR_SOURCE_PIXELS
    try:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                image = Image.open(io.BytesIO(data))
            if image.format is None:
                raise AvatarValidationError("avatar_decode_failed", "无法识别图片格式", 415)
            if image.format.lower() != source_format:
                raise AvatarValidationError(
                    "avatar_format_mismatch",
                    "图片内容与文件头不符",
                    415,
                )
            if image.width * image.height > MAX_AVATAR_SOURCE_PIXELS:
                raise AvatarValidationError(
                    "avatar_too_many_pixels",
                    "图片像素总量超过限制",
                    413,
                )
            image.load()
        except Image.DecompressionBombError as exc:
            raise AvatarValidationError(
                "avatar_too_many_pixels",
                "图片像素总量超过限制",
                413,
            ) from exc
        except AvatarValidationError:
            raise
        except Exception as exc:
            raise AvatarValidationError("avatar_decode_failed", "图片解码失败", 415) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

    # EXIF orientation is applied first so a rotated portrait photo crops on the right axis.
    oriented = ImageOps.exif_transpose(image)
    if oriented.mode != "RGB":
        # Palette and grayscale sources lose nothing; alpha is preserved for transparent PNG/WebP.
        oriented = oriented.convert("RGBA" if "A" in oriented.getbands() else "RGB")

    width, height = oriented.size
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    square = oriented.crop((left, top, left + edge, top + edge))

    if edge > AVATAR_OUTPUT_EDGE:
        square = square.resize(
            (AVATAR_OUTPUT_EDGE, AVATAR_OUTPUT_EDGE),
            Image.Resampling.LANCZOS,
        )

    output = io.BytesIO()
    # Saving without exif=/icc_profile=/dpi= arguments writes none of the source metadata.
    square.save(
        output,
        format="WEBP",
        quality=AVATAR_WEBP_QUALITY,
        method=4,
        lossless=False,
        exact=False,
    )
    content = output.getvalue()
    if not content.startswith(b"RIFF") or content[8:12] != b"WEBP":
        raise AvatarValidationError("avatar_encode_failed", "头像编码失败", 500)
    return NormalizedAvatar(
        content=content,
        content_type=AVATAR_CONTENT_TYPE,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        width=square.width,
        height=square.height,
    )
