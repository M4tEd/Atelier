#!/usr/bin/env bash
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
APP_PYTHON="$ROOT/.venv/bin/python"
LOG_FILE="/tmp/atelier-launch.log"

show_error() {
    local message="$1"
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title="Atelier" --text="$message" 2>/dev/null
    elif command -v kdialog >/dev/null 2>&1; then
        kdialog --error "$message" 2>/dev/null
    else
        echo "$message" >&2
    fi
}

runtime_ready() {
    [[ -x "$APP_PYTHON" ]] && \
        "$APP_PYTHON" -c 'import sys; assert sys.version_info >= (3, 12); import collection_manager, PySide6, sqlalchemy, alembic' >/dev/null 2>&1
}

if ! runtime_ready; then
    BASE_PYTHON=""
    for candidate in python3.12 python3.14 python3; do
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
