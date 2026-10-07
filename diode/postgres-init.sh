#!/bin/sh
# Creates the Diode and Hydra databases/users on first start of diode-postgres.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<EOSQL
CREATE USER diode WITH PASSWORD '${DIODE_POSTGRES_PASSWORD}';
CREATE DATABASE diode OWNER diode;
CREATE USER hydra WITH PASSWORD '${HYDRA_POSTGRES_PASSWORD}';
CREATE DATABASE hydra OWNER hydra;
EOSQL
