"""Images : reconnaissance par le contenu, normalisation (JPEG, taille bornée, EXIF retiré) et version allégée pour la vision.

On ne fait confiance ni à l'en-tête Content-Type ni à l'extension : le format est reconnu aux premiers octets, puis
l'image est décodée et ré-encodée. Ce qui sort d'ici est toujours un JPEG que nous avons produit nous-mêmes :
jamais le fichier d'origine (donc jamais de métadonnées GPS, jamais de contenu actif déguisé en image).
"""
import io
import logging

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 40_000_000          # au-delà : refusé (bombe de décompression)
MAX_SIDE = 1600                              # plus grand côté conservé pour les images stockées
JPEG = "image/jpeg"


class ImageError(Exception):
    """Image refusée, avec un message lisible par l'utilisateur."""


def sniff_image(data: bytes) -> str | None:
    """Type MIME reconnu d'après les premiers octets (JPEG, PNG, GIF, WebP), ou None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _open_rgb(data: bytes) -> Image.Image:
    if sniff_image(data) is None:
        raise ImageError("Format d'image non pris en charge (JPEG, PNG, WebP ou GIF).")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()                                           # force le décodage : une image tronquée échoue ici
        img = ImageOps.exif_transpose(img)                   # applique l'orientation du téléphone
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, SyntaxError) as e:
        raise ImageError("Image illisible, abîmée ou trop grande.") from e
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))   # fond blanc sous la transparence
        flat.paste(rgba, mask=rgba.split()[-1])
        return flat
    return img.convert("RGB")


def _encode(img: Image.Image, max_side: int, quality: int) -> bytes:
    copy = img.copy()
    copy.thumbnail((max_side, max_side), Image.LANCZOS)      # ne grossit jamais l'image
    out = io.BytesIO()
    copy.save(out, "JPEG", quality=quality, optimize=True)  # aucune métadonnée recopiée
    return out.getvalue()


def normalize_image(data: bytes, *, max_side: int = MAX_SIDE, quality: int = 85) -> tuple[bytes, str]:
    """(JPEG, 'image/jpeg') prêt à stocker et à envoyer. Lève ImageError si ce n'est pas une image exploitable."""
    return _encode(_open_rgb(data), max_side, quality), JPEG


# Paliers (côté max, qualité) essayés jusqu'à tenir dans la limite : certains fournisseurs bornent la taille d'une image envoyée en base64.
_VISION_STEPS = ((1024, 80), (1024, 65), (800, 65), (640, 60), (512, 55), (384, 50))


def compress_for_vision(data: bytes, *, max_bytes: int = 130_000) -> bytes:
    """Version allégée d'une image pour le modèle de vision (moins de jetons, moins de latence, passe les limites d'envoi)."""
    img = _open_rgb(data)
    best = b""
    for side, quality in _VISION_STEPS:
        best = _encode(img, side, quality)
        if len(best) <= max_bytes:
            break
    return best
