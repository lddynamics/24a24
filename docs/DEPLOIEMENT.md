# Mise en ligne du prototype (gratuit) : GitHub + Supabase + Render

Architecture : **Supabase** = base PostgreSQL · **Render** = un seul service web gratuit (API + console + worker dans le même
processus, variable `EMBEDDED_WORKER=true`) · **GitHub Actions** = tests, réveil du service, administration à distance.
Rien ne tourne plus sur votre PC : tout se fait depuis le navigateur.

## Limites du gratuit à connaître avant la présentation

| Service | Limite | Conséquence / parade |
|---|---|---|
| Render gratuit | Service endormi après 15 min sans requête ; réveil ≈ 30-60 s. Le worker dort aussi. | Workflow `keep-alive` + **UptimeRobot** (ping `/health` toutes les 5 min). Le jour J, ouvrir `/health` 2 minutes avant. |
| Render gratuit | 512 Mo de RAM, pas de disque persistant | Suffisant pour l'application : images/vocaux déjà en base (`bytea`). Evolution est plus serré : voir section 8. |
| Supabase gratuit | Projet **mis en pause après 7 jours d'inactivité** ; 500 Mo de base | Se connecter au tableau de bord avant la démo ; le ping `/health` fait une requête SQL, ce qui compte comme activité. |
| Modèles gratuits (NVIDIA…) | Quotas, outils moins fiables | Basculer sur Claude avant la démo (voir README). |

## 1. GitHub

