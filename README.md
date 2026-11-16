# Agent24 : backend (moteur d'Armistice AI)

Socle multi-tenant (PostgreSQL + RLS), webhook Messenger signé, file de traitement, agent IA avec outils,
transcription des vocaux, envoi des réponses sur Messenger. Prototype en cours : démo **Maison Kora** (vente par Messenger).

## Démarrage

```bash
cp .env.example .env            # puis renseigner META_APP_SECRET, MESSENGER_VERIFY_TOKEN, ENCRYPTION_KEY, clés IA
docker compose up -d db         # PostgreSQL 16 + rôle agent24_app + base agent24_test
pip install -r requirements.txt
alembic upgrade head            # migrations (rôle propriétaire)
uvicorn app.main:app --reload   # API (rôle applicatif, soumis à la RLS)
python -m app.worker            # worker : fait répondre l'agent
pytest                          # tests, sur la base agent24_test uniquement
```

## Choisir le modèle : tests gratuits puis Claude

Le fournisseur est choisi **d'après le nom du modèle** (`app/agent/llm.py`, `RoutingLLM`) :

| Nom du modèle | Fournisseur | Clé requise |
|---|---|---|
| `claude-…` (ex. `claude-sonnet-5-5`) | Anthropic | `ANTHROPIC_API_KEY` |
| autre (ex. `nvidia/nemotron-3-super-120b-a12b`, `meta/llama-3.3-70b-instruct`) | Fournisseur compatible OpenAI défini par `LLM_BASE_URL` (NVIDIA NIM : `https://integrate.api.nvidia.com/v1`) | `LLM_API_KEY` (à défaut `GROQ_API_KEY`, `GROQ_BASE_URL`) |

`GROQ_API_KEY` reste utilisée pour la **transcription des vocaux** (Whisper), indépendamment du modèle de l'agent.
Sur un 429, le client attend le délai `Retry-After` du fournisseur (20 s max) et réessaie deux fois avant de laisser la
file de jobs replanifier.

- **Défaut pour tout le système** : `AGENT_MODEL` dans `.env`.
- **Par client** (utile pour tester en parallèle) : `"agent": {"model": "..."}` dans le JSON du client, puis
  `set_tenant_config`. Exemple : la page de test sur un modèle gratuit, la page de démo sur Claude.
- **Basculer avant la présentation** : mettre `AGENT_MODEL=claude-sonnet-5-5` (ou le modèle du client), renseigner
  `ANTHROPIC_API_KEY`, relancer le worker. Aucun code à modifier.
- Le worker refuse de démarrer si la clé du modèle par défaut manque.

Limites des modèles gratuits : quotas stricts (tokens par minute et par jour, à vérifier sur la page de limites de ton
compte Groq) ; appels d'outils moins fiables que Claude (arguments mal formés, outil oublié) ; qualité de rédaction
inférieure. Utiles pour développer, **pas représentatifs de la démo finale** : refaire un passage complet sur Claude
avant de présenter.

## Vocaux

Si `GROQ_API_KEY` est renseignée, le worker télécharge l'audio d'un message vocal, le transcrit (Whisper, modèle
`TRANSCRIPTION_MODEL`, langue `TRANSCRIPTION_LANGUAGE`) et enregistre le texte dans le message. Le modèle le reçoit
préfixé « [Message vocal transcrit] » et confirme les détails douteux. Sans clé, ou en cas d'échec de transcription,
l'agent demande poliment d'écrire. Les données audio partent chez le fournisseur de transcription : à encadrer
(consentement, politique de confidentialité) avant toute mise en production.

## Images : lecture des photos reçues, envoi des photos produits

**Lire une image envoyée par un client.** Si `VISION_MODEL` est renseigné, le worker télécharge l'image, en garde une copie
normalisée (JPEG, 1600 px max, métadonnées GPS/EXIF retirées, format reconnu par le contenu et non par l'en-tête), la fait
décrire en français par un modèle de vision, et enregistre la description (`message_media.description`). L'agent reçoit
« [Image envoyée par le client — description automatique : …] » à la place de l'image. Conséquences voulues : n'importe quel
modèle de texte peut rester l'agent ; la description n'est calculée qu'**une fois** (l'historique rejoue du texte) ; la console
affiche l'image et « Vu par l'agent : … ». Sans `VISION_MODEL` (ou en cas de panne du fournisseur de vision), l'agent demande
poliment d'écrire, comme avant. Le texte lu dans une image est une donnée, jamais une instruction ; une capture d'écran de
paiement ne prouve rien (seul `check_payment_status` confirme). Les autocollants Messenger (👍) ne partent pas au modèle de vision.

