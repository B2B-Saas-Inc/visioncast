"""Uploaded event cover images: validation, re-encoding and the public response.

Only PNG, JPEG and WebP rasters are accepted, identified by their magic bytes *and*
a full Pillow decode (SVG, GIF, HTML and polyglots are rejected). Every accepted
image is decoded and re-encoded from pixels only, which drops EXIF/XMP/ICC/text
chunks (GPS, camera and author metadata) and any trailing payload. Bytes are
stored in the database (``CoverImage``), so no media volume or writable
filesystem is needed in the read-only web container; ids are random UUIDs and the
serving view never touches file paths.

Size ceilings (marketing covers, not photo archives): ``MAX_UPLOAD_BYTES`` raw
upload, ``MAX_DIMENSION`` per side, ``MAX_PIXELS`` total, ``MAX_STORED_BYTES``
after re-encoding.
"""
import hashlib
import io
import warnings

from django.core.exceptions import ValidationError
from django.core.files.uploadhandler import MemoryFileUploadHandler, SkipFile
from django.http import Http404, HttpResponse

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_STORED_BYTES = 3 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = 12_000_000
MIN_DIMENSION = 16

# Pillow format name -> stored content type.
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class BoundedMemoryUploadHandler(MemoryFileUploadHandler):
    """The only upload handler (settings.FILE_UPLOAD_HANDLERS): files stay in memory
    (no temp files in the read-only container) and a file over MAX_UPLOAD_BYTES is
    dropped mid-stream; ``request.upload_too_large`` tells the view to refuse the save
    rather than proceed as if no file was sent."""

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        self.activated = True  # never fall back to temporary files

    def new_file(self, *args, **kwargs):
        self.received = 0
        return super().new_file(*args, **kwargs)

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > MAX_UPLOAD_BYTES:
            if self.request is not None:
                self.request.upload_too_large = True
            raise SkipFile()
        return super().receive_data_chunk(raw_data, start)


def _sniff(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    return None


def read_upload(upload):
    """Bytes of a Django ``UploadedFile``, refusing anything over MAX_UPLOAD_BYTES without reading it all."""
    if upload.size is not None and upload.size > MAX_UPLOAD_BYTES:
        raise ValidationError(f"Images must be {MAX_UPLOAD_BYTES // (1024 * 1024)} MB or smaller.")
    upload.seek(0)
    data = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(f"Images must be {MAX_UPLOAD_BYTES // (1024 * 1024)} MB or smaller.")
    if not data:
        raise ValidationError("The file is empty.")
    return data


def process(data):
    """Validate raw bytes and return ``(content_type, encoded_bytes, width, height)``.

    Raises ``ValidationError`` for anything that is not a decodable PNG/JPEG/WebP
    within the limits.
    """
    from PIL import Image, ImageOps

    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(f"Images must be {MAX_UPLOAD_BYTES // (1024 * 1024)} MB or smaller.")
    fmt = _sniff(data[:16])
    if fmt is None:
        raise ValidationError("Upload a PNG, JPEG or WebP image. SVG and other formats aren't accepted.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=[fmt]) as img:
                if img.format != fmt:
                    raise ValidationError("The file's contents don't match its image type.")
                width, height = img.size
                if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
                    raise ValidationError(
                        f"Images can be at most {MAX_DIMENSION}×{MAX_DIMENSION} pixels ({MAX_PIXELS // 1_000_000} megapixels)."
                    )
                if width < MIN_DIMENSION or height < MIN_DIMENSION:
                    raise ValidationError("That image is too small to use as a cover.")
                if getattr(img, "n_frames", 1) > 1:
                    raise ValidationError("Animated images aren't accepted.")
                img.load()
                img = ImageOps.exif_transpose(img)
                # Convert palette transparency before clearing metadata, then keep pixels only.
                mode = "RGBA" if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info else "RGB"
                img = img.convert(mode)
                img.info.clear()
                out = io.BytesIO()
                if fmt == "JPEG":
                    img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
                elif fmt == "PNG":
                    mode = "RGBA" if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info else "RGB"
                    img.convert(mode).save(out, "PNG", optimize=True)
                else:
                    mode = "RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB"
                    img.convert(mode).save(out, "WEBP", quality=85)
                width, height = img.size
    except ValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError("That image is too large to process.")
    except Exception:  # Pillow raises many types (OSError, SyntaxError, ValueError, struct.error) for bad input
        raise ValidationError("That file isn't a valid image.")
    encoded = out.getvalue()
    if len(encoded) > MAX_STORED_BYTES:
        raise ValidationError("That image is too large after processing; try a smaller one.")
    return FORMATS[fmt], encoded, width, height


def create_cover(upload, *, user=None):
    """Validate ``upload`` and save a ``CoverImage``. Call inside the same transaction
    as the event save so a failed save leaves no orphan row."""
    from webinars.models import CoverImage

    content_type, encoded, width, height = process(read_upload(upload))
    return CoverImage.objects.create(
        content_type=content_type,
        data=encoded,
        width=width,
        height=height,
        byte_size=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )


def cover_response(request, pk, ext):
    """Public, immutable image response. Looks up by UUID only; never a filesystem path."""
    from webinars.models import CoverImage

    if request.method not in ("GET", "HEAD"):
        response = HttpResponse(status=405)
        response["Allow"] = "GET, HEAD"
        return response
    cover = CoverImage.objects.filter(pk=pk).only("id", "content_type", "data", "sha256").first()
    if cover is None or CoverImage.EXTENSIONS.get(cover.content_type) != ext:
        raise Http404("No such image")
    etag = f'"{cover.sha256}"'
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponse(status=304)
    else:
        response = HttpResponse(bytes(cover.data), content_type=cover.content_type)
    response["ETag"] = etag
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["Content-Disposition"] = f'inline; filename="cover.{ext}"'
    response["Cross-Origin-Resource-Policy"] = "cross-origin"
    return response
