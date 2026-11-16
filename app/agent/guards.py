"""Garde-fous de sortie : le modèle ne doit jamais annoncer un paiement reçu que la base ne confirme pas.

Défense en profondeur : le prompt et les outils disent déjà « seul check_payment_status confirme », mais un modèle
(surtout un modèle gratuit) peut l'oublier. L'état de la commande en base fait foi, pas la phrase du modèle.
"""
import re

from app.agent.text import fold

PAYMENT_NOT_CONFIRMED = (
    "Je n'ai pas encore de confirmation de votre paiement. "
    "Dès qu'il sera validé, je vous le confirme ici."
)

_CLAIMS = [
    re.compile(r"paiement\s+(a\s+(bien\s+)?ete|est|bien)\s+(bien\s+)?(confirme|recu|valide|effectue|accepte|approuve|enregistre)"),
    re.compile(r"paiement\s+(confirme|recu|valide|accepte|approuve)"),
    re.compile(r"(avons|ai)\s+(bien\s+)?recu\s+(votre\s+)?(paiement|reglement)"),
    re.compile(r"(reglement|paiement)\s+(a\s+ete|est)\s+(bien\s+)?(recu|valide|confirme|enregistre)"),
    re.compile(r"commande\s+(a\s+ete\s+|est\s+)(bien\s+)?(payee|reglee)"),
]
# Une phrase qui nie, conditionne ou annonce pour plus tard n'affirme rien.
_HEDGES = re.compile(
    r"\b(pas|aucun|aucune|non|des|dès|une fois|lorsque|quand|si|afin|pour|verifier|verification|attente)\b"
)


def claims_payment_received(reply: str) -> bool:
    for sentence in re.split(r"[.!?\n]+", fold(reply)):
        if any(c.search(sentence) for c in _CLAIMS) and not _HEDGES.search(sentence):
            return True
    return False