```bash
# .env : modèle de vision (mêmes clés que l'agent par défaut : LLM_API_KEY / LLM_BASE_URL, ou GROQ_*, ou ANTHROPIC_API_KEY)
VISION_MODEL=meta/llama-3.2-11b-vision-instruct     # NVIDIA NIM ; « claude-… » pour Anthropic
python -m scripts.vision_diag photo.jpg              # vérifie le modèle de vision avec VOTRE clé avant de lancer le worker
```

**Envoyer des photos.** Outil `send_product_image` (à activer dans `tools_enabled` du client, puis `set_tenant_config`).
Le modèle donne seulement un identifiant de produit : le serveur vérifie que l'article est visible et a des photos, 3 photos
maximum par réponse, et envoie les images **avant** le texte. Aucune photo n'est envoyée si l'agent bascule vers un humain.
Les photos sont enregistrées avec la réponse (même transaction) et reprises sans doublon si un envoi échoue.
`get_catalog` indique à l'agent le nombre de photos (`photos`).

**Photos dans le catalogue.** Console → Catalogue → Modifier : ajout par sélection ou glisser-déposer (6 par produit,
réduites dans le navigateur puis revalidées et ré-encodées par le serveur), photo principale (★), suppression. Après la création
d'un produit, la console ouvre directement sa page de photos.

## Démo Maison Kora

```bash
PAGE_ACCESS_TOKEN=xxx python -m scripts.create_tenant --name "Maison Kora" --slug maison-kora --sector commerce --page-id <ID_PAGE>
python -m scripts.set_tenant_config --tenant-id <UUID> --file tenants/maison-kora.json
SEND_MODE=console python -m app.worker      # réponses affichées dans le terminal ; SEND_MODE=messenger pour envoyer vraiment
python -m scripts.send_test_webhook --page-id <ID_PAGE> --text "Vous avez une robe de soirée ?"
```

