# ai-actions для Windows

Экспериментальный Windows-порт. Это отдельная программа `ai-actions.exe`,
которая использует ту же логику работы с Ollama / OpenAI-совместимым API,
но не зависит от KDE и Wayland.

## Что работает

- Глобальные горячие клавиши (через Win32 `RegisterHotKey`).
- Буфер обмена (`Get-Clipboard` / `Set-Clipboard` через PowerShell).
- Действия: перевод, объяснение, краткий пересказ, исправление текста,
  вопрос модели с контекстом.
- OCR выделенной области экрана ( tkinter + `pytesseract` ).
- Окно результата с кнопкой «Скопировать».
- Окно настроек: модель, скачивание модели, язык перевода, `keep_alive`,
  и hotkeys.
- Размышления моделей (DeepSeek-R1 и т.п.) не показываются: берётся только
  финальный ответ из `message.content`.

## Что НЕ работает

- KDE-интеграция, `kdialog`, Spectacle, `wl-clipboard` — их в Windows нет.
- Автозапуск и системный трей пока не реализованы.
- OCR требует отдельно установленный **Tesseract OCR**.

## Требования

- Windows 10/11 x64.
- Запущенная [Ollama](https://ollama.com) с моделью, например `qwen3:8b`.
- Для OCR — Tesseract OCR:
  https://github.com/UB-Mannheim/tesseract/wiki
  (при установке отметь Russian language data и добавь папку с `tesseract.exe`
  в `PATH`).

## Как пользоваться

1. Запусти `ai-actions.exe`. Программа останется в фоне (окна не видно).
2. Нажми хоткей:

| Клавиши | Действие |
|---|---|
| `Ctrl+Alt+Shift+T` | Перевести выделенное |
| `Ctrl+Alt+Shift+E` | Объяснить выделенное |
| `Ctrl+Alt+Shift+M` | Кратко пересказать |
| `Ctrl+Alt+Shift+F` | Исправить текст |
| `Ctrl+Alt+Shift+O` | Выделить область → OCR → объяснить |
| `Ctrl+Alt+Shift+Q` | Спросить модель (введёшь вопрос в диалоге) |
| `Ctrl+Alt+Shift+P` | Настройки |
| `Ctrl+Alt+Shift+X` | Выход |

3. Результат появится в окне и будет уже скопирован в буфер — `Ctrl+V`.

Настройки хранятся в `%APPDATA%\ai-actions\config.json`. Там же можно
поменять модель, хост Ollama, язык перевода, `keep_alive` и сами hotkeys.
После изменения hotkeys нужно перезапустить программу.

## Сборка из исходников

Готовый `.exe` лежит в `dist/ai-actions.exe`. Чтобы пересобрать:

### На Windows

```powershell
py -3.12 -m pip install pyinstaller pillow pytesseract
py -3.12 -m PyInstaller --noconsole --onefile --clean `
  --name ai-actions --hidden-import=ai_actions ai_actions_win.py
```

### На Linux через Wine

Скрипт сам поставит Windows Python и зависимости в Wine-префикс:

```bash
./build_windows.sh
```

Результат: `dist/ai-actions.exe`.

## Ограничения

Это порт «как есть», не полноценная замена KDE-версии:

- хоткеи `Ctrl+Alt+Shift+*` выбраны, чтобы не конфликтовать с Windows;
- если комбинация занята другой программой, Windows не даст её зарегистрировать;
- OCR не заработает без Tesseract;
- нет трей-иконки — выйти можно хоткеем `Ctrl+Alt+Shift+X`.
