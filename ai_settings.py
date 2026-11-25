#!/usr/bin/env python3
"""Графические настройки ai-actions (PyQt6).

Запускается командой `ai-actions settings`. Позволяет выбрать модель Ollama
(и скачать её), переназначить горячие клавиши, выбрать язык перевода и
настроить поведение модели в памяти.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ai_actions import CONFIG_PATH, LANGUAGE_NAMES, load_config

APP_DIR = Path(__file__).resolve().parent
APPS_DIR = Path.home() / ".local" / "share" / "applications"
AUTOSTART_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "ai-actions-warmup.desktop"

ACTION_ORDER = ["translate", "explain", "summarize", "fix", "ocr", "question", "settings"]

DESKTOP = {
    "translate": ("ai-actions-translate.desktop", "AI: перевести выделенное"),
    "explain": ("ai-actions-explain.desktop", "AI: объяснить выделенное"),
    "summarize": ("ai-actions-summarize.desktop", "AI: кратко пересказать"),
    "fix": ("ai-actions-fix.desktop", "AI: исправить текст"),
    "ocr": ("ai-actions-ocr.desktop", "AI: объяснить область экрана (OCR)"),
    "question": ("ai-actions-question.desktop", "AI: спросить модель"),
    "settings": ("ai-actions-settings.desktop", "AI: настройки"),
}

DEFAULT_SHORTCUTS = {
    "translate": "Meta+Shift+T",
    "explain": "Meta+Shift+E",
    "summarize": "Meta+Shift+M",
    "fix": "Meta+Shift+F",
    "ocr": "Meta+Shift+O",
    "question": "Meta+Shift+Q",
    "settings": "Meta+Shift+P",
}

TITLES = {
    "translate": "Перевод",
    "explain": "Объяснение",
    "summarize": "Кратко",
    "fix": "Исправить текст",
    "ocr": "OCR области экрана",
    "question": "Спросить модель",
    "settings": "Настройки",
}

RECOMMENDED = [
    "qwen3:8b",
    "qwen3:4b",
    "qwen3:1.7b",
    "llama3.2:3b",
    "gemma3:4b",
    "mistral:7b",
    "phi4-mini:3.8b",
    "deepseek-r1:8b",
]

LANGUAGES = [("auto", "Авто (RU ⇄ EN)")] + [
    (code, name.capitalize()) for code, name in LANGUAGE_NAMES.items()
]

KEEP_ALIVE = [
    ("5m", "5 минут"),
    ("10m", "10 минут"),
    ("30m", "30 минут"),
    ("1h", "1 час"),
    (-1, "Не выгружать"),
    (0, "Выгружать сразу"),
]


# ---------------------------------------------------------------- сеть и файлы


def http_json(url: str, payload: dict | None = None, timeout: float = 15) -> dict:
    data = None
    headers = {}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_installed(host: str) -> dict[str, int]:
    data = http_json(host.rstrip("/") + "/api/tags")
    return {
        m.get("name", ""): int(m.get("size") or 0)
        for m in data.get("models", [])
        if m.get("name")
    }


def fetch_loaded(host: str) -> list[dict]:
    data = http_json(host.rstrip("/") + "/api/ps", timeout=4)
    return data.get("models", [])


def fmt_size(size: int | float) -> str:
    if not size:
        return "?"
    return f"{float(size) / 1024 ** 3:.1f} ГБ"


def write_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def desktop_shortcut(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("X-KDE-Shortcuts="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def write_desktop_shortcut(path: Path, seq: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith("X-KDE-Shortcuts="):
            lines[i] = f"X-KDE-Shortcuts={seq}"
            found = True
    if not found:
        index = next(
            (i for i, line in enumerate(lines) if line.startswith("Categories=")),
            len(lines),
        )
        lines.insert(index, f"X-KDE-Shortcuts={seq}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, path.stat().st_mode)
    except OSError:
        pass
    os.replace(tmp, path)
    return True


def shortcut_key(seq_text: str) -> int:
    seq = QKeySequence.fromString(seq_text, QKeySequence.SequenceFormat.PortableText)
    if seq.isEmpty() or seq.count() != 1:
        raise ValueError("нужна одна комбинация клавиш")
    return int(seq[0].toCombined())


def validate_shortcut(seq_text: str) -> None:
    if not seq_text:
        raise ValueError("комбинация не задана")
    parts = seq_text.split("+")
    if not any(p in ("Meta", "Ctrl", "Alt") for p in parts):
        raise ValueError("нужен модификатор Meta, Ctrl или Alt")
    shortcut_key(seq_text)


def _gvariant(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def apply_kde_shortcut(component: str, title: str, seq_text: str) -> bool:
    """Меняет шорткат в KDE. Флаг 5 = SetPresent|IsDefault — сразу пишет в конфиг."""
    try:
        key = shortcut_key(seq_text)
    except ValueError:
        return False
    action_id = "['%s','_launch','%s','%s']" % (
        component,
        _gvariant(title),
        _gvariant(title),
    )
    cmd = [
        "gdbus", "call", "--session",
        "--dest", "org.kde.kglobalaccel",
        "--object-path", "/kglobalaccel",
        "--method", "org.kde.KGlobalAccel.setShortcut",
        action_id, f"[{key}]", "5",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and f"[{key}]" in result.stdout


def refresh_kbuildsycoca() -> None:
    if shutil.which("kbuildsycoca6"):
        try:
            subprocess.Popen(
                ["kbuildsycoca6", "--noincremental"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass


def set_autostart(enabled: bool) -> None:
    if not enabled:
        try:
            AUTOSTART_FILE.unlink()
        except FileNotFoundError:
            pass
        return
    AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    exe = shutil.which("ai-actions") or str(Path.home() / ".local" / "bin" / "ai-actions")
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=AI Actions — прогрев модели\n"
        "Comment=Заранее загружает модель Ollama в видеопамять\n"
        f"Exec={exe} warmup\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "X-KDE-autostart-after=panel\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- фоновые задачи


class PullThread(QThread):
    progress = pyqtSignal(int, str)
    ok = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, host: str, model: str) -> None:
        super().__init__()
        self.host = host
        self.model = model
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        url = self.host.rstrip("/") + "/api/pull"
        body = json.dumps({"model": self.model, "stream": True}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                for raw in resp:
                    if self._cancel:
                        self.error.emit("Загрузка отменена.")
                        return
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("error"):
                        self.error.emit(str(obj["error"]))
                        return
                    status = obj.get("status") or ""
                    total = int(obj.get("total") or 0)
                    completed = int(obj.get("completed") or 0)
                    percent = int(completed * 100 / total) if total else -1
                    self.progress.emit(percent, status)
            self.ok.emit(self.model)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))


class GenerateThread(QThread):
    ok = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, host: str, payload: dict) -> None:
        super().__init__()
        self.host = host
        self.payload = payload

    def run(self) -> None:
        try:
            http_json(
                self.host.rstrip("/") + "/api/generate",
                self.payload,
                timeout=600,
            )
            self.ok.emit()
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))


# ---------------------------------------------------------------- окно


class SettingsWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        self.host = self.cfg.get("host", "http://127.0.0.1:11434")
        self.installed: dict[str, int] = {}
        self.pull_thread: PullThread | None = None
        self.generate_thread: GenerateThread | None = None

        self.setWindowTitle("AI Actions — настройки")
        self.setWindowIcon(QIcon.fromTheme("preferences-system"))
        self.resize(760, 600)
        self._build()
        self._load_values()

    # ------------------------------------------------------------ UI

    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 12)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_model(), "Модель")
        self.tabs.addTab(self._tab_keys(), "Клавиши")
        self.tabs.addTab(self._tab_translate(), "Перевод")
        self.tabs.addTab(self._tab_perf(), "Память")
        root.addWidget(self.tabs)

        bottom = QHBoxLayout()
        doctor = QPushButton("Проверить")
        doctor.clicked.connect(self._run_doctor)
        bottom.addWidget(doctor)
        bottom.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.close)
        save = QPushButton("Сохранить")
        save.setDefault(True)
        save.clicked.connect(self._save)
        bottom.addWidget(cancel)
        bottom.addWidget(save)
        root.addLayout(bottom)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Готово")

    def _tab_model(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        form = QFormLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.currentTextChanged.connect(self._update_model_state)
        form.addRow("Модель:", self.model_combo)

        self.model_hint = QLabel()
        self.model_hint.setWordWrap(True)
        form.addRow("", self.model_hint)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.refresh_btn = QPushButton("Обновить список")
        self.refresh_btn.clicked.connect(self.reload_models)
        self.download_btn = QPushButton("Скачать / обновить")
        self.download_btn.clicked.connect(self._download_model)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.download_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)
        self.progress_label = QLabel()
        self.progress_label.setWordWrap(True)
        lay.addWidget(self.progress_label)

        hint = QLabel(
            "В списке — установленные модели и рекомендуемые. Можно ввести любое "
            "имя, например <b>qwen3:4b</b> или <b>gemma3:4b</b>: если модели нет, "
            "она скачается при сохранении или по кнопке."
        )
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        return w

    def _tab_keys(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.key_edits: dict[str, QKeySequenceEdit] = {}

        for action in ACTION_ORDER:
            if action == "settings":
                title = "Настройки"
            else:
                title = TITLES[action]
            edit = QKeySequenceEdit()
            edit.setMaximumSequenceLength(1)
            self.key_edits[action] = edit
            form.addRow(f"{title}:", edit)

        lay.addLayout(form)

        reset = QPushButton("Сбросить все на стандартные")
        reset.clicked.connect(self._reset_shortcuts)
        lay.addWidget(reset)

        hint = QLabel(
            "Нажми на поле и введи комбинацию. Нужен Meta, Ctrl или Alt. "
            "Если комбинация занята системой, KDE оставит прежнюю — об этом будет "
            "предупреждение после сохранения."
        )
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        return w

    def _tab_translate(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.lang_combo = QComboBox()
        for code, label in LANGUAGES:
            self.lang_combo.addItem(label, code)
        self.lang_combo.currentIndexChanged.connect(self._update_lang_hint)
        form.addRow("Переводить на:", self.lang_combo)
        lay.addLayout(form)

        self.lang_hint = QLabel()
        self.lang_hint.setWordWrap(True)
        lay.addWidget(self.lang_hint)

        note = QLabel(
            "Режим «Авто» переводит русский на английский, а любой другой язык — "
            "на русский. Остальные пункты всегда переводят на выбранный язык."
        )
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        return w

    def _tab_perf(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        form = QFormLayout()
        self.keep_combo = QComboBox()
        for value, label in KEEP_ALIVE:
            self.keep_combo.addItem(label, value)
        form.addRow("Держать модель:", self.keep_combo)
        lay.addLayout(form)

        self.warmup_check = QCheckBox("Загружать модель при входе в систему")
        lay.addWidget(self.warmup_check)

        box = QGroupBox("Состояние")
        box_lay = QVBoxLayout(box)
        self.model_status = QLabel("…")
        self.model_status.setWordWrap(True)
        box_lay.addWidget(self.model_status)
        row = QHBoxLayout()
        self.load_btn = QPushButton("Загрузить сейчас")
        self.load_btn.clicked.connect(self._load_model)
        self.unload_btn = QPushButton("Выгрузить")
        self.unload_btn.clicked.connect(self._unload_model)
        self.status_refresh = QPushButton("Обновить")
        self.status_refresh.clicked.connect(self._refresh_status)
        row.addWidget(self.load_btn)
        row.addWidget(self.unload_btn)
        row.addWidget(self.status_refresh)
        row.addStretch(1)
        box_lay.addLayout(row)
        lay.addWidget(box)

        hint = QLabel(
            "Ollama сама управляет памятью: при нехватке видеопамяти она выгружает "
            "часть слоёв в RAM, а при нехватке RAM — модель целиком. «Не выгружать» "
            "запрещает выгрузку по таймеру, но не отменяет нехватку памяти. Если "
            "модель часто выгружается, выбери модель меньше или закрой приложения, "
            "которые занимают GPU."
        )
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        return w

    # ------------------------------------------------------------ значения

    def _load_values(self) -> None:
        self.reload_models(select=self.cfg.get("model", "qwen3:8b"))

        target = self.cfg.get("translate_target", "auto")
        index = self.lang_combo.findData(target)
        self.lang_combo.setCurrentIndex(index if index >= 0 else 0)
        self._update_lang_hint()

        keep = self.cfg.get("keep_alive", "10m")
        index = self.keep_combo.findData(keep)
        if index < 0:
            index = self.keep_combo.findData("10m")
        self.keep_combo.setCurrentIndex(max(index, 0))
        self.warmup_check.setChecked(AUTOSTART_FILE.exists())

        for action in ACTION_ORDER:
            component, _ = DESKTOP[action]
            seq = desktop_shortcut(APPS_DIR / component) or DEFAULT_SHORTCUTS[action]
            self.key_edits[action].setKeySequence(
                QKeySequence.fromString(seq, QKeySequence.SequenceFormat.PortableText)
            )

        self._refresh_status()

    def reload_models(self, select: str | None = None) -> None:
        current = select or self.model_combo.currentText().strip() or self.cfg.get("model")
        try:
            self.installed = fetch_installed(self.host)
            self.statusBar().showMessage(f"Ollama: найдено моделей — {len(self.installed)}", 4000)
        except Exception as exc:  # noqa: BLE001
            self.installed = {}
            self.statusBar().showMessage(f"Ollama недоступна: {exc}", 8000)

        names = list(self.installed) + [m for m in RECOMMENDED if m not in self.installed]
        if current and current not in names:
            names.insert(0, current)

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for name in names:
            self.model_combo.addItem(name)
            if name in self.installed:
                self.model_combo.setItemData(
                    self.model_combo.count() - 1,
                    f"Установлена · {fmt_size(self.installed[name])}",
                    3,  # ToolTipRole
                )
            else:
                self.model_combo.setItemData(
                    self.model_combo.count() - 1,
                    "Не установлена — будет скачана",
                    3,
                )
        if current:
            self.model_combo.setCurrentText(current)
        self.model_combo.blockSignals(False)
        self._update_model_state()

    def _update_model_state(self) -> None:
        name = self.model_combo.currentText().strip()
        if not name:
            self.model_hint.setText("Введи имя модели.")
            self.download_btn.setEnabled(False)
            return
        if name in self.installed:
            self.model_hint.setText(
                f"✅ Установлена · {fmt_size(self.installed[name])}"
            )
        else:
            self.model_hint.setText(
                "⬇ Не установлена — скачается при сохранении или по кнопке ниже."
            )
        self.download_btn.setEnabled(True)

    def _update_lang_hint(self) -> None:
        code = self.lang_combo.currentData()
        if code == "auto":
            self.lang_hint.setText(
                "Русский текст → английский, остальные языки → русский."
            )
        else:
            self.lang_hint.setText(
                f"Весь текст будет переведён на: {LANGUAGE_NAMES.get(code, code)}."
            )

    def _refresh_status(self) -> None:
        model = self.model_combo.currentText().strip() or self.cfg.get("model", "")
        try:
            loaded = fetch_loaded(self.host)
        except Exception as exc:  # noqa: BLE001
            self.model_status.setText(f"❌ Ollama недоступна: {exc}")
            return
        found = next(
            (m for m in loaded if m.get("name") == model or m.get("model") == model),
            None,
        )
        if found:
            vram = found.get("size_vram") or 0
            total = found.get("size") or 0
            where = f"{fmt_size(vram)} в VRAM" if vram else "полностью в RAM"
            self.model_status.setText(
                f"✅ {model} загружена · {where}"
                + (f" · всего {fmt_size(total)}" if total else "")
            )
        else:
            self.model_status.setText(f"○ {model} не загружена")

    def _reset_shortcuts(self) -> None:
        for action, seq in DEFAULT_SHORTCUTS.items():
            self.key_edits[action].setKeySequence(
                QKeySequence.fromString(seq, QKeySequence.SequenceFormat.PortableText)
            )

    # ------------------------------------------------------------ действия

    def _download_model(self) -> None:
        name = self.model_combo.currentText().strip()
        if not name:
            QMessageBox.warning(self, "Модель", "Сначала укажи имя модели.")
            return
        self._start_pull(name)

    def _start_pull(self, name: str) -> None:
        if self.pull_thread and self.pull_thread.isRunning():
            return
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText(f"Скачиваю {name}…")
        self.download_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.statusBar().showMessage(f"Загрузка модели {name}…")

        thread = PullThread(self.host, name)
        thread.progress.connect(self._on_pull_progress)
        thread.ok.connect(self._on_pull_ok)
        thread.error.connect(self._on_pull_error)
        thread.finished.connect(self._on_pull_finished)
        self.pull_thread = thread
        thread.start()

    def _on_pull_progress(self, percent: int, status: str) -> None:
        if percent >= 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
            self.progress_label.setText(f"{status} — {percent}%")
        else:
            self.progress.setRange(0, 0)
            self.progress_label.setText(status)

    def _on_pull_ok(self, model: str) -> None:
        self.progress_label.setText(f"✅ {model} скачана.")
        self.reload_models(select=model)
        self._refresh_status()
        QMessageBox.information(self, "Готово", f"Модель {model} скачана и выбрана.")

    def _on_pull_error(self, message: str) -> None:
        self.progress_label.setText(f"❌ {message}")
        QMessageBox.warning(self, "Ошибка загрузки", message)

    def _on_pull_finished(self) -> None:
        self.progress.setVisible(False)
        self.progress.setRange(0, 100)
        self.download_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.statusBar().showMessage("Готово", 3000)
        if self.pull_thread:
            self.pull_thread.deleteLater()
            self.pull_thread = None

    def _load_model(self) -> None:
        name = self.model_combo.currentText().strip()
        if not name:
            return
        keep = self.keep_combo.currentData()
        payload = {
            "model": name,
            "prompt": "hi",
            "stream": False,
            "keep_alive": keep,
            "options": {"num_predict": 1},
        }
        self._run_generate(payload, "Загружаю модель…")

    def _unload_model(self) -> None:
        name = self.model_combo.currentText().strip()
        if not name:
            return
        payload = {"model": name, "prompt": "", "stream": False, "keep_alive": 0}
        self._run_generate(payload, "Выгружаю модель…")

    def _run_generate(self, payload: dict, message: str) -> None:
        if self.generate_thread and self.generate_thread.isRunning():
            return
        self.load_btn.setEnabled(False)
        self.unload_btn.setEnabled(False)
        self.statusBar().showMessage(message)
        thread = GenerateThread(self.host, payload)
        thread.ok.connect(self._on_generate_ok)
        thread.error.connect(self._on_generate_error)
        thread.finished.connect(self._on_generate_finished)
        self.generate_thread = thread
        thread.start()

    def _on_generate_ok(self) -> None:
        self._refresh_status()
        self.statusBar().showMessage("Готово", 3000)

    def _on_generate_error(self, message: str) -> None:
        QMessageBox.warning(self, "Ошибка", message)

    def _on_generate_finished(self) -> None:
        self.load_btn.setEnabled(True)
        self.unload_btn.setEnabled(True)
        if self.generate_thread:
            self.generate_thread.deleteLater()
            self.generate_thread = None

    def _run_doctor(self) -> None:
        try:
            result = subprocess.run(
                [sys.executable, str(APP_DIR / "ai_actions.py"), "doctor", "--print-only"],
                capture_output=True,
                text=True,
                timeout=40,
            )
            text = result.stdout or result.stderr
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
        QMessageBox.information(self, "Проверка", text)

    # ------------------------------------------------------------ сохранение

    def _save(self) -> None:
        model = self.model_combo.currentText().strip()
        if not model:
            QMessageBox.warning(self, "Модель", "Укажи имя модели.")
            return

        sequences: dict[str, str] = {}
        seen: dict[str, str] = {}
        for action in ACTION_ORDER:
            seq = self.key_edits[action].keySequence().toString(
                QKeySequence.SequenceFormat.PortableText
            )
            title = "Настройки" if action == "settings" else TITLES[action]
            try:
                validate_shortcut(seq)
            except ValueError as exc:
                QMessageBox.warning(self, "Клавиши", f"{title}: {exc}")
                return
            if seq in seen:
                QMessageBox.warning(
                    self,
                    "Клавиши",
                    f"Комбинация {seq} уже занята действием «{seen[seq]}».",
                )
                return
            seen[seq] = title
            sequences[action] = seq

        cfg = load_config()
        cfg["model"] = model
        cfg["translate_target"] = self.lang_combo.currentData()
        cfg["keep_alive"] = self.keep_combo.currentData()
        cfg["warmup_on_login"] = self.warmup_check.isChecked()
        write_config(cfg)
        self.cfg = cfg
        self.host = cfg.get("host", self.host)

        failed = []
        for action, seq in sequences.items():
            component, title = DESKTOP[action]
            path = APPS_DIR / component
            if not path.exists():
                failed.append(f"{title}: нет файла {component}")
                continue
            write_desktop_shortcut(path, seq)
            if not apply_kde_shortcut(component, title, seq):
                failed.append(f"{title}: комбинация {seq} не применилась (занята?)")
        refresh_kbuildsycoca()
        set_autostart(self.warmup_check.isChecked())
        self._refresh_status()

        if failed:
            QMessageBox.warning(
                self,
                "Сохранено с замечаниями",
                "Настройки записаны, но:\n\n" + "\n".join(failed),
            )
        else:
            QMessageBox.information(self, "Готово", "Настройки сохранены.")

        if model not in self.installed:
            answer = QMessageBox.question(
                self,
                "Модель не скачана",
                f"Модели {model} нет локально. Скачать сейчас?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._start_pull(model)
            else:
                QMessageBox.warning(
                    self,
                    "Модель не скачана",
                    f"Модель {model} не установлена — действия будут падать с ошибкой, "
                    "пока ты не скачаешь её кнопкой «Скачать / обновить».",
                )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.pull_thread and self.pull_thread.isRunning():
            answer = QMessageBox.question(
                self,
                "Идёт загрузка",
                "Модель ещё скачивается. Отменить загрузку и закрыть?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.pull_thread.cancel()
            if not self.pull_thread.wait(2000):
                self.pull_thread.terminate()
                self.pull_thread.wait(1000)
        event.accept()


def main() -> int:
    if "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
        os.environ.setdefault("QT_QPA_PLATFORMTHEME", "kde")
    app = QApplication(sys.argv)
    app.setApplicationName("ai-actions")
    app.setDesktopFileName("ai-actions-settings")
    app.setWindowIcon(QIcon.fromTheme("preferences-system"))
    window = SettingsWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