- `tenants/maison-kora.json` : marque, ton, informations officielles, catalogue. **Données fictives de démonstration.**
- Le prompt est assemblé en niveaux (`app/agent/prompt.py`) : règles de plateforme, secteur (`commerce`), marque, puis
  modules « commande » et « paiement » **ajoutés seulement si** les outils `create_order` / `create_payment_link` sont
  activés (le prompt ne promet jamais ce que l'agent ne sait pas faire).
- Outils : `get_business_info`, `get_catalog` (stock bas signalé uniquement s'il est réel), `create_order`,
  `create_payment_link`, `check_payment_status`, `escalate_to_human`.

## Console d'entreprise (`/console`)

Pages servies par le backend (Jinja2, sans framework JS), accès par mot de passe unique : `CONSOLE_PASSWORD` et
`CONSOLE_TENANT_ID` dans `.env` (console désactivée tant qu'ils sont vides), puis ouvrir `https://<hôte>/console`.

- **Tableau de bord** : chiffre d'affaires encaissé, conversion, temps de réponse médian, taux d'intervention humaine,
  encaissements des 7 derniers jours, conversations à traiter.
- **Conversations** : interface plein écran façon messagerie (liste avec recherche et filtre « À traiter », fil avec séparateurs de jour,
  actualisation toutes les 2,5 s). « Prendre la main » / « Rendre à l'agent ». Écrire (Entrée = envoyer, Maj+Entrée = retour à la ligne)
  ou enregistrer un vocal prend la main automatiquement : l'agent est mis en pause (conversation `human`) et le message part par le
  même circuit que l'agent. Un message non envoyé (fenêtre de 24 h dépassée) reste visible, marqué « non envoyé ».
- **Vocaux reçus** : l'audio est copié à la réception (les URL de Meta expirent) dans `message_media`, puis lu dans la console avec
  son lecteur et sa transcription. Format reconnu aux premiers octets, jamais d'après l'en-tête ; un contenu qui n'est pas de l'audio
  n'est ni stocké ni servi. Lecture protégée par la session (cache privé, `nosniff`, CSP stricte, requêtes `Range` prises en charge).
- **Vocaux envoyés** : bouton micro (HTTPS requis par le navigateur) -> enregistrement encodé dans le navigateur en WAV 16 bits mono
  16 kHz (aucun outil à installer, 120 s maximum) -> envoyé comme pièce jointe audio via l'API d'envoi de Messenger (WAV, M4A, AAC
  et MP4 acceptés, 25 Mo, nom de fichier ASCII). Le vocal est conservé avant l'envoi ; en cas d'échec il reste visible et marqué.
- **Commandes** : liste filtrable, détail (articles figés, livraison, paiements), « Marquer comme livrée », « Annuler » (impossible
  si déjà payée : les remboursements restent manuels).
- **Catalogue** : ajouter, modifier, masquer/afficher, supprimer ; variantes taille/couleur/stock. La table `products` est la **source
  de vérité** : l'agent (`get_catalog`, `create_order`) la lit, un paiement confirmé **décrémente le stock** (une seule fois, jamais
  sous zéro). Bouton « Importer le catalogue d'exemple » depuis le JSON du client (idempotent).

Sécurité : cookie de session signé (HttpOnly, SameSite=Lax, Secure derrière HTTPS) lié au mot de passe, 5 essais par 5 minutes,
tout le rendu auto-échappé (un message client ne peut pas injecter de HTML), endpoints d'écriture JSON ou formulaires protégés par
SameSite, toutes les requêtes passent par `tenant_session` (RLS) : la console d'un client ne voit jamais les données d'un autre.

## Publications sur la page Facebook (`/console/posts`)

Module « Présence sociale » (phase 2 du document de conception) : rédiger, programmer et publier sur la page, avec validation humaine.

- **Cycle de vie** : brouillon -> programmée -> publication en cours -> publiée (ou échec ; publiée -> supprimée de Facebook).
  **Rien ne part sans une action explicite** de la console (« Publier maintenant » ou « Programmer »).
- **Éditeur** : texte (3 000 caractères), image JPEG/PNG/GIF (8 Mo, format reconnu aux premiers octets), aperçu façon Facebook en direct,
  calendrier éditorial mensuel, liste filtrable par statut.
- **Rédaction par l'IA** : à partir d'une consigne et, au choix, d'un produit du catalogue. Le modèle ne reçoit que des faits
  vérifiés (fiche produit, informations officielles) ; les montants cités sont comparés au catalogue et signalés s'ils diffèrent ;
  le brouillon n'est jamais publié automatiquement.
- **Planificateur** : le worker vérifie toutes les 10 s (`SOCIAL_SCHEDULER_SECONDS`) les publications dues, tous clients confondus
  (table `social_schedule`, alimentée par trigger). Erreur passagère : 3 essais espacés (1, 5 puis 15 min) ; erreur définitive (jeton,
  permission, doublon) : « échec » avec explication en français et bouton « Réessayer ». Réclamation atomique : jamais deux envois
  simultanés ; une publication restée « en cours » plus de 10 min (worker arrêté) est reprise (risque rare de doublon).
- **Après publication** : lien « Voir sur Facebook », statistiques (réactions, commentaires, partages), « Retirer de Facebook ».

Prérequis côté Meta :

1. Le jeton de la **page** doit avoir `pages_manage_posts`, `pages_read_engagement` et `pages_show_list` (en plus de `pages_messaging`).
   Avec l'outil « Graph API Explorer » : choisir l'application, demander ces permissions pour votre utilisateur, puis lire le jeton de la
   page dans `GET /me/accounts`. D'après la documentation Meta (à vérifier), un jeton de page tiré d'un jeton utilisateur longue durée n'expire pas.
2. Enregistrer le jeton (chiffré) : `python -m scripts.set_page_token --tenant-id <UUID>` (le jeton est demandé sans être affiché).
3. Bouton « Tester la connexion » de la page Publications : vérifie le jeton et le nom de la page. La permission de publier n'est vérifiée
   qu'à la première publication (message d'erreur explicite si elle manque).
4. **Mode développement** : tant que l'application Meta n'est pas en mode live avec les permissions approuvées, seules les personnes qui ont
   un rôle sur l'application (administrateur, développeur, testeur) voient les publications faites par l'API. Pour une démo, ajouter les
   spectateurs comme testeurs.

## Commande et paiement FedaPay

Règles d'or (document de conception, chapitre 07) appliquées dans `app/services/orders.py` et `app/services/fedapay.py` :

- Le modèle ne fournit que des **choix** (article, taille, quantité, zone). Prix, frais de livraison et total sont
  **recalculés par le serveur** depuis le catalogue ; les paramètres inattendus (prix, tenant) sont refusés.
- Le lien de paiement est créé par le backend (transaction FedaPay + token) et **ne passe jamais par le modèle**
  (`app/agent/links.py`) : l'outil ne renvoie au modèle que le jeton `{{LIEN_PAIEMENT}}`, la vraie URL reste dans
  `ToolContext.payment_url`, et le backend la substitue juste avant l'envoi. Toute autre URL écrite par le modèle
  (inventée, tronquée, domaine déformé) est remplacée par le vrai lien ou supprimée ; un lien généré mais absent de la
  réponse est ajouté ; sans lien réel disponible, le message n'est pas envoyé et la conversation passe à un humain.
  L'historique rejoué au modèle masque les URL des anciens messages. Domaines autorisés en plus (site, réseaux) :
  `agent.allowed_link_domains` dans le JSON du client.
  Un même lien est réutilisé pendant 30 minutes (pas de transactions en double).
- Un paiement n'est « réussi » qu'après **revérification auprès de l'API FedaPay** (`GET /transactions/{id}`), y compris
  quand un webhook arrive : le webhook n'est qu'un déclencheur. Montant FedaPay différent du montant attendu = jamais « payé ».
- Un client ne peut agir que sur les commandes de **sa** conversation.
- Chaque transaction est rattachée à un **client FedaPay** (nom donné pour la livraison + e-mail de l'entreprise, là où part
  la facture ; `FEDAPAY_CUSTOMER_EMAIL` ou `payment.invoice_email` dans le JSON du client). Sans client, la page de paiement
  FedaPay ne s'ouvrait pas (constaté en bac à sable). Sans e-mail configuré, aucune transaction n'est créée.
- Webhook `POST /webhooks/fedapay` : signature `X-FEDAPAY-SIGNATURE` vérifiée (fermé par défaut), anti-rejeu 5 minutes,
  idempotent. À la confirmation, un message système + un job font prévenir le client par l'agent lui-même.

Mise en route (bac à sable) :

```bash
alembic upgrade head                                   # migration 0003 (rôle propriétaire)
# .env : FEDAPAY_SECRET_KEY, FEDAPAY_WEBHOOK_SECRET, FEDAPAY_CUSTOMER_EMAIL (FEDAPAY_ENVIRONMENT=sandbox)
python -m scripts.set_tenant_config --tenant-id <UUID> --file tenants/maison-kora.json   # active les outils de commande
# Tableau de bord FedaPay : créer un webhook  https://<votre-domaine-https>/webhooks/fedapay
#   événements : transaction.approved, transaction.declined, transaction.canceled
python -m scripts.send_test_fedapay_webhook --transaction-id <ID>    # simuler un webhook signé en local
```

À confirmer au premier essai réel : le format exact de l'en-tête de signature (la documentation publique ne le détaille pas ;
le code accepte `t=<horodatage>,s=<signature>` et la signature seule), la structure exacte de l'événement et de la réponse
de l'API. Si un vrai webhook est rejeté, les logs indiquent la forme de l'en-tête reçu (jamais le secret).
Même sans webhook, le client reçoit la confirmation : `check_payment_status` interroge l'API quand il écrit « j'ai payé ».

