"""Authenticated, bounded poster uploads for cinema favorites."""

from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import tempfile
import warnings

from aiohttp import web
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError:  # Keep the integration usable without an optional runtime library.
    Image = ImageOps = None
    UnidentifiedImageError = OSError


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_BODY_BYTES = MAX_FILE_BYTES + 64 * 1024
MAX_PIXELS = 24_000_000
MAX_DIMENSION = 8192
POSTER_DIRECTORY = ("www", "cinema_control", "posters")
POSTER_URL = "/local/cinema_control/posters/"


class ImageUploadError(Exception):
    """An error containing only safe, user-facing upload information."""

    def __init__(self, message: str, code: str = "invalid_image", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _file_from_multipart(body: bytes, content_type: str) -> bytes:
    """Parse an already bounded form without trusting names or MIME image types."""
    try:
        header = content_type.encode("ascii")
    except UnicodeError:
        raise ImageUploadError("Bitte eine Bilddatei als Formular hochladen.") from None
    if len(header) > 512 or b"\r" in header or b"\n" in header:
        raise ImageUploadError("Das Upload-Formular ist ungültig.")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + header + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if message.get_content_type() != "multipart/form-data" or not message.is_multipart() or message.defects:
        raise ImageUploadError("Bitte eine Bilddatei als Formular hochladen.")
    parts = iter(message.iter_parts())
    part = next(parts, None)
    if (part is None or next(parts, None) is not None or part.is_multipart() or part.defects
            or part.get_content_disposition() != "form-data"
            or part.get_param("name", header="content-disposition") != "file"
            or str(part.get("Content-Transfer-Encoding", "binary")).lower() not in {"binary", "8bit"}):
        raise ImageUploadError("Bitte genau eine Bilddatei im Feld „file“ hochladen.")
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes) or not payload:
        raise ImageUploadError("Bitte eine Bilddatei auswählen.")
    if len(payload) > MAX_FILE_BYTES:
        raise ImageUploadError("Das Bild darf höchstens 5 MiB groß sein.", "image_too_large", 413)
    return payload


def _normalize_image(payload: bytes) -> bytes:
    """Decode only raster images and create a metadata-free, bounded JPEG."""
    if Image is None or ImageOps is None:
        raise ImageUploadError("Der Bild-Upload ist auf dieser Instanz nicht verfügbar.", "image_unavailable", 503)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload), formats=("JPEG", "PNG", "WEBP")) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ImageUploadError("Bitte ein JPEG-, PNG- oder WebP-Bild auswählen.")
                width, height = source.size
                if (width < 1 or height < 1 or max(width, height) > MAX_DIMENSION
                        or width * height > MAX_PIXELS):
                    raise ImageUploadError("Das Bild darf höchstens 24 Megapixel und 8192 Pixel pro Seite haben.",
                                           "image_dimensions", 413)
                source.load()
                image = ImageOps.exif_transpose(source)
                image.thumbnail((1200, 1800), Image.Resampling.LANCZOS)
                # A fresh RGB canvas also drops EXIF, ICC, comments and other metadata.
                clean = Image.new("RGB", image.size, "white")
                if "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info):
                    rgba = image.convert("RGBA")
                    clean.paste(rgba, mask=rgba.getchannel("A"))
                    rgba.close()
                else:
                    rgb = image.convert("RGB")
                    clean.paste(rgb)
                    rgb.close()
                image.close()
                output = BytesIO()
                clean.save(output, format="JPEG", quality=90, optimize=True)
                clean.close()
                return output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageUploadError("Die Abmessungen dieses Bildes sind zu groß.", "image_dimensions", 413) from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ImageUploadError("Das Bild konnte nicht gelesen werden. Bitte JPEG, PNG oder WebP verwenden.") from None


