#!/usr/bin/env bash
# Сборка Windows .exe из этого репозитория через Wine (Linux).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

WINE_PY="${WINE_PY:-$HOME/.wine/drive_c/Python312/python.exe}"
INSTALLER="/tmp/opencode/python-3.12.9-amd64.exe"

if [[ ! -x "$WINE_PY" ]]; then
    command -v wine >/dev/null 2>&1 || {
        echo "Нужен wine: sudo apt install -y wine" >&2
        exit 1
    }
    echo "Ставлю Windows Python 3.12 в Wine..."
    mkdir -p /tmp/opencode
    curl -fsSL --retry 2 \
        -o "$INSTALLER" \
        https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe
    wine "$INSTALLER" /quiet InstallAllUsers=0 PrependPath=0 \
        Include_pip=1 Include_test=0 TargetDir=C:\\Python312
fi

echo "Ставлю зависимости..."
"$WINE_PY" -m pip install --disable-pip-version-check --quiet --upgrade \
    pip pyinstaller pillow pytesseract

echo "Собираю exe..."
"$WINE_PY" -m PyInstaller \
    --noconsole --onefile --clean \
    --name ai-actions \
    --hidden-import=ai_actions \
    ai_actions_win.py

echo ""
echo "Готово: $DIR/dist/ai-actions.exe"
echo "Инструкция: $DIR/WINDOWS.md"
