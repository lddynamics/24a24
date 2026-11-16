"""Les liens de paiement ne passent JAMAIS par le modèle.

Problème constaté : un modèle qui recopie une URL (surtout un jeton FedaPay de 200+ caractères) la déforme
(« fedapay » -> « fedipay »), la tronque ou, pire, en invente une (« pay.maisonkora.com/... »).
Une consigne de prompt ne suffit pas : ici le CODE garantit le résultat.

Principe :
1. l'outil `create_payment_link` ne renvoie PAS l'URL au modèle, seulement un jeton `{{LIEN_PAIEMENT}}` ;
   la vraie URL est gardée côté serveur (ToolContext.payment_url) ;
2. avant l'envoi, `finalize_reply` remplace le jeton par la vraie URL ;
3. toute autre URL écrite par le modèle est supprimée (ou remplacée par la vraie, si elle existe) ;
4. si un lien a été généré pendant ce tour et que la réponse ne le contient pas, il est ajouté ;
5. l'historique rejoué au modèle ne contient plus de vraies URL (`mask_urls`) : il n'a donc rien à recopier,
   et on économise des tokens (une URL FedaPay en coûte beaucoup).
"""
import re
from dataclasses import dataclass

LINK_TOKEN = "{{LIEN_PAIEMENT}}"

# Variantes que les modèles produisent à la place du jeton exact : {{ LIEN_PAIEMENT }}, [LIEN_PAIEMENT], <LIEN_PAIEMENT>…
_TOKEN_RE = re.compile(r"[\{\[<]{1,2}\s*LIEN[_ ]PAIEMENT\s*[\}\]>]{1,2}", re.IGNORECASE)

# URL avec schéma, « www. », ou domaine.tld/chemin (cas d'une URL inventée sans https://).
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"'«»]+"
    r"|(?<![\w@.])(?:[a-z0-9-]+\.)+[a-z]{2,}/[^\s<>\"'«»]*",
    re.IGNORECASE,
)
_TRAILING = ".,;:!?)]}»'\""   # ponctuation de phrase collée à une URL


@dataclass
class Finalized:
    text: str
    ok: bool                       # False : le modèle parlait d'un lien que le serveur n'a pas -> ne pas envoyer tel quel
    removed_urls: list[str]


def _split_trailing(url: str) -> tuple[str, str]:
    core = url.rstrip(_TRAILING)
    return core, url[len(core):]


def _domain(url: str) -> str:
    m = re.match(r"(?:https?://)?(?:www\.)?([^/\s?#:]+)", url, re.IGNORECASE)
    return (m.group(1) if m else "").lower()


def mask_urls(text: str) -> str:
    """Pour l'historique rejoué au modèle : remplace toute URL par le jeton (le modèle ne voit jamais de vraie URL)."""
    text = _TOKEN_RE.sub(LINK_TOKEN, text)

    def sub(m: re.Match) -> str:
        core, tail = _split_trailing(m.group(0))
        return LINK_TOKEN + tail

    return _URL_RE.sub(sub, text)


def has_link_reference(text: str) -> bool:
    """Le texte contient-il un jeton ou une URL (donc une promesse de lien) ?"""
    return bool(_TOKEN_RE.search(text) or _URL_RE.search(text))


def finalize_reply(text: str, trusted_url: str | None, *, allowed_domains: list[str] | tuple[str, ...] = ()) -> Finalized:
    """Remplace/valide les liens de la réponse du modèle. `trusted_url` : seule URL de paiement autorisée."""
    allowed = {d.lower().removeprefix("www.") for d in allowed_domains}
    removed: list[str] = []
    token_present = bool(trusted_url and _TOKEN_RE.search(text))   # le jeton portera déjà la vraie URL
    placed = False                                                  # la vraie URL est-elle déjà dans le texte ?

    # 1. URL écrites par le modèle : seule la vraie (ou un domaine explicitement autorisé) survit, une seule fois.
    def check(m: re.Match) -> str:
        nonlocal placed
        raw = m.group(0)
        core, tail = _split_trailing(raw)
        if trusted_url and core == trusted_url:
            if placed or token_present:
                return tail            # doublon
            placed = True
            return raw
        if _domain(core).removeprefix("www.") in allowed:
            return raw
        removed.append(core)
        if trusted_url and not placed and not token_present:  # lien inventé/déformé à la place du vrai : on met le vrai
            placed = True
            return trusted_url + tail
        return tail

    out = _URL_RE.sub(check, text)

    # 2. jeton -> vraie URL (un seul exemplaire : les doublons sont retirés)
    def put_token(_: re.Match) -> str:
        nonlocal placed
        if trusted_url and not placed:
            placed = True
            return trusted_url
        return ""

    out = _TOKEN_RE.sub(put_token, out)

    # 3. un lien a été généré ce tour-ci mais le modèle ne l'a pas écrit : on l'ajoute
    if trusted_url and not placed:
        out = f"{out.rstrip()}\n\n{trusted_url}"
        placed = True

    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    # Le modèle annonçait un lien (jeton ou URL) mais le serveur n'en a aucun à fournir.
    ok = not ((_TOKEN_RE.search(text) or removed) and not trusted_url)
    return Finalized(out, ok, removed)