1. Créez un dépôt **privé** (le fichier `tenants/vigo-services.json` contient les données réelles d'un client).
2. Envoyez le projet (le `.gitignore` exclut `.env`, `qrcode.png` et les `.zip`) :
   ```bash
   git init -b main && git add . && git commit -m "Prototype Agent24"
   git remote add origin https://github.com/<vous>/<depot>.git && git push -u origin main
   ```
   Sans Git sur le PC : « Add file > Upload files » sur github.com.
3. L'onglet **Actions** doit montrer le workflow `CI` en vert (372 tests).

## 2. Supabase (base de données)

1. supabase.com > New project. Choisissez une région proche (Europe) ; notez le mot de passe de la base.
2. **SQL Editor** : collez `supabase/01-setup.sql` (changez `CHANGE_ME`) et exécutez. Cela crée le rôle `agent24_app`
   (sans superuser, sans `BYPASSRLS` : indispensable à l'isolation entre clients ; l'application refuse de démarrer sinon).
3. **Connect > Session pooler** : copiez l'hôte (`aws-0-<région>.pooler.supabase.com`) et la référence du projet `<ref>`.
   > Utilisez le **pooler** et non « Direct connection » : la connexion directe est en IPv6 uniquement, et Render gratuit
   > n'accède qu'en IPv4. Port **5432** (mode « session »).
4. Construisez les deux URL (si le mot de passe contient `@ : / # %`, encodez-les, ou choisissez un mot de passe alphanumérique) :
   ```
   DATABASE_URL            = postgresql+psycopg://agent24_app.<ref>:<MDP_APP>@<hôte-pooler>:5432/postgres
   MIGRATIONS_DATABASE_URL = postgresql+psycopg://postgres.<ref>:<MDP_BASE>@<hôte-pooler>:5432/postgres
   ```
5. Petit pool de connexions côté application (5 max) : le pooler gratuit est partagé avec Evolution. Si les logs disent « max clients reached », passez `DATABASE_URL` sur le pooler « Transaction » (port 6543) ; seuls `MIGRATIONS_DATABASE_URL` et Evolution doivent rester en mode session.
6. La migration `0009` retire d'elle-même l'accès des rôles `anon`/`authenticated` (API Data de Supabase) aux tables : sans cela,
   la table `channels` (jetons chiffrés) serait lisible depuis Internet avec la clé publique du projet.

## 3. Secrets à générer

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # ENCRYPTION_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"                                 # MESSENGER_VERIFY_TOKEN, EVOLUTION_*...
```
(Sans Python : n'importe quel générateur de mots de passe de 40 caractères convient pour les jetons ; pour `ENCRYPTION_KEY`,
la clé doit être une clé Fernet valide : base64 de 32 octets, 44 caractères se terminant par `=`.)
**Conservez `ENCRYPTION_KEY`** : la perdre rend les jetons déjà enregistrés illisibles.

## 4. Render

1. render.com > **New > Blueprint** > choisissez le dépôt : `render.yaml` crée le service `agent24`.
2. Renseignez les variables « à saisir » : `DATABASE_URL`, `MIGRATIONS_DATABASE_URL`, `ENCRYPTION_KEY`, `META_APP_SECRET`,
   `MESSENGER_VERIFY_TOKEN`, `CONSOLE_PASSWORD`, `LLM_API_KEY` (ou `ANTHROPIC_API_KEY` + `AGENT_MODEL=claude-…`), `GROQ_API_KEY`,
   clés FedaPay. `CONSOLE_TENANT_ID` : laissez vide pour l'instant.
3. Au premier démarrage, `alembic upgrade head` crée les tables sur Supabase, puis l'API démarre.
   Vérifiez : `https://<service>.onrender.com/health` → `{"status":"ok"}`. Les logs doivent contenir « worker intégré démarré ».
4. Si le démarrage échoue : « Le rôle agent24_app doit exister » → étape 2 de Supabase oubliée ; erreur d'authentification →
   mauvais `<ref>` dans le nom d'utilisateur ou mot de passe mal encodé.

## 5. Créer le client (WhatsApp : suivre la section 8 en parallèle) et le relier à la console (depuis le navigateur)

Dépôt GitHub > Settings > Secrets and variables > Actions : ajoutez les **secrets** `MIGRATIONS_DATABASE_URL`, `ENCRYPTION_KEY`
(identique à Render) et `PAGE_ACCESS_TOKEN`, et la **variable** `APP_URL` (`https://<service>.onrender.com`, active le keep-alive).

1. Actions > **Admin** > Run workflow : `create_tenant` avec nom, slug, secteur, identifiant de page → le journal affiche `tenant_id=<UUID>`.
2. Relancez **Admin** : `set_config` avec ce `tenant_id` et `tenants/vigo-services.json` (ou `maison-kora.json`).
3. Sur Render, saisissez `CONSOLE_TENANT_ID=<UUID>` → redémarrage automatique → console sur `https://<service>.onrender.com/console`.

## 6. Branchements externes (URLs publiques)

- **Messenger (Meta for Developers)** → Webhooks : URL `https://<service>.onrender.com/webhooks/messenger`, jeton = `MESSENGER_VERIFY_TOKEN`.
- **FedaPay** → webhook `https://<service>.onrender.com/webhooks/fedapay`, secret = `FEDAPAY_WEBHOOK_SECRET` ;
  `FEDAPAY_CALLBACK_URL` si vous avez une page de retour.
- Passez `SEND_MODE=messenger` (déjà le cas dans `render.yaml`) pour envoyer réellement.
- Le micro de la console exige HTTPS : c'est le cas sur `onrender.com`.

## 7. Rester éveillé pendant la semaine

1. Variable `APP_URL` renseignée → `keep-alive.yml` pingue toutes les 10 min (GitHub peut retarder ces tâches).
2. Ajoutez **UptimeRobot** (gratuit) : monitor HTTP sur `https://<service>.onrender.com/health`, intervalle 5 min.
3. Si un message arrive pendant l'endormissement, Meta réessaie, et l'idempotence évite les doublons : pas de perte, seulement du retard.
4. Plan gratuit Render : 750 h/mois, soit un seul service allumé en continu : ne créez pas d'autre service gratuit dans le même compte.

## 8. WhatsApp (Evolution API) sur Render : à faire AVANT les sections 5 et 6 si c'est ce que vous présentez

`render.yaml` crée déjà trois choses : le service `agent24`, le service **`evolution`** (image officielle `v2.3.7`, via `evolution/Dockerfile`)
et un **Key Value gratuit** `evolution-redis`. Evolution utilise Supabase comme base (schéma `evolution`, rôle `evolution`, sans accès à vos tables).

Pourquoi un Key Value : Evolution garde les identifiants de la session dans Postgres, mais les **clés de chiffrement** de WhatsApp dans Redis ou sur
disque. Render gratuit n'a pas de disque : à chaque endormissement du service, tout disque local serait perdu et il faudrait **rescanner le QR**.
Le Key Value, lui, reste allumé quand le service dort.

1. **Supabase > SQL Editor** : exécutez `supabase/02-evolution.sql` (changez `CHANGE_ME_EVO`).
2. Au déploiement du Blueprint, saisissez pour le service `evolution` :
   - `AUTHENTICATION_API_KEY` : une longue chaîne aléatoire (la même valeur ira dans `EVOLUTION_API_KEY` du service `agent24`) ;
   - `DATABASE_CONNECTION_URI` : `postgresql://evolution.<ref>:<MDP_EVO>@<hôte-pooler>:5432/postgres?schema=evolution&connection_limit=4`
     (pooler en mode **session**, port 5432 : Prisma, l'outil de migration d'Evolution, l'exige) ;
   - `SERVER_URL` : laissez vide au premier passage.
3. Quand `evolution` est « Live » : copiez son URL publique, collez-la dans `SERVER_URL` (service `evolution`) et dans `EVOLUTION_API_URL` (service `agent24`).
   Sur `agent24`, ajoutez aussi `EVOLUTION_API_KEY` et `EVOLUTION_WEBHOOK_SECRET` (autre chaîne aléatoire). Vérifiez `https://<evolution>.onrender.com/` : une réponse JSON.
4. GitHub > Settings > Secrets : ajoutez `EVOLUTION_API_KEY` et `EVOLUTION_WEBHOOK_SECRET` (mêmes valeurs) ; Variables : `EVOLUTION_URL`.
5. Si le client n'existe pas encore dans cette nouvelle base : Actions > **Admin** > `create_tenant` (pour un test 100 % WhatsApp, un `page_id` fictif
   comme `demo-page` convient), puis `set_config` avec `tenants/vigo-services.json`. Notez le `tenant_id`, mettez-le dans `CONSOLE_TENANT_ID` sur Render.
6. Actions > **Admin** > `evolution_instance`, `instance = agent24`, et `number` = **le numéro dédié à la démo** avec indicatif, chiffres seuls
   (ex. `22901XXXXXXXX`). Le journal affiche un **code d'appairage** : sur le téléphone, WhatsApp > Paramètres > Appareils connectés >
   Connecter un appareil > *Connecter avec un numéro de téléphone*, puis saisissez le code.
   *Plan B QR code* : laissez `number` vide, téléchargez l'artefact `qrcode` du run et scannez-le (60 s) ; ou ouvrez `https://<evolution>.onrender.com/manager`
   (connexion avec `AUTHENTICATION_API_KEY`) pour afficher un QR en direct.
7. Actions > **Admin** > `add_whatsapp_channel` avec le `tenant_id` et `instance = agent24` : relie ce numéro au client.
8. Envoyez un message WhatsApp au numéro : il doit apparaître dans la console, et l'agent répond.

**Ce qui peut mal tourner (à tester dès aujourd'hui, pas la veille)**

| Risque | Détail | Parade |
|---|---|---|
| Mémoire | Evolution + Baileys sont gourmands ; le plan gratuit est à 512 Mo (la même taille que le plan payant « Starter » recommandé par Render pour Evolution). `NODE_OPTIONS` limite déjà le tas. | Surveillez Render > Metrics pendant un test d'une heure. Redémarrages en boucle = pas assez de RAM : voir « plan B ». |
| Heures gratuites | 750 h par mois pour **tout le compte** : deux services gardés éveillés 24 h/24 consomment ~1 490 h par mois. | Suffisant pour une semaine de démo (~240 h) ; ne laissez pas ces deux services tourner tout le mois. |
| Endormissement | Evolution endormi = WhatsApp déconnecté. Le message envoyé pendant ce temps est généralement livré à la reconnexion, mais le client attend. | `keep-alive` + UptimeRobot sur **les deux** URL (`/health` et `/`). |
| Réveil de l'application | Si `agent24` dort, le webhook d'Evolution attend son réveil (30-60 s). | Même parade : garder les deux éveillés. |
| Key Value gratuit | Sans persistance : si Render le redémarre (maintenance), les clés sont perdues et le QR doit être refait. | Prévoir 5 min avant la démo pour re-lier si besoin ; vérifier l'état dans `/manager`. |
| Première migration | Je n'ai pas pu exécuter l'image Evolution ici (pas de Docker) : le démarrage sur schéma Supabase n'est **pas testé**. | Regardez les logs `evolution` au premier déploiement (erreur Prisma `P1001`/`P1010` = URL, mot de passe ou rôle). |
| Numéro | Ce mode est contraire aux conditions de WhatsApp : risque de bannissement. | Numéro dédié à la démo, jamais le numéro principal du client. |

**Plan B si Render ne tient pas Evolution** : une VM gratuite (Oracle Cloud « Always Free », 1 Go+ de RAM) avec `docker-compose.evolution.yml`
(changer `SERVER_URL` et ouvrir le port) ; l'application reste sur Render et pointe `EVOLUTION_API_URL` vers cette VM.

## 9. Avant le jour J

- [ ] `AGENT_MODEL=claude-sonnet-5-5` + `ANTHROPIC_API_KEY` (ou garder le modèle gratuit si quotas suffisants) et un passage complet de la démo.
- [ ] `/health` OK, console accessible, un message Messenger de bout en bout, un paiement FedaPay bac à sable.
- [ ] Projet Supabase actif (non en pause), UptimeRobot au vert.
- [ ] Mot de passe de console long ; `FEDAPAY_ENVIRONMENT=sandbox` tant que la démo est fictive.

## Fichiers ajoutés ou modifiés

| Fichier | Rôle |
|---|---|
| `render.yaml` | Services Render gratuits : application (migrations + API + worker intégré), Evolution API, Key Value |
| `evolution/Dockerfile`, `supabase/02-evolution.sql` | Image Evolution figée en 2.3.7 ; schéma et rôle dédiés sur Supabase |
| `scripts/setup_evolution_instance.py` | Rendu autonome (sans .env complet) + code d'appairage, lancé par le workflow Admin |
| `supabase/01-setup.sql` | Création du rôle applicatif sur Supabase |
| `migrations/versions/0009_supabase_lockdown.py` | Ferme l'API Data de Supabase aux tables de l'application |
| `app/main.py`, `app/worker.py`, `app/config.py` | Worker intégré optionnel (`EMBEDDED_WORKER`) ; boucle résistante aux coupures de base |
| `app/db.py`, `migrations/env.py` | Compatible pooler Supabase (`prepare_threshold=None`), petit pool de connexions |
| `.github/workflows/ci.yml` · `keep-alive.yml` · `admin.yml` | Tests · réveil · création de client sans PC |
| `requirements.txt` / `requirements-dev.txt` | `pytest` sorti de la production |
