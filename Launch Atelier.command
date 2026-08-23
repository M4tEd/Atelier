#!/bin/zsh

set -u
ROOT="${0:A:h}"
cd "$ROOT" || exit 1

APP_PYTHON="$ROOT/.venv/bin/python"
LOG_FILE="/tmp/atelier-launch.log"

show_error() {
    local message="$1"
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display alert \"Atelier\" message \"$message\" as critical" >/dev/null 2>&1
    else
        print -u2 -- "$message"
    fi
}

runtime_ready() {
    [[ -x "$APP_PYTHON" ]] && \
        "$APP_PYTHON" -c 'import sys; assert sys.version_info >= (3, 12); import collection_manager, PySide6, sqlalchemy, alembic' >/dev/null 2>&1
}

if ! runtime_ready; then
    BASE_PYTHON=""
    for candidate in python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && \
            "$candidate" -c 'import sys; assert sys.version_info >= (3, 12)' >/dev/null 2>&1; then
            BASE_PYTHON="$(command -v "$candidate")"
            break
        fi
    done

    if [[ -z "$BASE_PYTHON" ]]; then
        show_error "Python 3.12 or newer is required. Install it from python.org, then double-click this launcher again."
        exit 1
    fi

    if [[ -x "$APP_PYTHON" ]] && \
        ! "$APP_PYTHON" -c 'import sys; assert sys.version_info >= (3, 12)' >/dev/null 2>&1; then
        "$BASE_PYTHON" -m venv --upgrade "$ROOT/.venv" || {
            show_error "The application environment could not be upgraded."
            exit 1
        }
    elif [[ ! -x "$APP_PYTHON" ]]; then
        "$BASE_PYTHON" -m venv "$ROOT/.venv" || {
            show_error "The application environment could not be created."
            exit 1
        }
    fi

    "$APP_PYTHON" -m pip install -e "$ROOT" || {
        show_error "The one-time setup failed. Check your internet connection and try again."
        exit 1
    }
fi

nohup "$APP_PYTHON" -m collection_manager >"$LOG_FILE" 2>&1 &
disown
exit 0
