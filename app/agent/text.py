"""Nettoyage des réponses avant envoi : Messenger n'affiche pas le Markdown (les ** restent visibles)."""
import re

_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_BULLET = re.compile(r"^([ \t]*)[*•][ \t]+", re.MULTILINE)
_BLANKS = re.compile(r"\n{3,}")
# Annotations ajoutées par le système dans l'historique : un modèle peut les recopier par imitation, le client ne doit jamais les lire.
_ANNOTATION = re.compile(r"^[ \t]*\[(?:Photo envoyée|Image envoyée|Image du client|Message vocal)[^\]\n]*\][ \t]*", re.MULTILINE)


def clean_for_messenger(text: str) -> str:
    text = _ANNOTATION.sub("", text)
    text = _BOLD.sub(r"\2", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub(r"\1- ", text)
    text = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"\1", text)  # *italique*
    text = text.replace("`", "")
    return _BLANKS.sub("\n\n", text).strip()


def fold(value: str) -> str:
    """Minuscules sans accents : pour comparer des libellés saisis par un humain ou un modèle."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", value.lower().strip())
    return "".join(c for c in decomposed if not unicodedata.combining(c))
