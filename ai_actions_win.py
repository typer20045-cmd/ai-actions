#!/usr/bin/env python3
"""
Windows-порт ai-actions.

Оставляет общую логику работы с Ollama / OpenAI-совместимым API из ai_actions.py,
но вместо KDE/Wayland использует:
  * глобальные хоткеи через Win32 RegisterHotKey;
  * буфер через PowerShell;
  * окна и выбор области экрана через tkinter;
  * OCR через pytesseract + Tesseract OCR.

Сборка: см. build_windows.sh / WINDOWS.md.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

import pytesseract
from PIL import ImageGrab, ImageTk
from tkinter import ttk
import tkinter as tk
from tkinter import messagebox, simpledialog

import ai_actions as core

# ------------------------------------------------------------------ пути Windows

APPDATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))

# Основной модуль ai_actions использует XDG-пути; под Windows перенаправляем их.
core.CONFIG_DIR = APPDATA / "ai-actions"
core.CONFIG_PATH = core.CONFIG_DIR / "config.json"
core.CACHE_DIR = LOCALAPPDATA / "ai-actions"
core.LOG_PATH = core.CACHE_DIR / "log.txt"

# ------------------------------------------------------------------ хоткеи

DEFAULT_HOTKEYS = {
    "translate": "Ctrl+Alt+Shift+T",
    "explain": "Ctrl+Alt+Shift+E",
    "summarize": "Ctrl+Alt+Shift+M",
    "fix": "Ctrl+Alt+Shift+F",
    "ocr": "Ctrl+Alt+Shift+O",
    "ask": "Ctrl+Alt+Shift+Q",
    "settings": "Ctrl+Alt+Shift+P",
    "exit": "Ctrl+Alt+Shift+X",
}

ACTION_TITLES = {
    "translate": "Перевод",
    "explain": "Объяснение",
    "summarize": "Кратко",
    "fix": "Исправить текст",
    "ocr": "OCR области экрана",
    "ask": "Спросить модель",
    "settings": "Настройки",
    "exit": "Выход",
}

RECOMMENDED_MODELS = [
    "qwen3:8b", "qwen3:4b", "qwen3:1.7b", "llama3.2:3b",
    "gemma3:4b", "mistral:7b", "phi4-mini:3.8b", "deepseek-r1:8b",
]

MODIFIERS = {"Alt": 0x0001, "Ctrl": 0x0002, "Shift": 0x0004, "Win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

VK_EXTRA = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "Space": 0x20, "Enter": 0x0D, "Esc": 0x1B,
}


def parse_hotkey(combo: str) -> tuple[int, int] | None:
    """Возвращает (модификаторы, VK-код) для строки вида Ctrl+Alt+Shift+T."""
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = None
    for part in parts:
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif part in VK_EXTRA:
            key = VK_EXTRA[part]
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        else:
            return None
    if not mods or key is None:
        return None
    return mods, key


# ------------------------------------------------------------------ буфер Windows


def clipboard_get() -> str:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (result.stdout or "").strip("\r\n")
    except Exception:
        return ""


def clipboard_set(text: str) -> None:
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.communicate(text.encode("utf-8"))
    except Exception as exc:
        core.log(f"clipboard error: {exc}")


# ------------------------------------------------------------------ поток хоткеев


class HotkeyThread(threading.Thread):
    def __init__(self, hotkeys: dict[str, str], events: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.hotkeys = hotkeys
        self.events = events

    def run(self) -> None:
        user32 = ctypes.windll.user32
        ids: dict[int, str] = {}
        for index, (action, combo) in enumerate(self.hotkeys.items(), 1):
            parsed = parse_hotkey(combo)
            if not parsed:
                core.log(f"bad hotkey {action}: {combo}")
                continue
            mods, vk = parsed
            if user32.RegisterHotKey(None, index, mods | MOD_NOREPEAT, vk):
                ids[index] = action
            else:
                core.log(f"hotkey busy {action}: {combo}")

        msg = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if result <= 0:
                break
            if msg.message == WM_HOTKEY:
                action = ids.get(int(msg.wParam))
                if action:
                    self.events.put(("hotkey", action))
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        for index in ids:
            user32.UnregisterHotKey(None, index)


# ------------------------------------------------------------------ главное окно


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.events: queue.Queue = queue.Queue()
        self.cfg = core.load_config()
        self.hotkeys = dict(DEFAULT_HOTKEYS)
        self.hotkeys.update(self.cfg.get("hotkeys") or {})
        self._settings_window = None
        self._download_btn = None
        self._download_status = None
        self._model_combo = None

        self.thread = HotkeyThread(self.hotkeys, self.events)
        self.thread.start()
        self.root.after(80, self._poll)

        # Тихий статус: без трея, но процесс виден в Dispetcher.
        core.log(f"windows start model={self.cfg.get('model')}")

    # -------------------------------------------------------------- события

    def _poll(self) -> None:
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle(item)
        self.root.after(80, self._poll)

    def _handle(self, item: tuple) -> None:
        kind = item[0]
        if kind == "hotkey":
            self._on_hotkey(item[1])
        elif kind == "ask":
            self._do_ask_dialog(item[1], item[2])
        elif kind == "region":
            self._do_region_overlay(item[1], item[2])
        elif kind == "result":
            self._show_result(item[1], item[2])
        elif kind == "error":
            messagebox.showerror("AI Actions", item[1])
        elif kind == "info":
            messagebox.showinfo("AI Actions", item[1])
        elif kind == "status":
            if self._download_status is not None:
                self._download_status.config(text=item[1])
        elif kind == "download_done":
            if self._download_btn is not None:
                self._download_btn.config(state="normal")
            if self._download_status is not None:
                self._download_status.config(text="")
        elif kind == "model_list":
            if self._model_combo is not None:
                self._model_combo["values"] = item[1]

    def _on_hotkey(self, action: str) -> None:
        if action == "exit":
            self.quit()
        elif action == "settings":
            self.open_settings()
        else:
            threading.Thread(target=self._run_action, args=(action,), daemon=True).start()

    # -------------------------------------------------------------- действия

    def _request_ask(self) -> str:
        event = threading.Event()
        answer: dict[str, str] = {}
        self.events.put(("ask", event, answer))
        event.wait(120)
        return answer.get("value", "")

    def _request_region(self) -> object | None:
        event = threading.Event()
        result: dict[str, object] = {}
        self.events.put(("region", event, result))
        event.wait(300)
        return result.get("image")

    def _run_action(self, action: str) -> None:
        cfg = core.load_config()
        was_ocr = False
        truncated = False

        if action == "ocr":
            image = self._request_region()
            if image is None:
                return
            try:
                text = pytesseract.image_to_string(
                    image, lang=cfg.get("ocr_lang", "rus+eng")
                ).strip()
            except Exception as exc:
                self.events.put(("error", f"Tesseract OCR недоступен:\n{exc}\n\n"
                                          "Установи Tesseract OCR и добавь его в PATH."))
                return
            was_ocr = True
        else:
            text = clipboard_get()

        if action == "ask":
            question = self._request_ask()
            if not question:
                return
            prompt = (f"Контекст:\n{text}\n\nВопрос: {question}"
                      if text.strip() else question)
        else:
            limit = int(cfg.get("max_input_chars", 6000))
            if len(text) > limit:
                text = text[:limit]
                truncated = True
            if not text.strip():
                self.events.put(("error", "Буфер пуст. Сначала скопируй текст."))
                return
            args = argparse.Namespace(
                prompt=None, image=False, screen=False, full=False, window=False
            )
            prompt = core.build_prompt(action, text, cfg, args)

        try:
            result = core.call_llm(prompt, cfg)
        except SystemExit:
            self.events.put(("error", "Не удалось получить ответ от модели. "
                                      "Проверь Ollama и настройки (ai-actions doctor)."))
            return
        except Exception as exc:
            self.events.put(("error", f"Ошибка модели: {exc}"))
            return

        if not result:
            self.events.put(("error", "Модель вернула пустой ответ."))
            return

        clipboard_set(result)
        shown = core.strip_markdown(result)
        if truncated:
            shown += f"\n\n— — —\n[входной текст обрезан до {limit} символов]"
        core.log(f"ok action={action} chars={len(text)} ocr={was_ocr}")
        self.events.put(("result", ACTION_TITLES.get(action, action), shown))

    # -------------------------------------------------------------- диалоги в UI

    def _do_ask_dialog(self, event: threading.Event, answer: dict[str, str]) -> None:
        value = simpledialog.askstring(
            "AI Actions", "Что спросить у модели?", parent=self.root
        )
        answer["value"] = value or ""
        event.set()

    def _do_region_overlay(self, event: threading.Event, result: dict[str, object]) -> None:
        user32 = ctypes.windll.user32
        x = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        y = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        w = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        h = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        if not w or not h:
            event.set()
            return

        image = ImageGrab.grab(all_screens=True)
        top = tk.Toplevel(self.root)
        top.geometry(f"{w}x{h}+{x}+{y}")
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        canvas = tk.Canvas(top, width=w, height=h, highlightthickness=0, cursor="cross")
        canvas.pack()
        photo = ImageTk.PhotoImage(image)
        canvas.image = photo  # держим ссылку
        canvas.create_image(0, 0, anchor="nw", image=photo)
        rect = canvas.create_rectangle(0, 0, 0, 0, outline="#ff5252", width=2)
        state: dict[str, int] = {}

        def on_press(e: tk.Event) -> None:
            state["x1"], state["y1"] = e.x, e.y
            canvas.coords(rect, e.x, e.y, e.x, e.y)

        def on_move(e: tk.Event) -> None:
            if "x1" not in state:
                return
            canvas.coords(rect, state["x1"], state["y1"], e.x, e.y)

        def on_release(e: tk.Event) -> None:
            if "x1" not in state:
                return
            x1, y1 = state["x1"], state["y1"]
            left, right = min(x1, e.x), max(x1, e.x)
            upper, lower = min(y1, e.y), max(y1, e.y)
            top.destroy()
            if right - left < 5 or lower - upper < 5:
                event.set()
                return
            result["image"] = image.crop((left, upper, right, lower))
            event.set()

        def on_cancel(_: tk.Event) -> None:
            top.destroy()
            event.set()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_move)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", on_cancel)
        top.focus_force()

    def _show_result(self, title: str, text: str) -> None:
        win = tk.Toplevel(self.root)
        win.title(f"AI · {title}")
        win.geometry("780x540")
        win.attributes("-topmost", True)

        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)
        text_widget = tk.Text(frame, wrap="word", font=("Consolas", 10))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text_widget.yview)
        text_widget.configure(yscrollcommand=scroll.set)
        text_widget.insert("1.0", text)
        text_widget.configure(state="disabled")
        text_widget.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        buttons = ttk.Frame(win, padding=(8, 0, 8, 8))
        buttons.pack(fill="x")

        def copy() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            clipboard_set(text)

        ttk.Button(buttons, text="Скопировать", command=copy).pack(side="left")
        ttk.Button(buttons, text="Закрыть", command=win.destroy).pack(side="right")
        win.focus_force()

    # -------------------------------------------------------------- настройки

    def _fetch_models(self, host: str) -> None:
        """Подтягивает список моделей из Ollama и рекомендуемые."""
        host = host.strip().rstrip("/") or "http://127.0.0.1:11434"

        def work() -> None:
            installed: list[str] = []
            try:
                req = urllib.request.Request(host + "/api/tags", method="GET")
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                installed = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            except Exception as exc:
                core.log(f"model list error: {exc}")
            names = installed + [m for m in RECOMMENDED_MODELS if m not in installed]
            self.events.put(("model_list", names))

        threading.Thread(target=work, daemon=True).start()

    def open_settings(self) -> None:
        if self._settings_window is not None:
            try:
                self._settings_window.lift()
                return
            except tk.TclError:
                pass

        cfg = core.load_config()
        win = tk.Toplevel(self.root)
        self._settings_window = win
        win.title("AI Actions — настройки")
        win.geometry("640x560")
        win.protocol("WM_DELETE_WINDOW", self._close_settings)

        notebook = ttk.Notebook(win)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        # --- основное
        main = ttk.Frame(notebook, padding=10)
        notebook.add(main, text="Основное")

        ttk.Label(main, text="Модель:").grid(row=0, column=0, sticky="w", pady=4)
        model_var = tk.StringVar(value=cfg.get("model", "qwen3:8b"))
        model_combo = ttk.Combobox(main, textvariable=model_var, width=34)
        model_combo.set(model_var.get())
        model_combo.grid(row=0, column=1, sticky="ew", pady=4)
        self._model_combo = model_combo

        ttk.Label(main, text="Хост Ollama:").grid(row=1, column=0, sticky="w", pady=4)
        host_var = tk.StringVar(value=cfg.get("host", "http://127.0.0.1:11434"))
        ttk.Entry(main, textvariable=host_var, width=36).grid(
            row=1, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            main, text="Обновить список",
            command=lambda: self._fetch_models(host_var.get()),
        ).grid(row=0, column=2, padx=4, sticky="w")
        self._fetch_models(host_var.get())

        ttk.Label(main, text="Язык перевода:").grid(row=2, column=0, sticky="w", pady=4)
        lang_var = tk.StringVar(value=cfg.get("translate_target", "auto"))
        langs = [("auto", "Авто (RU ⇄ EN)")] + [
            (code, name.capitalize()) for code, name in core.LANGUAGE_NAMES.items()
        ]
        lang_combo = ttk.Combobox(
            main, textvariable=lang_var,
            values=[label for _, label in langs], state="readonly", width=33,
        )
        code_to_label = dict(langs)
        lang_combo.set(code_to_label.get(lang_var.get(), "Авто (RU ⇄ EN)"))
        lang_combo.grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(main, text="Держать модель:").grid(row=3, column=0, sticky="w", pady=4)
        keep_var = tk.StringVar(value=str(cfg.get("keep_alive", "10m")))
        keep_combo = ttk.Combobox(
            main, textvariable=keep_var,
            values=["5m", "10m", "30m", "1h", "-1", "0"], state="readonly", width=33,
        )
        keep_combo.grid(row=3, column=1, sticky="ew", pady=4)
        main.columnconfigure(1, weight=1)

        def download() -> None:
            model = model_var.get().strip()
            host = host_var.get().strip().rstrip("/")
            if not model:
                self.events.put(("error", "Укажи имя модели."))
                return
            if not host:
                self.events.put(("error", "Укажи хост Ollama."))
                return
            if self._download_btn is not None:
                self._download_btn.config(state="disabled")
            self.events.put(("status", f"Подключаюсь к {host}…"))

            def work() -> None:
                try:
                    body = json.dumps({"model": model, "stream": True}).encode()
                    req = urllib.request.Request(
                        host + "/api/pull",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=3600) as resp:
                        for raw in resp:
                            raw = raw.strip()
                            if not raw:
                                continue
                            obj = json.loads(raw)
                            if obj.get("error"):
                                raise RuntimeError(str(obj["error"]))
                            status = obj.get("status") or ""
                            total = int(obj.get("total") or 0)
                            completed = int(obj.get("completed") or 0)
                            if total:
                                percent = int(completed * 100 / total)
                                self.events.put(("status", f"{status} — {percent}%"))
                            elif status:
                                self.events.put(("status", status))
                    self.events.put(("info", f"Модель {model} скачана."))
                except Exception as exc:
                    self.events.put((
                        "error",
                        f"Не удалось скачать модель:\n{exc}\n\n"
                        f"Проверь хост Ollama: {host}\n"
                        "Если Ollama в другой машине/VM, укажи её IP, "
                        "например http://10.0.2.2:11434",
                    ))
                finally:
                    self.events.put(("download_done",))

            threading.Thread(target=work, daemon=True).start()

        download_btn = ttk.Button(main, text="Скачать модель", command=download)
        download_btn.grid(row=4, column=1, sticky="w", pady=8)
        self._download_btn = download_btn
        download_status = ttk.Label(main, text="", foreground="#0066cc")
        download_status.grid(row=5, column=0, columnspan=2, sticky="w")
        self._download_status = download_status

        hint = ttk.Label(
            main,
            text="Хост Ollama: если модель скачивается в другой машине или VM, "
                 "укажи её адрес, например http://10.0.2.2:11434.\n"
                 "Для OCR нужен установленный Tesseract OCR "
                 "(https://github.com/UB-Mannheim/tesseract/wiki).",
            foreground="#666666",
        )
        hint.grid(row=6, column=0, columnspan=2, sticky="w", pady=(16, 4))

        # --- клавиши
        keys = ttk.Frame(notebook, padding=10)
        notebook.add(keys, text="Клавиши")
        key_vars: dict[str, tk.StringVar] = {}
        for row, (action, combo) in enumerate(self.hotkeys.items()):
            ttk.Label(keys, text=ACTION_TITLES.get(action, action) + ":").grid(
                row=row, column=0, sticky="w", pady=3
            )
            var = tk.StringVar(value=combo)
            key_vars[action] = var
            ttk.Entry(keys, textvariable=var, width=26).grid(
                row=row, column=1, sticky="w", pady=3
            )

        def save() -> None:
            model = model_var.get().strip()
            if not model or model.lower() in ("ollama", "model", "llm"):
                messagebox.showerror(
                    "Модель",
                    "Укажи имя модели из списка, например qwen3:8b или deepseek-r1:8b.",
                    parent=win,
                )
                return
            new_cfg = core.load_config()
            new_cfg["model"] = model
            new_cfg["host"] = host_var.get().strip() or "http://127.0.0.1:11434"
            selected = lang_combo.get()
            new_cfg["translate_target"] = next(
                (code for code, label in langs if label == selected), "auto"
            )
            new_cfg["keep_alive"] = keep_var.get()
            new_hooks: dict[str, str] = {}
            for action, var in key_vars.items():
                combo = var.get().strip()
                if not parse_hotkey(combo):
                    messagebox.showerror(
                        "Клавиши",
                        f"Неверная комбинация «{combo}» для «{action}».\n"
                        "Пример: Ctrl+Alt+Shift+T",
                        parent=win,
                    )
                    return
                new_hooks[action] = combo
            new_cfg["hotkeys"] = new_hooks
            core.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            core.CONFIG_PATH.write_text(
                json.dumps(new_cfg, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.cfg = new_cfg
            self.hotkeys = new_hooks
            self._close_settings()
            messagebox.showinfo(
                "AI Actions", "Настройки сохранены.\nХоткеи применятся после перезапуска.",
                parent=self.root,
            )

        buttons = ttk.Frame(win, padding=(8, 0, 8, 8))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Сохранить", command=save).pack(side="right")

    def _close_settings(self) -> None:
        win = self._settings_window
        self._settings_window = None
        self._download_btn = None
        self._download_status = None
        self._model_combo = None
        if win is not None:
            win.destroy()

    # -------------------------------------------------------------- выход

    def quit(self) -> None:
        core.log("windows stop")
        self.root.quit()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    app = App()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
