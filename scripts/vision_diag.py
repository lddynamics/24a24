"""Diagnostic du modèle de vision (lecture des images reçues).

    python -m scripts.vision_diag [photo.jpg]                 # teste le modèle VISION_MODEL du .env
    python -m scripts.vision_diag photo.jpg --model MODELE    # teste un autre modèle, sans modifier .env
    python -m scripts.vision_diag --candidates                # essaie quelques modèles « vision » courants avec votre clé

Sans photo, une image synthétique est utilisée (elle vérifie que l'API accepte les images, pas la qualité de la description :
essayez avec une vraie photo de robe). Un 404 « not found for account » = modèle indisponible pour votre compte : en choisir un autre.
"""
import argparse
import io
import sys

from PIL import Image, ImageDraw

from app.config import get_settings
from app.services import vision

CANDIDATES = [
    "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.2-90b-vision-instruct",
    "meta/llama-4-maverick-17b-128e-instruct",
    "nvidia/nemotron-nano-12b-v2-vl",
    "microsoft/phi-3.5-vision-instruct",
    "meta-llama/llama-4-scout-17b-16e-instruct",   # Groq
]


def _sample() -> bytes:
    img = Image.new("RGB", (640, 480), (245, 245, 240))
    d = ImageDraw.Draw(img)
    d.polygon([(260, 80), (380, 80), (430, 400), (210, 400)], fill=(230, 190, 20))     # une « robe » jaune
    d.rectangle([300, 20, 340, 80], fill=(120, 80, 40))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def _try(model: str, data: bytes) -> bool:
    s = get_settings()
    s.vision_model = model
    describer = vision.build_describer()
    if describer is None:
        print(f"  {model}: clé manquante (VISION_API_KEY / LLM_API_KEY / GROQ_API_KEY, ou ANTHROPIC_API_KEY pour claude-…)")
        return False
    try:
        out = describer.describe(data, "image/jpeg")
    except vision.VisionError as e:
        print(f"  {model}: ÉCHEC — {e}")
        return False
    print(f"  {model}: OK\n    {out}")
    return True


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Teste le modèle de vision.")
    ap.add_argument("photo", nargs="?", help="image à décrire (défaut : image synthétique)")
    ap.add_argument("--model", help="modèle à tester (défaut : VISION_MODEL du .env)")
    ap.add_argument("--candidates", action="store_true", help="essaie quelques modèles courants")
    args = ap.parse_args(argv)
    data = open(args.photo, "rb").read() if args.photo else _sample()
    if args.candidates:
        return 0 if any([_try(m, data) for m in CANDIDATES]) else 1
    model = args.model or get_settings().vision_model
    if not model:
        print("VISION_MODEL est vide dans .env (ou utilisez --model MODELE, ou --candidates).")
        return 2
    return 0 if _try(model, data) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
