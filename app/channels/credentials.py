"""Identifiants d'un canal, tels que stockés (chiffrés) dans `channels.credentials_encrypted`.

- Messenger : le jeton d'accès de la page, tel quel.
- WhatsApp  : un petit JSON `{"access_token": ..., "phone_number_id": ...}`. L'API d'envoi WhatsApp exige l'identifiant du
  numéro dans l'URL ; il voyage avec le jeton pour que l'interface des expéditeurs (`get_sender(type, credentials)`) reste
  identique pour tous les canaux. `phone_number_id` est aussi `channels.external_id` : `add_channel` écrit les deux d'un coup.
"""
import json


class CredentialsError(ValueError):
    pass


def encode(channel_type: str, access_token: str, external_id: str) -> str:
    if channel_type == "whatsapp":
        return json.dumps({"access_token": access_token, "phone_number_id": external_id})
    return access_token


def whatsapp_credentials(secret: str) -> tuple[str, str]:
    """(access_token, phone_number_id) depuis les identifiants déchiffrés d'un canal WhatsApp."""
    try:
        data = json.loads(secret)
        token, phone_number_id = str(data["access_token"]), str(data["phone_number_id"])
    except (ValueError, KeyError, TypeError) as e:
        raise CredentialsError("identifiants WhatsApp invalides (JSON access_token + phone_number_id attendu)") from e
    if not token or not phone_number_id:
        raise CredentialsError("identifiants WhatsApp incomplets")
    return token, phone_number_id
