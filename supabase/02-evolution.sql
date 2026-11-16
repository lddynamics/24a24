-- À exécuter UNE FOIS dans Supabase > SQL Editor (en plus de 01-setup.sql).
-- Evolution API a sa propre « zone » : un schéma « evolution » et un rôle « evolution » qui ne voit rien d'autre.
-- Il n'est pas exposé par l'API Data de Supabase (seul le schéma public l'est) et ne touche pas aux tables de l'application.
-- Remplacer CHANGE_ME_EVO (lettres et chiffres uniquement).
CREATE ROLE evolution LOGIN PASSWORD 'CHANGE_ME_EVO'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
GRANT evolution TO postgres;
CREATE SCHEMA IF NOT EXISTS evolution AUTHORIZATION evolution;
ALTER ROLE evolution SET search_path = evolution;
