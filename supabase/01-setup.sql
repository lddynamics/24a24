-- À exécuter UNE FOIS dans Supabase > SQL Editor, AVANT « alembic upgrade head ».
-- Remplacer CHANGE_ME par un mot de passe long (lettres et chiffres uniquement, pour éviter l'encodage d'URL).
-- Même rôle qu'en local (docker/initdb/01-roles.sql) : ni superuser, ni BYPASSRLS, sinon la RLS serait ignorée.
CREATE ROLE agent24_app LOGIN PASSWORD 'CHANGE_ME'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

-- Permet au rôle propriétaire des migrations (postgres) de gérer les droits de ce rôle.
GRANT agent24_app TO postgres;
