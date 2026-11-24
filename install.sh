#!/usr/bin/env bash
# Установка ai-actions: симлинк + .desktop-файлы с горячими клавишами KDE.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
TARGET="$BIN_DIR/ai-actions"

say() { printf '%s\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
ok() { printf '\033[32m%s\033[0m\n' "$*"; }

uninstall() {
    say "Удаляю ярлыки и симлинк…"
    rm -f "$APPS_DIR"/ai-actions-*.desktop "$TARGET"
    command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
    ok "Готово. Конфиг и логи оставил: ~/.config/ai-actions, ~/.cache/ai-actions"
    exit 0
}

[[ "${1:-}" == "--uninstall" ]] && uninstall

command -v python3 >/dev/null 2>&1 || { warn "Нет python3"; exit 1; }

mkdir -p "$BIN_DIR" "$APPS_DIR"
chmod +x "$DIR/ai_actions.py"
ln -sfn "$DIR/ai_actions.py" "$TARGET"
ok "Скрипт: $TARGET"

# Убираем устаревшие имена (были заняты другими шорткатами)
rm -f "$APPS_DIR/ai-actions-screen.desktop" "$APPS_DIR/ai-actions-ask.desktop"

# файл | шорткат | заголовок | иконка | команда
ACTIONS=(
  "translate|Meta+Shift+T|AI: перевести выделенное|accessories-dictionary|translate"
  "explain|Meta+Shift+E|AI: объяснить выделенное|help-hint|explain"
  "summarize|Meta+Shift+M|AI: кратко пересказать|view-list-details|summarize"
  "fix|Meta+Shift+F|AI: исправить текст|tools-check-spelling|fix"
  "ocr|Meta+Shift+O|AI: объяснить область экрана (OCR)|view-fullscreen|explain --screen"
  "question|Meta+Shift+Q|AI: спросить модель|system-search|ask"
)

write_desktop() {
    local file="$1" shortcut="$2" title="$3" icon="$4" cmd="$5"
    local name="ai-actions-${file}.desktop"
    cat > "$APPS_DIR/$name" <<EOF
[Desktop Entry]
Type=Application
Name=$title
Comment=Локальная модель: перевод, объяснение, OCR
Exec=$TARGET $cmd
Icon=$icon
Terminal=false
StartupNotify=false
NoDisplay=true
X-KDE-Shortcuts=$shortcut
Categories=Utility;
EOF
    chmod +x "$APPS_DIR/$name"
    printf '  %-16s %s\n' "$shortcut" "$title"
}

say ""
say "Ставлю горячие клавиши:"
for row in "${ACTIONS[@]}"; do
    IFS='|' read -r file shortcut title icon cmd <<< "$row"
    write_desktop "$file" "$shortcut" "$title" "$icon" "$cmd"
done

if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
    ok "База KDE обновлена — шорткаты активны сразу."
else
    warn "kbuildsycoca6 не найден: перелогинься, чтобы шорткаты подхватились."
fi

say ""
say "Проверка зависимостей:"
missing=()
for t in kdialog qdbus6 wl-paste wl-copy spectacle tesseract; do
    if command -v "$t" >/dev/null 2>&1; then
        printf '  ✅ %s\n' "$t"
    else
        printf '  ❌ %s\n' "$t"
        missing+=("$t")
    fi
done

if ((${#missing[@]})); then
    say ""
    warn "Не хватает: ${missing[*]}"
    say "Поставь одной командой:"
    say "  sudo apt install -y wl-clipboard tesseract-ocr tesseract-ocr-rus"
    say ""
    say "(kdialog/qdbus6/spectacle обычно уже есть в KDE.)"
fi

say ""
say "Диагностика:  ai-actions doctor"
say "Настройки:    ~/.config/ai-actions/config.json"
say "Удалить:      $DIR/install.sh --uninstall"
