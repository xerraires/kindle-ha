#!/bin/bash
# Arranca el servidor de salvapantallas del Kindle.
# Los datos de conexion NO van aqui: leelos de .env o exporta las variables.
set -e
cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

if [ -z "${HA_TOKEN:-}" ]; then
    echo "ERROR: define HA_TOKEN (y HA_URL) en .env o en el entorno." >&2
    exit 1
fi

export PORT="${PORT:-8090}"
export DATA_TTL="${DATA_TTL:-300}"

exec python3 kindle-ha.py
