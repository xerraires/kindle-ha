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

# Que Python usamos: PYTHON si lo fijas, el venv del repo (ver README §5)
# si existe, y si no, el del sistema.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x ../venv/bin/python ]; then
        PY=../venv/bin/python
    else
        PY=python3
    fi
fi

# Si faltan dependencias, mensaje claro en vez de un traceback criptico.
if ! "$PY" -c 'import PIL, requests' >/dev/null 2>&1; then
    echo "ERROR: faltan dependencias (pillow, requests) para: $PY" >&2
    echo "  Crea el venv como dice el README (§5):" >&2
    echo "    python3 -m venv ../venv && ../venv/bin/pip install pillow requests" >&2
    echo "  o instala los paquetes del sistema (p. ej. python3-pil python3-requests)." >&2
    exit 1
fi

export PORT="${PORT:-8090}"
export DATA_TTL="${DATA_TTL:-300}"

exec "$PY" kindle-ha.py
