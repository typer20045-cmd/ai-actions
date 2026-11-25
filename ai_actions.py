#!/usr/bin/env python3
"""
ai-actions — ИИ-действия по горячей клавише для KDE Plasma 6.

Берёт выделенный текст (или скриншот с OCR), отправляет в локальную модель
(Ollama / OpenAI-совместимый API) и показывает результат, копируя его в буфер.

Зависимости: python3 (stdlib). Опционально: wl-clipboard (чтение выделения),
tesseract-ocr (распознавание скриншотов), spectacle (скриншоты), kdialog (UI).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = "ai-actions"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP
LOG_PATH = CACHE_DIR / "log.txt"

DEFAULTS = {
    "api": "ollama",  # "ollama" | "openai"
    "host": "http://127.0.0.1:11434",
    "model": "qwen3:8b",
    "think": False,
    "temperature": 0.2,
    "num_predict": 900,
    "num_ctx": 8192,
    "keep_alive": "10m",
    "timeout": 180,
    "max_input_chars": 6000,
    "ocr_lang": "rus+eng",
    "translate_target": "auto",  # auto | ru | en | de | ...
    "warmup_on_login": False,
    "openai": {
        "base_url": "http://127.0.0.1:3001/v1",
        "api_key": "",
        "model": "qwen3:8b",
    },
}

ACTIONS = {
    "translate": {
        "title": "Перевод",
        "icon": "accessories-dictionary",
        "prompt": (
            "Переведи этот текст на русский язык. Если он уже на русском — "
            "переведи на английский. Верни только перевод, без пояснений и кавычек."
        ),
    },
    "explain": {
        "title": "Объяснение",
        "icon": "help-hint",
        "prompt": (
            "Ответь по-русски кратко и по делу. Если это вопрос — ответь на него. "
            "Если это ошибка или код — назови причину и как исправить. "
            "Если это термин или фрагмент текста — объясни суть. "
            "Не описывай сам текст и его тип, сразу переходи к ответу."
        ),
    },
    "summarize": {
        "title": "Кратко",
        "icon": "view-list-details",
        "prompt": (
            "Кратко перескажи этот текст по-русски в виде 3–5 пунктов. "
            "Только суть, без вступлений."
        ),
    },
    "fix": {
        "title": "Исправить текст",
        "icon": "tools-check-spelling",
        "prompt": (
            "Исправь орфографию, пунктуацию и стиль этого текста, сохранив смысл "
            "и язык оригинала. Верни только исправленный текст."
        ),
    },
    "ask": {
        "title": "Спросить",
        "icon": "system-search",
        "prompt": None,  # инструкция вводится в диалоге
    },
}

LANGUAGE_NAMES = {
    "ru": "русский",
    "en": "английский",
    "uk": "украинский",
    "de": "немецкий",
    "fr": "французский",
    "es": "испанский",
    "it": "итальянский",
    "pt": "португальский",
    "pl": "польский",
    "tr": "турецкий",
    "zh": "китайский",
    "ja": "японский",
    "ko": "корейский",
    "ar": "арабский",
}

CONFIG_TEMPLATE = json.dumps(DEFAULTS, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- утилиты


def log(msg: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        # простая ротация
        if LOG_PATH.stat().st_size > 512_000:
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            LOG_PATH.write_text("\n".join(lines[-1000:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def run(cmd: list[str], timeout: float = 60, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
    )


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def notify(text: str, seconds: int = 3) -> None:
    if have("kdialog"):
        try:
            subprocess.Popen(
                ["kdialog", "--passivepopup", text, str(seconds)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass
    print(text, file=sys.stderr)


def fail(text: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    log("ERROR " + text.replace("\n", " ")[:500])
    if have("kdialog"):
        try:
            run(["kdialog", "--title", "AI-действия — ошибка", "--error", text], timeout=120)
        except Exception:
            print(text, file=sys.stderr)
    else:
        print(text, file=sys.stderr)
    raise SystemExit(code)


# ---------------------------------------------------------------- конфиг


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update(user)
                if isinstance(user.get("openai"), dict):
                    cfg["openai"] = {**DEFAULTS["openai"], **user["openai"]}
        except (OSError, json.JSONDecodeError) as exc:
            log(f"config error: {exc}")
    else:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(CONFIG_TEMPLATE + "\n", encoding="utf-8")
        except OSError:
            pass
    return cfg


# ---------------------------------------------------------------- буфер обмена


def _klipper_get() -> str:
    if not have("qdbus6"):
        return ""
    try:
        r = run(
            ["qdbus6", "org.kde.klipper", "/klipper",
             "org.kde.klipper.klipper.getClipboardContents"],
            timeout=5,
        )
        return r.stdout.rstrip("\n") if r.returncode == 0 else ""
    except Exception:
        return ""


def _klipper_set(text: str) -> bool:
    if not have("qdbus6"):
        return False
    try:
        r = run(
            ["qdbus6", "org.kde.klipper", "/klipper",
             "org.kde.klipper.klipper.setClipboardContents", text],
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def get_clipboard() -> str:
    if have("wl-paste"):
        try:
            r = run(["wl-paste", "--no-newline"], timeout=5)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception:
            pass
    return _klipper_get()


def get_primary() -> str:
    if have("wl-paste"):
        try:
            r = run(["wl-paste", "--primary", "--no-newline"], timeout=5)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception:
            pass
    return ""


def set_clipboard(text: str) -> None:
    if have("wl-copy"):
        try:
            if run(["wl-copy"], timeout=5, stdin=text).returncode == 0:
                return
        except Exception:
            pass
    if _klipper_set(text):
        return
    if have("xclip"):
        try:
            run(["xclip", "-selection", "clipboard"], timeout=5, stdin=text)
            return
        except Exception:
            pass
    log("clipboard: не удалось скопировать результат")


# ---------------------------------------------------------------- ввод


def cleanup_temp_screenshots(max_age: float = 3600) -> None:
    """Удаляет старые снимки, оставшиеся после сбоев (сами снимки живут до OCR)."""
    tmp = Path(tempfile.gettempdir())
    now = time.time()
    for path in tmp.glob("ai-actions-*.png"):
        try:
            if now - path.stat().st_mtime > max_age:
                path.unlink()
        except OSError:
            pass


def screenshot(mode: str) -> str:
    """Делает снимок и возвращает путь. mode: region | full | window."""
    if not have("spectacle"):
        fail("Не найден spectacle — нужен для снимков экрана.")
    cleanup_temp_screenshots()
    fd, path = tempfile.mkstemp(prefix="ai-actions-", suffix=".png")
    os.close(fd)
    os.unlink(path)
    flag = {"region": "-r", "full": "-f", "window": "-a"}.get(mode, "-r")
    try:
        run(["spectacle", flag, "-b", "-n", "-o", path], timeout=180)
    except subprocess.TimeoutExpired:
        fail("Снимок экрана не был сделан (таймаут).")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        fail("Снимок отменён или пустой.")
    return path


def ocr(path: str, cfg: dict) -> str:
    if not have("tesseract"):
        fail(
            "Для распознавания скриншотов нужен tesseract.\n\n"
            "Установи:\n  sudo apt install -y tesseract-ocr tesseract-ocr-rus"
        )
    langs = cfg.get("ocr_lang", "rus+eng")
    try:
        r = run(["tesseract", path, "stdout", "-l", langs], timeout=120)
    except subprocess.TimeoutExpired:
        fail("OCR не успел за 120 секунд.")
    if r.returncode != 0:
        # возможно, нет языковых пакетов — пробуем eng
        try:
            r2 = run(["tesseract", path, "stdout", "-l", "eng"], timeout=120)
            if r2.returncode == 0 and r2.stdout.strip():
                return r2.stdout.strip()
        except Exception:
            pass
        fail("OCR не сработал:\n" + (r.stderr or "неизвестная ошибка")[:500])
    text = r.stdout.strip()
    if not text:
        fail("На скриншоте не распознан текст.")
    return text


def read_input(args: argparse.Namespace, cfg: dict) -> tuple[str, bool]:
    """Возвращает (текст, был_ли_ocr)."""
    if args.text is not None:
        return args.text, False
    if args.stdin:
        return sys.stdin.read(), False
    if args.image:
        return ocr(args.image, cfg), True
    if args.screen or args.full or args.window:
        mode = "full" if args.full else "window" if args.window else "region"
        path = screenshot(mode)
        try:
            return ocr(path, cfg), True
        finally:
            # Снимок нужен только для OCR — не оставляем его на диске.
            try:
                os.unlink(path)
            except OSError:
                pass
    if args.clipboard:
        return get_clipboard(), False
    if args.primary:
        return get_primary(), False
    # по умолчанию: выделение, затем буфер
    text = get_primary()
    if not text.strip():
        text = get_clipboard()
    return text, False


def ask_dialog(prompt: str, initial: str = "") -> str:
    if not have("kdialog"):
        return ""
    try:
        r = run(["kdialog", "--title", "AI: что сделать?", "--inputbox", prompt,
                 *([initial] if initial else [])], timeout=300)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


# ---------------------------------------------------------------- LLM


def strip_markdown(text: str) -> str:
    """Убирает markdown-разметку для показа в простом текстовом окне."""
    out = []
    for line in text.splitlines():
        s = line.rstrip()
        if re.fullmatch(r"\s*[-*_]{3,}\s*", s):
            out.append("")
            continue
        if s.lstrip().startswith("```"):
            continue
        s = re.sub(r"^(\s*)#{1,6}\s*", r"\1", s)
        s = re.sub(r"^(\s*)[-*]\s+", r"\1• ", s)
        s = s.replace("**", "").replace("__", "").replace("`", "")
        s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", s)
        out.append(s)
    return "\n".join(out).strip()


def model_loaded(cfg: dict) -> bool:
    """True, если модель уже в памяти Ollama (иначе первый ответ будет долгим)."""
    if (cfg.get("api") or "ollama").lower() != "ollama":
        return True
    try:
        host = cfg.get("host", "").rstrip("/")
        with urllib.request.urlopen(host + "/api/ps", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        model = (cfg.get("model") or "").split(":")[0]
        return any((m.get("name") or "").split(":")[0] == model
                   for m in data.get("models", []))
    except Exception:  # noqa: BLE001
        return True


def call_llm(prompt: str, cfg: dict) -> str:
    api = (cfg.get("api") or "ollama").lower()
    timeout = float(cfg.get("timeout", 180))

    if api == "openai":
        o = cfg.get("openai", {})
        url = o.get("base_url", "").rstrip("/") + "/chat/completions"
        body = {
            "model": o.get("model") or cfg.get("model"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": cfg.get("temperature", 0.2),
            "max_tokens": cfg.get("num_predict", 900),
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if o.get("api_key"):
            headers["Authorization"] = f"Bearer {o['api_key']}"
    else:
        url = cfg.get("host", "http://127.0.0.1:11434").rstrip("/") + "/api/generate"
        body = {
            "model": cfg.get("model", "qwen3:8b"),
            "prompt": prompt,
            "stream": False,
            "think": bool(cfg.get("think", False)),
            "keep_alive": cfg.get("keep_alive", "10m"),
            "options": {
                "temperature": cfg.get("temperature", 0.2),
                "num_predict": cfg.get("num_predict", 900),
                "num_ctx": cfg.get("num_ctx", 8192),
            },
        }
        headers = {"Content-Type": "application/json"}

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        fail(f"LLM вернул ошибку HTTP {exc.code}.\n\n{detail}")
    except urllib.error.URLError as exc:
        fail(
            f"Не могу подключиться к модели:\n{url}\n\n{exc.reason}\n\n"
            "Проверь, что Ollama запущена: systemctl --user status ollama"
        )
    except TimeoutError:
        fail(f"Модель не ответила за {int(timeout)} с.")

    if api == "openai":
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            fail("Неожиданный ответ OpenAI-совместимого API:\n" + str(data)[:400])

    text = (data.get("response") or "").strip()
    if not text and data.get("thinking"):
        text = str(data["thinking"]).strip()
    for junk in ("/no_think", "<|im_start|>", "<|im_end|>"):
        text = text.replace(junk, "")
    return text.strip()


# ---------------------------------------------------------------- вывод


def show_result(title: str, text: str) -> None:
    if not have("kdialog"):
        print(text)
        return
    fd, path = tempfile.mkstemp(prefix="ai-actions-out-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        run(
            ["kdialog", "--title", f"AI · {title}", "--textbox", path,
             "900", "600"],
            timeout=3600,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------- команды


def translate_instruction(cfg: dict) -> str:
    """Инструкция перевода с учётом выбранного языка назначения."""
    target = (cfg.get("translate_target") or "auto").lower()
    name = LANGUAGE_NAMES.get(target)
    if name:
        return (
            f"Переведи этот текст на {name} язык. "
            "Верни только перевод, без пояснений и кавычек."
        )
    return ACTIONS["translate"]["prompt"]


def build_prompt(action: str, text: str, cfg: dict, args: argparse.Namespace) -> str:
    if action == "ask":
        question = args.prompt or ask_dialog("Что спросить у модели?")
        if not question:
            fail("Запрос отменён.")
        if text.strip():
            return f"Контекст:\n{text}\n\nВопрос: {question}"
        return question

    spec = ACTIONS[action]
    if action == "translate":
        instruction = args.prompt or translate_instruction(cfg)
    else:
        instruction = args.prompt or spec["prompt"]
    ocr_note = ""
    if args.image or args.screen or args.full or args.window:
        ocr_note = "Текст ниже распознан с экрана через OCR, в нём могут быть ошибки.\n\n"
    # Инструкция идёт ПОСЛЕ текста: так qwen3 стабильнее выполняет задание,
    # а не просто повторяет исходный текст.
    return f"{ocr_note}Текст:\n{text}\n\nЗадание: {instruction}"


def cmd_run(action: str, args: argparse.Namespace) -> None:
    cfg = load_config()
    started = time.time()

    text, was_ocr = read_input(args, cfg)
    if action != "ask" and (not text or not text.strip()):
        hint = ""
        if not have("wl-paste"):
            hint = ("\n\nСовет: поставь wl-clipboard, чтобы работало выделение без Ctrl+C:\n"
                    "  sudo apt install -y wl-clipboard")
        fail("Нет входного текста: ничего не выделено и буфер пуст." + hint)

    limit = int(cfg.get("max_input_chars", 6000))
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]

    prompt = build_prompt(action, text, cfg, args)
    if not model_loaded(cfg):
        notify("⏳ Загружаю модель в видеопамять…", 15)
    result = call_llm(prompt, cfg)
    if not result:
        fail("Модель вернула пустой ответ.")

    elapsed = time.time() - started
    title = ACTIONS.get(action, {}).get("title", action)
    set_clipboard(result)
    log(f"ok action={action} chars={len(text)} ocr={was_ocr} trunc={truncated} "
        f"elapsed={elapsed:.1f}s")

    if args.no_window:
        snippet = result if len(result) <= 300 else result[:300] + "…"
        notify(f"✅ {title} (скопировано)\n\n{snippet}", 8)
        return

    shown = result
    if truncated:
        shown += "\n\n— — —\n[входной текст был обрезан до %d символов]" % limit
    notify(f"✅ {title}: скопировано в буфер", 2)
    show_result(title, strip_markdown(shown))


def cmd_warmup(args: argparse.Namespace) -> int:
    """Прогревает модель, не показывая окон. Нужен для автозапуска."""
    cfg = load_config()
    if (cfg.get("api") or "ollama").lower() != "ollama":
        return 0
    host = cfg.get("host", "http://127.0.0.1:11434").rstrip("/")
    body = {
        "model": cfg.get("model", "qwen3:8b"),
        "prompt": "hi",
        "stream": False,
        "keep_alive": cfg.get("keep_alive", "10m"),
        "options": {"num_predict": 1},
    }
    req = urllib.request.Request(
        host + "/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(cfg.get("timeout", 180))) as resp:
            resp.read()
        log("warmup ok")
    except Exception as exc:  # noqa: BLE001
        log(f"warmup failed: {exc}")
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    """Запускает графическое окно настроек (PyQt6)."""
    import importlib.util

    if importlib.util.find_spec("PyQt6") is None:
        fail(
            "Для окна настроек нужен PyQt6.\n\n"
            "Установи:\n  sudo apt install -y python3-pyqt6"
        )
    script = Path(__file__).resolve().parent / "ai_settings.py"
    if not script.exists():
        fail(f"Не найден {script}.\nОбнови проект: cd ~/ai-actions && git pull")
    try:
        subprocess.Popen(
            [sys.executable, str(script)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        fail(f"Не удалось открыть настройки: {exc}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> None:
    cfg = load_config()
    lines = []
    ok = "✅"
    no = "❌"

    def check(name: str, present: bool, hint: str = "") -> None:
        lines.append(f"{ok if present else no} {name}" + (f"  — {hint}" if not present and hint else ""))

    check("python3", True)
    check("kdialog (окна и уведомления)", have("kdialog"), "sudo apt install kdialog")
    check("qdbus6 (буфер KDE)", have("qdbus6"), "sudo apt install qt6-tools")
    check("wl-clipboard (выделение)", have("wl-paste"), "sudo apt install wl-clipboard")
    check("spectacle (скриншоты)", have("spectacle"), "sudo apt install spectacle")
    check("tesseract (OCR)", have("tesseract"), "sudo apt install tesseract-ocr tesseract-ocr-rus")
    import importlib.util
    check("PyQt6 (окно настроек)", importlib.util.find_spec("PyQt6") is not None,
          "sudo apt install python3-pyqt6")

    api = (cfg.get("api") or "ollama").lower()
    if api == "openai":
        base = cfg.get("openai", {}).get("base_url", "")
        try:
            req = urllib.request.Request(base.rstrip("/") + "/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                lines.append(f"{ok} OpenAI-совместимый API: {base} (HTTP {resp.status})")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{no} OpenAI-совместимый API: {base} — {exc}")
    else:
        host = cfg.get("host", "").rstrip("/")
        try:
            with urllib.request.urlopen(host + "/api/tags", timeout=5) as resp:
                tags = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "?") for m in tags.get("models", [])]
            lines.append(f"{ok} Ollama: {host}")
            model = cfg.get("model", "")
            if model in models or any(m.split(":")[0] == model.split(":")[0] for m in models):
                lines.append(f"{ok} Модель: {model}")
            else:
                lines.append(f"{no} Модель {model} не найдена. Есть: {', '.join(models) or '—'}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{no} Ollama: {host} — {exc}")

    lines.append("")
    lines.append(f"Модель: {cfg.get('model')} · keep_alive: {cfg.get('keep_alive')}")
    target = cfg.get("translate_target", "auto")
    lines.append(f"Перевод: {LANGUAGE_NAMES.get(target, 'авто (RU ⇄ EN)')}")
    lines.append(f"Конфиг: {CONFIG_PATH}")
    lines.append(f"Лог:    {LOG_PATH}")
    text = "\n".join(lines)
    print(text)
    if not args.print_only and have("kdialog"):
        fd, path = tempfile.mkstemp(prefix="ai-actions-doctor-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            run(["kdialog", "--title", "AI-действия — проверка", "--textbox", path, "760", "420"],
                timeout=3600)
        finally:
            os.unlink(path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="ai-actions",
        description="ИИ-действия по горячей клавише: перевод, объяснение, OCR и другое.",
    )
    p.add_argument("action", nargs="?", default="explain",
                   choices=sorted(ACTIONS) + ["doctor", "settings", "warmup"],
                   help="что сделать (по умолчанию explain)")
    p.add_argument("--text", help="взять этот текст вместо выделения")
    p.add_argument("--stdin", action="store_true", help="читать текст из stdin")
    p.add_argument("--clipboard", action="store_true", help="взять обычный буфер (Ctrl+C)")
    p.add_argument("--primary", action="store_true", help="взять только выделение (primary)")
    p.add_argument("--screen", action="store_true", help="снимок области экрана + OCR")
    p.add_argument("--full", action="store_true", help="снимок всего экрана + OCR")
    p.add_argument("--window", action="store_true", help="снимок активного окна + OCR")
    p.add_argument("--image", help="распознать готовое изображение")
    p.add_argument("--prompt", help="своя инструкция для модели")
    p.add_argument("--no-window", action="store_true", help="только уведомление, без окна")
    p.add_argument("--print-only", action="store_true", help="doctor: только stdout")
    p.add_argument("--version", action="version", version="ai-actions 1.1.0")

    args = p.parse_args(argv)
    try:
        if args.action == "doctor":
            cmd_doctor(args)
        elif args.action == "settings":
            return cmd_settings(args)
        elif args.action == "warmup":
            return cmd_warmup(args)
        else:
            cmd_run(args.action, args)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        fail(f"Неожиданная ошибка: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