def _poster_root(config_directory: str) -> Path:
    """Create and resolve the poster directory without following it outside config."""
    try:
        config_root = Path(config_directory).resolve(strict=True)
        poster_root = config_root.joinpath(*POSTER_DIRECTORY).resolve()
        if not poster_root.is_relative_to(config_root):
            raise OSError("Poster directory outside config root")
        poster_root.mkdir(parents=True, exist_ok=True)
        poster_root = poster_root.resolve(strict=True)
        if not poster_root.is_relative_to(config_root) or not poster_root.is_dir():
            raise OSError("Poster directory outside config root")
        return poster_root
    except (OSError, RuntimeError, ValueError):
        raise OSError("Das Bildverzeichnis konnte nicht angelegt werden.") from None


def _save_poster(config_directory: str, body: bytes, content_type: str) -> str:
    """Normalize and atomically persist a poster inside the checked HA config root."""
    normalized = _normalize_image(_file_from_multipart(body, content_type))
    filename = sha256(normalized).hexdigest() + ".jpg"
    temporary = None
    try:
        poster_root = _poster_root(config_directory)
        destination = poster_root / filename
        if destination.is_symlink():
            raise OSError("Invalid poster destination")
        if not destination.exists():
            with tempfile.NamedTemporaryFile(dir=poster_root, prefix=".upload-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write(normalized)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            temporary = None
        elif not destination.is_file():
            raise OSError("Invalid poster destination")
    except (OSError, RuntimeError, ValueError):
        raise ImageUploadError("Das Bild konnte nicht gespeichert werden.", "image_storage", 400) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return POSTER_URL + filename


class CinemaImageView(HomeAssistantView):
    """An authenticated upload endpoint restricted to HA administrators."""

    url = "/api/cinema_control/image"
    name = "api:cinema_control:image"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._lock = asyncio.Lock()

    async def post(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return web.json_response({"error": "forbidden", "message": "Bilder dürfen nur Administratoren hochladen."}, status=403)
        if not self._hass.data.get(DOMAIN, {}).get("image_static_registered"):
            return web.json_response({"error": "image_storage", "message": "Die Bildspeicherung ist momentan nicht verfügbar."}, status=503)
        if request.content_type != "multipart/form-data":
            return web.json_response({"error": "invalid_image", "message": "Bitte eine Bilddatei als Formular hochladen."}, status=400)
        try:
            if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
                raise ImageUploadError("Das Bild darf höchstens 5 MiB groß sein.", "image_too_large", 413)
            # The raw stream is bounded even for chunked requests; no request.post() buffers it first.
            async with self._lock:
                async with asyncio.timeout(30):
                    body = bytearray()
                    async for chunk in request.content.iter_chunked(64 * 1024):
                        if len(body) + len(chunk) > MAX_BODY_BYTES:
                            raise ImageUploadError("Das Bild darf höchstens 5 MiB groß sein.", "image_too_large", 413)
                        body.extend(chunk)
                url = await self._hass.async_add_executor_job(
                    _save_poster, self._hass.config.path(), bytes(body), request.headers.get("Content-Type", "")
                )
        except ImageUploadError as error:
            return web.json_response({"error": error.code, "message": str(error)}, status=error.status)
        except (TimeoutError, ConnectionError, ValueError):
            return web.json_response({"error": "invalid_upload", "message": "Der Upload wurde nicht vollständig übertragen. Bitte erneut versuchen."}, status=400)
        return web.json_response({"url": url}, status=201)


async def async_register_image_upload(hass: HomeAssistant) -> None:
    """Serve posters immediately, even if HA started before www existed."""
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("image_static_registered"):
        try:
            poster_root = await hass.async_add_executor_job(_poster_root, hass.config.path())
            await hass.http.async_register_static_paths([
                StaticPathConfig(POSTER_URL.rstrip("/"), str(poster_root), True)
            ])
        except OSError:
            # Poster storage must not prevent existing room controls from loading.
            pass
        else:
            data["image_static_registered"] = True
    if not data.get("image_upload_registered"):
        hass.http.register_view(CinemaImageView(hass))
        data["image_upload_registered"] = True
