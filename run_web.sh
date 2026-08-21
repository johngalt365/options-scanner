#!/bin/sh

# Always run relative to the repository, not to the caller's working directory.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
cd "$SCRIPT_DIR" || exit 1

if [ ! -f .venv/bin/activate ]; then
    echo "No se encontró .venv/bin/activate. Crea el entorno virtual antes de continuar." >&2
    exit 1
fi

# shellcheck disable=SC1091
. .venv/bin/activate

listener_pid() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -t -iTCP@127.0.0.1:8000 -sTCP:LISTEN 2>/dev/null | head -n 1
        return
    fi

    if command -v ss >/dev/null 2>&1; then
        ss -H -ltnp 'sport = :8000' 2>/dev/null |
            awk '$4 == "127.0.0.1:8000" && match($0, /pid=[0-9]+/) {
                value = substr($0, RSTART + 4, RLENGTH - 4)
                print value
                exit
            }'
    fi
}

port_is_listening() {
    python - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", 8000)) == 0 else 1)
PY
}

if port_is_listening; then
    PID=$(listener_pid)
    case "$PID" in
        ''|*[!0-9]*)
            echo "El puerto 8000 está ocupado, pero no se pudo identificar de forma segura el PID. Revísalo manualmente." >&2
            exit 1
            ;;
    esac

    echo "Deteniendo servidor anterior (PID $PID)..."
    kill -TERM "$PID" || {
        echo "No se pudo detener el proceso $PID. Revísalo manualmente." >&2
        exit 1
    }

    attempts=0
    while [ "$attempts" -lt 20 ] && port_is_listening; do
        sleep 0.1
        attempts=$((attempts + 1))
    done

    if port_is_listening; then
        echo "El puerto 8000 sigue ocupado. Revísalo manualmente." >&2
        exit 1
    fi
fi

echo "Iniciando PUT Options Scanner..."
echo "http://127.0.0.1:8000"
exec env PYTHONPATH=src python -m options_scanner.web