## Migrations

`alembic upgrade head` applique les migrations dans l'ordre, avec le rôle propriétaire :

- `0001` : tenants, channels, channel_routes, contacts, conversations, messages, RLS et droits du rôle applicatif.
- `0002` : jobs (file de traitement), agent_runs, tool_calls, statut de livraison des messages sortants.
- `0003` : commandes, paiements (FedaPay) et `payment_routes` (retrouver le client à partir de l'identifiant de transaction d'un webhook).
- `0006` : publications Facebook (`social_posts`, `social_post_media`) et `social_schedule` (planificateur multi-clients, sans RLS, alimenté par trigger).
- `0005` : `message_media` (audio des vocaux reçus et envoyés, en `bytea` : suffisant pour le prototype ; à remplacer par un stockage d'objets chiffré, avec durée de conservation, avant la production).
- `0008` : `product_images` (photos des produits, RLS) et `message_media.description` (ce que le modèle de vision a compris d'une image reçue).
- `0004` : table `products` (catalogue géré par la console, recopié depuis le JSON du client s'il existait), statut de commande « livrée ».

Ne jamais modifier une migration déjà appliquée : ajouter une nouvelle migration.

## Créer un client et tester sans Meta

```bash
PAGE_ACCESS_TOKEN=xxx python -m scripts.create_tenant --name "Vigo Services" --slug vigo-services --sector services --page-id <ID_PAGE_FACEBOOK>
python -m scripts.send_test_webhook --page-id <ID_PAGE_FACEBOOK> --text "Bonjour"
```

`tenants/vigo-services.json` est un **brouillon** tiré du site public, à faire valider par le dirigeant.

## Brancher le vrai Messenger

1. Exposer le serveur en HTTPS (tunnel Cloudflare ou ngrok en développement).
2. Dans l'app Meta : produit Messenger, webhook `https://<domaine>/webhooks/messenger`,
   jeton de vérification = `MESSENGER_VERIFY_TOKEN`, abonnement au champ `messages`.
3. Connecter la page à l'app et générer le jeton de la page.
4. En mode développement, seuls les rôles de l'app (admins, développeurs, testeurs) peuvent écrire. Pour de vrais clients il
   faut l'accès avancé `pages_messaging` (revue d'app Meta) : à lancer tôt. Vérifier les règles actuelles dans la doc Meta.

## Décisions d'architecture

- **Isolation** : `tenant_id` partout, RLS activée et forcée, clés étrangères composites `(tenant_id, id)`.
- **Deux rôles PostgreSQL** : propriétaire (migrations, scripts d'admin) et `agent24_app` (ni superuser ni BYPASSRLS).
  L'API refuse de démarrer sinon.
- **`tenant_id` vient du serveur** : déduit de l'identifiant de page via `channel_routes`, jamais d'un message client
  ni du modèle (paramètre inconnu refusé dans les outils).
- **Idempotence** : unicité `(tenant_id, channel_id, idempotency_key)`.
- **Jetons de pages** chiffrés (Fernet). La clé vit hors de la base.
- **Webhook** : signature vérifiée sur le corps brut, 200 si tout est enregistré, 500 sinon (Meta renvoie, l'idempotence évite les doublons).

## Flux d'un message

1. Webhook -> signature -> message + **job** enregistrés dans la même transaction (outbox).
2. Le worker réclame un job (`FOR UPDATE SKIP LOCKED`) ; **ordre garanti par conversation**.
3. Si le message est un vocal : téléchargement + transcription (hors transaction), texte enregistré.
4. Contexte chargé (historique, configuration du client) -> boucle modèle + outils (limite d'étapes).
5. La réponse est enregistrée *avant* l'envoi ; un échec d'envoi réessaie **sans rappeler le modèle**.
6. Échec définitif : conversation passée à un humain + message honnête au client.

Chaque appel d'outil est tracé (`agent_runs`, `tool_calls`). Les outils non activés pour un client ne sont ni exposés ni exécutables.

## Limites connues

- **Non testés ici** (tests avec faux modèle, faux expéditeur et faux service de transcription) : l'appel réel à Claude
  et à Groq, l'envoi réel vers Meta, le téléchargement d'un vrai vocal Messenger (format audio à confirmer).
- Publications : une seule image par publication (pas d'album ni de vidéo) ; pas encore de réponse aux commentaires ni de publicités ; la lecture
  des statistiques est manuelle (bouton) ; images stockées en base (`bytea`) pour le prototype ; pas de mode d'autonomie « automatique » (toute
  publication est validée par un humain).
- Console : un seul client et un mot de passe unique (démo) ; pas encore de comptes utilisateurs, de rôles ni d'actualisation par SSE
  (interrogation toutes les 2,5 s) ; pas d'images produit ni d'envoi d'images (seuls les vocaux sont envoyés) ; notification du dirigeant par e-mail à construire.
- Les clés FedaPay sont globales (`.env`) : à passer en clés chiffrées par entreprise (console) avant la production.
- L'appel à FedaPay se fait pendant la transaction de l'outil (court) : à sortir de la transaction si la charge augmente.
- Pas de validation humaine des actions sensibles (`pending_actions`).
- Garde-fou de sortie (`app/agent/guards.py`) : une réponse qui annonce un paiement reçu est remplacée si aucune commande de la
  conversation n'est « payée » en base (détection par phrases : heuristique, à compléter). Pas de contrôle équivalent sur les prix
  cités hors commande : le modèle doit s'appuyer sur `get_catalog`.
- Images : l'agent demande d'écrire (lecture d'images prévue en phase 2).
- `messages.raw` contient les événements bruts (données personnelles) : à purger/limiter.
- `media_ref` est une URL temporaire de Meta : télécharger vite (déjà le cas pour les vocaux).
- Un contact = une identité sur un canal ; la fusion entre canaux viendra avec WhatsApp.
