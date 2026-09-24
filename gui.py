"""
Zapret GUI — простая графическая обёртка поверх уже установленного zapret.
Версия с современным дизайном на CustomTkinter + плавными анимациями.

ВАЖНО: этот файл НЕ содержит никакого кода для работы с сетевыми пакетами.
Он только запускает уже существующие .bat-файлы стратегий (general*.bat)
из папки zapret, которую ты сам укажешь на стартовом экране. Всю реальную
работу с трафиком по-прежнему делает winws.exe из твоей установки zapret —
этот скрипт им не заменяет и не модифицирует.

Остановка стратегии делается вручную — закрытием окна "zapret: ...",
которое открывает сам .bat. Автоматическая остановка сознательно убрана:
у .bat-скриптов есть свой цикл перезапуска процесса, из-за которого
попытка "убить" процесс программно ни к чему не приводит — окно всё
равно нужно закрывать самому.

Установка зависимостей (один раз):
  pip install -r requirements.txt

Запуск:
  python gui.py

Сборка в .exe: см. build.bat рядом с этим файлом.

ВАЖНО про права администратора:
  winws.exe и WinDivert обычно требуют прав администратора для работы.
  Запуск здесь сделан через ShellExecute с verb="runas", поэтому Windows
  сама покажет диалог UAC при нажатии "Запустить".
"""

import ctypes
import math
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

try:
    from easing_functions import CubicEaseOut, CubicEaseInOut, BackEaseOut
except ImportError:
    from easing_functions import CubicEaseOut, CubicEaseInOut
    BackEaseOut = CubicEaseOut

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import winreg
else:  # на других ОС модуль хотя бы импортируется — main() покажет причину
    winreg = None

REGISTRY_PATH = r"Software\ZapretGUI"
TARGET_PROCESS_NAME = "winws.exe"
MAX_RECENT = 3
MAX_LOG_LINES = 500  # лог держим в памяти, поэтому не даём ему расти бесконечно

# --- обновление сборки Flowseal/zapret-discord-youtube ---
# Версию берём из того же файла, что и service.bat в пункте "Check Updates",
# а архив — из релиза, который собирает их workflow (release.yml).
FLOWSEAL_REPO = "Flowseal/zapret-discord-youtube"
FLOWSEAL_VERSION_URL = f"https://raw.githubusercontent.com/{FLOWSEAL_REPO}/main/.service/version.txt"
FLOWSEAL_ZIP_URL = (
    "https://github.com/" + FLOWSEAL_REPO + "/releases/download/{v}/zapret-discord-youtube-{v}.zip"
)
HTTP_TIMEOUT = 15
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # релиз весит единицы мегабайт; больше — точно что-то не то
MAX_UNPACKED_BYTES = 500 * 1024 * 1024
# версия подставляется в URL, поэтому пропускаем только безобидные символы
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$")
LOCAL_VERSION_RE = re.compile(r'set\s+"LOCAL_VERSION=([^"\r\n]+)"', re.IGNORECASE)
# маркер режима "none" у ipset-all.txt — тот же, что проверяет service.bat
IPSET_NONE_MARKER = "203.0.113.113/32"

# собственные экспериментальные стратегии, которые программа кладёт в папку zapret;
# в собранном .exe PyInstaller распаковывает их во временную папку sys._MEIPASS
BUNDLED_STRATEGIES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "strategies"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# --- палитра ---
COLOR_BG = "#0b0d12"
COLOR_CARD = "#141821"
COLOR_CARD_BORDER = "#242a36"
COLOR_ACCENT = "#2ee673"
COLOR_ACCENT_HOVER = "#4dffa0"
COLOR_TEXT = "#e6e8eb"
COLOR_MUTED = "#7d8590"
COLOR_LOG_BG = "#080a0d"
COLOR_LOG_TEXT = "#c9d1d9"
COLOR_WARN_BG = "#2b2411"
COLOR_WARN_BORDER = "#5a4a12"
COLOR_WARN_TEXT = "#e3b341"
COLOR_READY_BG = "#132a1c"
COLOR_READY_BORDER = "#1e5a34"
COLOR_READY_TEXT = "#4dffa0"
COLOR_DROPDOWN_HOVER = "#1c2128"
COLOR_ROW_HOVER = "#1c2128"

CREATE_NO_WINDOW = 0x08000000
CORNER_RADIUS = 10

# длительности анимаций (мс) — короткие UI-фидбеки быстрые, крупные переходы чуть медленнее
DUR_DROPDOWN = 170
DUR_SCREEN_TRANSITION = 220
FRAME_MS = 15  # ~60 fps


def _lerp_color(c1: str, c2: str, t: float) -> str:
    c1, c2 = c1.lstrip("#"), c2.lstrip("#")
    r1, g1, b1 = int(c1[0:2], 16), int(c1[2:4], 16), int(c1[4:6], 16)
    r2, g2, b2 = int(c2[0:2], 16), int(c2[2:4], 16), int(c2[4:6], 16)
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def load_config():
    result = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            try:
                value, _ = winreg.QueryValueEx(key, "zapret_dir")
                result["zapret_dir"] = value
            except FileNotFoundError:
                pass
            try:
                value, _ = winreg.QueryValueEx(key, "recent_strategies")
                result["recent_strategies"] = [v for v in value.split("|") if v] if value else []
            except FileNotFoundError:
                pass
    except Exception:
        pass
    return result


def save_config(config):
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            if "zapret_dir" in config:
                winreg.SetValueEx(key, "zapret_dir", 0, winreg.REG_SZ, config["zapret_dir"])
            if "recent_strategies" in config:
                winreg.SetValueEx(
                    key, "recent_strategies", 0, winreg.REG_SZ, "|".join(config["recent_strategies"])
                )
    except Exception:
        pass


def is_process_running(name: str) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}"],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        return name.lower() in result.stdout.lower()
    except Exception:
        return False


# ------------------------------------------------------------------ updater


class UpdateError(Exception):
    """Ошибка обновления с текстом, который можно показать пользователю как есть."""

    keep_backup = False  # True — откат прошёл не полностью, бэкап удалять нельзя


def read_local_version(zapret_dir: Path):
    """Версия сборки Flowseal из строки set "LOCAL_VERSION=..." в service.bat.
    None — если это не их сборка (например, оригинальный zapret от bol-van)."""
    try:
        text = (zapret_dir / "service.bat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = LOCAL_VERSION_RE.search(text)
    return match.group(1).strip() if match else None


def _open_url(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "ZapretGUI", "Cache-Control": "no-cache"}
    )
    return urllib.request.urlopen(request, timeout=HTTP_TIMEOUT)


def fetch_latest_version():
    with _open_url(FLOWSEAL_VERSION_URL) as resp:
        text = resp.read(64).decode("utf-8", errors="replace").strip()
    if not VERSION_RE.match(text):
        raise UpdateError(f"Сервер вернул странную версию: {text!r}")
    return text


def _version_key(version: str):
    return tuple(int(part) for part in re.findall(r"\d+", version))


def is_newer(remote: str, local: str) -> bool:
    return _version_key(remote) > _version_key(local)


def download_file(url, dest: Path, on_progress=None):
    with _open_url(url) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            done += len(chunk)
            if done > MAX_DOWNLOAD_BYTES:
                raise UpdateError("Архив подозрительно большой — загрузка прервана.")
            f.write(chunk)
            if on_progress:
                on_progress(done, total)


def extract_zip_safely(zip_path: Path, dest: Path):
    """Распаковка с защитой от путей вида ../../ (zip slip) и zip-бомб."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        unpacked = 0
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if target != dest and dest not in target.parents:
                raise UpdateError(f"Небезопасный путь в архиве: {info.filename}")
            unpacked += info.file_size
        if unpacked > MAX_UNPACKED_BYTES:
            raise UpdateError("Архив распаковывается в слишком большой объём — отмена.")
        zf.extractall(dest)


def find_release_root(extracted: Path):
    """Папка внутри архива, где лежит сама сборка (обычно zapret-discord-youtube-<версия>/)."""
    candidates = [extracted] + sorted(p for p in extracted.iterdir() if p.is_dir())
    for candidate in candidates:
        if (candidate / "service.bat").is_file() and (candidate / "bin" / TARGET_PROCESS_NAME).is_file():
            return candidate
    return None


def _ipset_mode_is_user_chosen(ipset_file: Path) -> bool:
    """True, если пользователь переключил ipset в режим none/any через service.bat.
    Логика та же, что в :ipset_switch_status: пустой файл — any, маркер — none."""
    try:
        content = ipset_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return not content.strip() or IPSET_NONE_MARKER in content


def build_install_plan(new_root: Path, zapret_dir: Path):
    """Список (файл из релиза, куда его положить относительно папки zapret).

    Пользовательское состояние не трогаем:
      * lists/*-user.txt и utils/game_filter.enabled в релиз не входят — их и не перезапишет;
      * utils/check_updates.enabled не возвращаем, если пользователь его удалил (выключил проверку);
      * bin/ACTIVE_*.bin — фейки, выбранные через "Replace active fakes", — оставляем свои;
      * ipset в режиме none/any оставляем как есть, а свежий список кладём в .backup —
        ровно туда, откуда service.bat достаёт его при переключении в loaded.
    """
    ipset_rel = Path("lists", "ipset-all.txt")
    ipset_backup_rel = Path("lists", "ipset-all.txt.backup")
    keep_ipset_mode = _ipset_mode_is_user_chosen(zapret_dir / ipset_rel)

    plan = []
    for src in sorted(new_root.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(new_root)
        if rel == Path("utils", "check_updates.enabled") and not (zapret_dir / rel).exists():
            continue
        if rel.parent == Path("bin") and rel.name.startswith("ACTIVE_") and (zapret_dir / rel).exists():
            continue
        if keep_ipset_mode:
            if rel == ipset_backup_rel:
                continue
            if rel == ipset_rel:
                rel = ipset_backup_rel
        plan.append((src, rel))
    return plan


def apply_install_plan(plan, zapret_dir: Path, backup_dir: Path):
    """Копирует файлы поверх папки zapret. Всё, что перезаписывается, сначала
    сохраняется в backup_dir; при любой ошибке изменения откатываются."""
    replaced, created = [], []
    try:
        for src, rel in plan:
            dst = zapret_dir / rel
            if dst.exists():
                saved = backup_dir / rel
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, saved)
                replaced.append(rel)
            else:
                created.append(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    except Exception as e:
        rollback_errors = []
        for rel in replaced:
            try:
                shutil.copy2(backup_dir / rel, zapret_dir / rel)
            except Exception as re_err:
                rollback_errors.append(f"{rel}: {re_err}")
        for rel in created:
            try:
                (zapret_dir / rel).unlink(missing_ok=True)
            except Exception as re_err:
                rollback_errors.append(f"{rel}: {re_err}")

        hint = ""
        if isinstance(e, PermissionError):
            hint = (
                "\n\nФайл занят или нет прав на запись. Закрой окно \"zapret: ...\", "
                "удали службы через service.bat (Remove Services) и попробуй снова. "
                "Если папка лежит в Program Files — перенеси её, например, в C:\\zapret."
            )
        if rollback_errors:
            err = UpdateError(
                f"Обновление не удалось: {e}\n\nОткатить удалось не всё — копии старых файлов "
                f"лежат в {backup_dir}:\n" + "\n".join(rollback_errors[:10]) + hint
            )
            err.keep_backup = True
            raise err from e
        raise UpdateError(f"Обновление не удалось, изменения откачены.\n\n{e}{hint}") from e
    return replaced, created


def bundled_strategy_names():
    try:
        return sorted(p.name for p in BUNDLED_STRATEGIES_DIR.glob("general*.bat"))
    except OSError:
        return []


def install_bundled_strategies(zapret_dir: Path):
    """Копирует стратегии программы в папку zapret, если их там нет или они устарели.
    Возвращает имена скопированных файлов."""
    installed = []
    for name in bundled_strategy_names():
        src = BUNDLED_STRATEGIES_DIR / name
        dst = zapret_dir / name
        data = src.read_bytes()
        if dst.is_file() and dst.read_bytes() == data:
            continue
        dst.write_bytes(data)
        installed.append(name)
    return installed


def is_service_installed(name: str) -> bool:
    try:
        result = subprocess.run(
            ["sc", "query", name], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
        )
        return result.returncode == 0
    except Exception:
        return False


def update_zapret(zapret_dir: Path, version: str, emit):
    """Скачивает релиз Flowseal и ставит его поверх zapret_dir. Работает в фоновом
    потоке, поэтому с интерфейсом общается только через emit(kind, text)."""
    if not VERSION_RE.match(version):
        raise UpdateError(f"Некорректная версия: {version!r}")

    with tempfile.TemporaryDirectory(prefix="zapretgui-update-") as tmp:
        tmp = Path(tmp)
        zip_path = tmp / "release.zip"
        url = FLOWSEAL_ZIP_URL.format(v=version)
        emit("log", f"Скачиваю {url}")

        last_pct = [-1]

        def on_progress(done, total):
            pct = done * 100 // total if total else -1
            if pct != last_pct[0]:
                last_pct[0] = pct
                emit("progress", f"Загрузка… {pct}%" if pct >= 0 else f"Загрузка… {done // 1024} КБ")

        try:
            download_file(url, zip_path, on_progress)
        except UpdateError:
            raise
        except Exception as e:
            raise UpdateError(f"Не удалось скачать релиз {version}: {e}") from e

        emit("progress", "Распаковка…")
        extracted = tmp / "extracted"
        try:
            extract_zip_safely(zip_path, extracted)
        except zipfile.BadZipFile as e:
            raise UpdateError(f"Скачанный архив повреждён: {e}") from e

        new_root = find_release_root(extracted)
        if new_root is None:
            raise UpdateError(
                "В архиве не нашлось service.bat и bin\\winws.exe — структура релиза изменилась."
            )
        new_version = read_local_version(new_root)
        if new_version != version:
            raise UpdateError(f"В архиве версия {new_version!r}, а ожидалась {version!r}.")

        # проверяем ещё раз прямо перед копированием: пока шла загрузка, стратегию могли запустить
        if is_process_running(TARGET_PROCESS_NAME):
            raise UpdateError("winws.exe запущен — закрой окно \"zapret: ...\" и повтори обновление.")

        emit("progress", "Установка…")
        plan = build_install_plan(new_root, zapret_dir)
        # бэкап вне временной папки: если откат не удастся, копии старых файлов
        # должны пережить выход из with, иначе сообщению об ошибке не на что сослаться
        backup_dir = Path(tempfile.mkdtemp(prefix="zapretgui-backup-"))
        try:
            replaced, created = apply_install_plan(plan, zapret_dir, backup_dir)
        except UpdateError as e:
            if not e.keep_backup:
                shutil.rmtree(backup_dir, ignore_errors=True)
            raise
        shutil.rmtree(backup_dir, ignore_errors=True)

        new_bats = {p.name for p in new_root.glob("general*.bat")} | set(bundled_strategy_names())
        orphaned = sorted(p.name for p in zapret_dir.glob("general*.bat") if p.name not in new_bats)

    emit("log", f"Обновлено файлов: {len(replaced)}, добавлено новых: {len(created)}.")
    if orphaned:
        emit("log", "Этих стратегий нет в новой версии, оставил как есть: " + ", ".join(orphaned))
    if is_service_installed("zapret"):
        emit(
            "log",
            "Установлена служба zapret со старыми параметрами — переустанови её "
            "через service.bat (Install Service), чтобы она подхватила новую версию.",
        )
    return version


def animate(widget, duration_ms, on_step, on_done=None, easing_cls=CubicEaseOut):
    """Универсальный помощник анимации: гоняет t от 0 до 1 через easing-кривую
    и на каждом кадре вызывает on_step(eased_t)."""
    ease = easing_cls(start=0, end=1, duration=duration_ms)
    steps = max(1, duration_ms // FRAME_MS)

    def step(i=0):
        if not widget.winfo_exists():
            return
        t = min(i / steps, 1.0)
        eased = ease(t * duration_ms)
        on_step(eased)
        if i < steps:
            widget.after(FRAME_MS, lambda: step(i + 1))
        elif on_done:
            on_done()

    step()


class AnimatedDropdown(ctk.CTkFrame):
    """Кнопка-селект с собственным всплывающим списком, который плавно
    выезжает вниз и проявляется (fade + slide), вместо резкого появления
    как у стандартного CTkOptionMenu."""

    def __init__(self, master, values=None, command=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.values = values or [""]
        self.command = command
        self.popup = None
        self._var = tk.StringVar(value=self.values[0] if self.values else "")

        self.button = ctk.CTkButton(
            self, text=self._display_text(), anchor="w",
            corner_radius=CORNER_RADIUS, fg_color="#21262d", hover_color="#2a3038",
            text_color=COLOR_TEXT, font=ctk.CTkFont(size=13),
            command=self.toggle,
        )
        self.button.pack(fill="both", expand=True)

        # Вместо <FocusOut> (ненадёжно закрывает попап при отпускании
        # скроллбара на overrideredirect-окнах) — глобальный обработчик клика,
        # который закрывает список только если клик реально произошёл вне него.
        # Вешается ровно один раз на всё окно: если делать это при каждом
        # открытии списка, обработчики накапливаются на каждый клик.
        self.winfo_toplevel().bind_all("<Button-1>", self._on_global_click, add="+")

    def _on_global_click(self, event):
        if self.popup is None:
            return
        w = event.widget
        while w is not None:
            if w == self.popup or w == self.button:
                return
            try:
                w = w.master
            except Exception:
                break
        self.close()

    def _display_text(self):
        val = self._var.get()
        return f"{val}   ▾" if val else "нет доступных стратегий   ▾"

    def get(self):
        return self._var.get()

    def set(self, value):
        self._var.set(value)
        self.button.configure(text=self._display_text())

    def configure_values(self, values):
        self.values = values or [""]
        # если выбранная стратегия никуда не делась — не сбрасываем выбор
        current = self._var.get()
        self.set(current if current in self.values else self.values[0])

    def toggle(self):
        if self.popup is not None:
            self.close()
        else:
            self.open()

    def open(self):
        root = self.winfo_toplevel()
        x = self.button.winfo_rootx()
        y = self.button.winfo_rooty() + self.button.winfo_height() + 4
        width = self.button.winfo_width()

        row_h = 38
        max_visible = 8
        visible_count = min(len(self.values), max_visible)
        final_height = visible_count * row_h + 8

        self.popup = tk.Toplevel(root)
        self.popup.overrideredirect(True)
        self.popup.attributes("-topmost", True)
        try:
            self.popup.attributes("-alpha", 0.0)
        except Exception:
            pass
        self.popup.configure(bg=COLOR_CARD_BORDER)
        self.popup.geometry(f"{width}x1+{x}+{y}")

        outer = tk.Frame(self.popup, bg=COLOR_CARD_BORDER)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        canvas = tk.Canvas(outer, bg="#1c2128", highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg="#1c2128")

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw", width=width - 2)
        canvas.configure(yscrollcommand=scrollbar.set)

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<MouseWheel>", on_mousewheel)
        inner.bind("<MouseWheel>", on_mousewheel)

        for i, val in enumerate(self.values):
            row = tk.Label(
                inner, text=val, bg="#1c2128", fg=COLOR_TEXT,
                font=("Segoe UI", 13), anchor="w", padx=14, pady=9, cursor="hand2",
            )
            row.pack(fill="x")
            row.bind("<Enter>", lambda e, r=row: r.configure(bg=COLOR_DROPDOWN_HOVER))
            row.bind("<Leave>", lambda e, r=row: r.configure(bg="#1c2128"))
            row.bind("<Button-1>", lambda e, v=val: self._pick(v))
            row.bind("<MouseWheel>", on_mousewheel)

            if i < len(self.values) - 1:
                divider = tk.Frame(inner, bg=COLOR_CARD_BORDER, height=1)
                divider.pack(fill="x")

        canvas.pack(side="left", fill="both", expand=True)
        if len(self.values) > max_visible:
            scrollbar.pack(side="right", fill="y")

        self.popup.focus_force()

        def on_step(t):
            h = max(1, int(final_height * t))
            self.popup.geometry(f"{width}x{h}+{x}+{y}")
            try:
                self.popup.attributes("-alpha", t)
            except Exception:
                pass

        animate(self.popup, DUR_DROPDOWN, on_step, easing_cls=CubicEaseOut)

    def _pick(self, value):
        self.set(value)
        if self.command:
            self.command(value)
        self.close()

    def close(self):
        if self.popup is None:
            return
        popup = self.popup
        self.popup = None
        try:
            width = popup.winfo_width()
            x = popup.winfo_x()
            y = popup.winfo_y()
            start_h = popup.winfo_height()

            def on_step(t):
                remaining = 1 - t
                h = max(1, int(start_h * remaining))
                popup.geometry(f"{width}x{h}+{x}+{y}")
                try:
                    popup.attributes("-alpha", remaining)
                except Exception:
                    pass

            def on_done():
                try:
                    popup.destroy()
                except Exception:
                    pass

            animate(popup, DUR_DROPDOWN, on_step, on_done=on_done, easing_cls=CubicEaseOut)
        except Exception:
            try:
                popup.destroy()
            except Exception:
                pass


class ZapretGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Zapret GUI")
        self.geometry("700x560")
        self.minsize(580, 480)
        self.configure(fg_color=COLOR_BG)

        self.config_data = load_config()
        self.zapret_dir = None
        self._log_tag_counter = 0
        self._log_history = []
        self.log_area = None
        self._log_popup = None
        self.recent_strategies = self.config_data.get("recent_strategies", [])
        self._updating = False
        self._latest_version = None

        try:
            self.attributes("-alpha", 0.0)
        except Exception:
            pass

        self.build_ui()
        self.main_container.grid_remove()

        self.show_welcome_screen()

    # ---------------------------------------------------------------- welcome

    def show_welcome_screen(self):
        self.welcome = ctk.CTkFrame(self, fg_color=COLOR_BG)
        self.welcome.place(x=0, y=0, relwidth=1, relheight=1)

        center = ctk.CTkFrame(self.welcome, fg_color="transparent")
        center.place(relx=0.5, rely=0.46, anchor="center")
        self.welcome_center = center

        self.welcome_icon = ctk.CTkLabel(center, text="🛡️", font=("Segoe UI Emoji", 72))
        self.welcome_icon.pack(pady=(0, 18))

        self.welcome_title = ctk.CTkLabel(
            center, text="Zapret GUI", font=ctk.CTkFont(size=26, weight="bold"), text_color=COLOR_TEXT
        )
        self.welcome_title.pack()

        self.welcome_sub = ctk.CTkLabel(
            center, text="управление стратегиями обхода DPI",
            font=ctk.CTkFont(size=13), text_color=COLOR_MUTED
        )
        self.welcome_sub.pack(pady=(2, 22))

        note = ctk.CTkFrame(
            center, corner_radius=CORNER_RADIUS, fg_color=COLOR_CARD,
            border_width=1, border_color=COLOR_CARD_BORDER
        )
        note.pack(fill="x", pady=(0, 22))
        note_inner = ctk.CTkFrame(note, fg_color="transparent")
        note_inner.pack(padx=18, pady=16)

        ctk.CTkLabel(
            note_inner, text="Перед началом работы",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=COLOR_TEXT, anchor="w"
        ).pack(anchor="w")
        ctk.CTkLabel(
            note_inner,
            text="На следующем шаге нужно будет указать папку, куда распакован zapret —\n"
                 "именно там, где лежат файлы general*.bat и service.bat.",
            font=ctk.CTkFont(size=12), text_color=COLOR_MUTED, anchor="w", justify="left"
        ).pack(anchor="w", pady=(4, 0))

        self.welcome_btn = ctk.CTkButton(
            center, text="Продолжить →", width=220, height=42,
            corner_radius=CORNER_RADIUS, fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            text_color="#0b0d12", font=ctk.CTkFont(size=14, weight="bold"),
            command=self.continue_from_welcome,
        )
        self.welcome_btn.pack()

        footer = ctk.CTkLabel(
            self.welcome, text="EliteSpirit",
            font=ctk.CTkFont(size=11), text_color=COLOR_MUTED
        )
        footer.place(relx=0.5, rely=0.96, anchor="center")

        # стартовая поза для входной анимации: чуть ниже и прозрачно
        center.place(relx=0.5, rely=0.5, anchor="center")
        self.update_idletasks()

        def on_step(t):
            try:
                self.attributes("-alpha", t)
            except Exception:
                pass
            rely = 0.5 + (1 - t) * 0.04
            center.place(relx=0.5, rely=rely, anchor="center")

        animate(self, DUR_SCREEN_TRANSITION, on_step, easing_cls=CubicEaseOut)

    def continue_from_welcome(self):
        self.welcome_center.place_forget()

        transition_label = ctk.CTkLabel(self.welcome, text="🚫", font=("Segoe UI Emoji", 10))
        transition_label.place(relx=0.5, rely=0.46, anchor="center")

        def set_size(size):
            try:
                transition_label.configure(font=("Segoe UI Emoji", max(1, int(size))))
            except Exception:
                pass

        def set_offset(dx):
            transition_label.place(relx=0.5, rely=0.46, anchor="center", x=dx)

        # 1. 🚫 выскакивает (pop-in)
        def pop_in_step(t):
            set_size(10 + t * 85)

        # 2. лёгкое покачивание влево-вправо с затуханием — как будто "нет-нет" (через сдвиг позиции,
        # потому что цветные эмодзи на Windows — растровые и не вращаются)
        def wobble_step(t):
            decay = 1 - t
            dx = math.sin(t * math.pi * 5) * 14 * decay
            set_offset(dx)
            set_size(95 + math.sin(t * math.pi * 3) * 4 * decay)

        # 3. сжимается почти до нуля
        def shrink_step(t):
            set_offset(0)
            set_size(95 - t * 93)

        # 4. иконка щита плавно «выстреливает» с небольшим перерастяжением (BackEaseOut)
        def grow_step(t):
            set_size(2 + t * 96)

        def start_wobble():
            animate(self.welcome, 240, wobble_step, on_done=start_shrink, easing_cls=CubicEaseOut)

        def start_shrink():
            animate(self.welcome, 130, shrink_step, on_done=swap_to_shield, easing_cls=CubicEaseInOut)

        def swap_to_shield():
            transition_label.configure(text="🛡️")
            animate(self.welcome, 280, grow_step, on_done=finish_soon, easing_cls=BackEaseOut)

        def finish_soon():
            self.after(120, finalize)

        def finalize():
            try:
                transition_label.destroy()
            except Exception:
                pass
            self.welcome.destroy()
            self.main_container.grid()
            self.load_or_ask_directory()
            self._pulse_phase = 0
            self.poll_status()
            self.animate_pulse()

        animate(self.welcome, 150, pop_in_step, on_done=start_wobble, easing_cls=CubicEaseOut)

    # ------------------------------------------------------------------ main

    def build_ui(self):
        self.main_container = ctk.CTkFrame(self, fg_color="transparent")
        self.main_container.grid(row=0, column=0, sticky="nsew")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.main_container.grid_columnconfigure(0, weight=1)

        # --- Акцентная полоска сверху, отражает общий статус ---
        self.top_accent = ctk.CTkFrame(
            self.main_container, height=3, corner_radius=0, fg_color=COLOR_CARD_BORDER
        )
        self.top_accent.grid(row=0, column=0, sticky="ew")
        self.main_container.grid_rowconfigure(0, weight=0)

        # --- Header ---
        header = ctk.CTkFrame(self.main_container, fg_color="transparent")
        header.grid(row=1, column=0, sticky="ew", padx=24, pady=(20, 16))
        header.grid_columnconfigure(0, weight=1)

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_row, text="🛡️", font=("Segoe UI Emoji", 28)
        ).pack(side="left", padx=(0, 10))

        title_col = ctk.CTkFrame(title_row, fg_color="transparent")
        title_col.pack(side="left")

        ctk.CTkLabel(
            title_col, text="Zapret", font=ctk.CTkFont(size=22, weight="bold"), text_color=COLOR_TEXT
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_col, text="управление стратегиями обхода DPI",
            font=ctk.CTkFont(size=12), text_color=COLOR_MUTED
        ).pack(anchor="w")

        # правый верхний угол: кнопка лога + статус-бейдж
        right_cluster = ctk.CTkFrame(header, fg_color="transparent")
        right_cluster.grid(row=0, column=1, sticky="e")

        self.log_btn = ctk.CTkButton(
            right_cluster, text="📋 лог", width=76, height=28, corner_radius=CORNER_RADIUS,
            fg_color="#1c2128", hover_color="#262c36", text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=12), command=self.open_log_popup,
        )
        self.log_btn.pack(side="left", padx=(0, 8))

        self.status_badge = ctk.CTkFrame(
            right_cluster, corner_radius=999, fg_color="#1c2128",
            border_width=1, border_color=COLOR_CARD_BORDER,
        )
        self.status_badge.pack(side="left")
        badge_inner = ctk.CTkFrame(self.status_badge, fg_color="transparent")
        badge_inner.pack(padx=14, pady=6)
        self.status_dot = ctk.CTkLabel(
            badge_inner, text="●", text_color=COLOR_MUTED, font=ctk.CTkFont(size=13)
        )
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_label = ctk.CTkLabel(
            badge_inner, text="остановлено", text_color=COLOR_MUTED, font=ctk.CTkFont(size=12)
        )
        self.status_label.pack(side="left")

        # --- Directory card ---
        dir_card = self._card()
        dir_card.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 14))
        dir_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            dir_card, text="ПАПКА ZAPRET", font=ctk.CTkFont(size=10, weight="bold"), text_color=COLOR_MUTED
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 0))

        self.dir_label = ctk.CTkLabel(
            dir_card, text="Папка не выбрана", anchor="w", font=ctk.CTkFont(size=13), text_color=COLOR_TEXT
        )
        self.dir_label.grid(row=1, column=0, sticky="ew", padx=16, pady=(2, 14))

        ctk.CTkButton(
            dir_card, text="Изменить", width=100, corner_radius=CORNER_RADIUS,
            fg_color="#21262d", hover_color="#30363d", text_color=COLOR_TEXT,
            command=self.choose_directory
        ).grid(row=1, column=1, padx=(8, 8), pady=(2, 14))

        ctk.CTkButton(
            dir_card, text="⚙ service.bat", width=130, corner_radius=CORNER_RADIUS,
            fg_color="#21262d", hover_color="#30363d", text_color=COLOR_TEXT,
            command=self.open_service_bat
        ).grid(row=1, column=2, padx=(0, 16), pady=(2, 14))

        # строка версии: что стоит локально и что лежит в релизах Flowseal
        self.version_label = ctk.CTkLabel(
            dir_card, text="", anchor="w", font=ctk.CTkFont(size=12), text_color=COLOR_MUTED
        )
        self.version_label.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))

        self.update_btn = ctk.CTkButton(
            dir_card, text="⬇ Проверить обновления", width=238, corner_radius=CORNER_RADIUS,
            fg_color="#21262d", hover_color="#30363d", text_color=COLOR_TEXT,
            command=self.check_and_update,
        )
        self.update_btn.grid(row=2, column=1, columnspan=2, sticky="e", padx=(8, 16), pady=(0, 14))

        # --- Strategy card ---
        strategy_card = self._card()
        strategy_card.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 14))
        strategy_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            strategy_card, text="СТРАТЕГИЯ", font=ctk.CTkFont(size=10, weight="bold"), text_color=COLOR_MUTED
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 4))

        self.strategy_dropdown = AnimatedDropdown(strategy_card, values=[""])
        self.strategy_dropdown.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))

        self.start_btn = ctk.CTkButton(
            strategy_card,
            text="▶  Запустить",
            corner_radius=CORNER_RADIUS,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#0b0d12",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.start_strategy,
        )
        self.start_btn.grid(row=1, column=1, padx=(0, 16), pady=(0, 14))

        # --- Динамическая плашка-подсказка ---
        self.hint_card = ctk.CTkFrame(
            self.main_container, corner_radius=CORNER_RADIUS, fg_color=COLOR_WARN_BG,
            border_width=1, border_color=COLOR_WARN_BORDER
        )
        self.hint_card.grid(row=4, column=0, sticky="ew", padx=24, pady=(0, 14))
        self.hint_card.grid_columnconfigure(1, weight=1)

        self.hint_icon = ctk.CTkLabel(self.hint_card, text="⚠️", font=ctk.CTkFont(size=16))
        self.hint_icon.grid(row=0, column=0, padx=(14, 8), pady=12)

        self.hint_text = ctk.CTkLabel(
            self.hint_card,
            text="",
            font=ctk.CTkFont(size=12), text_color=COLOR_WARN_TEXT,
            anchor="w", justify="left", wraplength=520
        )
        self.hint_text.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=12)

        self.update_hint()

        # --- Recent strategies card (на месте бывшего лога) ---
        recent_card = self._card()
        recent_card.grid(row=5, column=0, sticky="nsew", padx=24, pady=(0, 24))
        self.main_container.grid_rowconfigure(5, weight=1)
        recent_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            recent_card, text="ПОСЛЕДНИЕ СТРАТЕГИИ", font=ctk.CTkFont(size=10, weight="bold"),
            text_color=COLOR_MUTED
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 8))

        self.recent_list_frame = ctk.CTkFrame(recent_card, fg_color="transparent")
        self.recent_list_frame.grid(row=1, column=0, sticky="new", padx=12, pady=(0, 14))
        self.recent_list_frame.grid_columnconfigure(0, weight=1)

        self.refresh_recent_ui()

    def _card(self):
        return ctk.CTkFrame(
            self.main_container, corner_radius=CORNER_RADIUS, fg_color=COLOR_CARD,
            border_width=1, border_color=COLOR_CARD_BORDER
        )

    # -------------------------------------------------------------- recent

    def refresh_recent_ui(self):
        for w in self.recent_list_frame.winfo_children():
            w.destroy()

        if not self.recent_strategies:
            ctk.CTkLabel(
                self.recent_list_frame, text="Здесь появятся последние запущенные стратегии",
                font=ctk.CTkFont(size=12), text_color=COLOR_MUTED, anchor="w"
            ).grid(row=0, column=0, sticky="w", padx=4, pady=6)
            return

        for i, name in enumerate(self.recent_strategies[:MAX_RECENT]):
            row = ctk.CTkFrame(self.recent_list_frame, corner_radius=CORNER_RADIUS, fg_color="#1c2128")
            row.grid(row=i, column=0, sticky="ew", pady=(0, 8))
            row.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                row, text=name, font=ctk.CTkFont(size=13), text_color=COLOR_TEXT, anchor="w"
            ).grid(row=0, column=0, sticky="ew", padx=14, pady=10)

            ctk.CTkButton(
                row, text="▶  запустить снова", width=150, height=28, corner_radius=CORNER_RADIUS,
                fg_color="#262c36", hover_color="#30363d", text_color=COLOR_TEXT,
                font=ctk.CTkFont(size=11), command=lambda n=name: self.launch_strategy_by_name(n),
            ).grid(row=0, column=1, padx=(0, 10), pady=6)

    def remember_recent(self, name):
        self.recent_strategies = [name] + [s for s in self.recent_strategies if s != name]
        self.recent_strategies = self.recent_strategies[:MAX_RECENT]
        self.config_data["recent_strategies"] = self.recent_strategies
        save_config(self.config_data)
        self.refresh_recent_ui()

    def launch_strategy_by_name(self, name):
        if self.zapret_dir and (self.zapret_dir / name).exists():
            self.strategy_dropdown.set(name)
            self.start_strategy()
        else:
            messagebox.showwarning("Файл не найден", f"Стратегии \"{name}\" больше нет в текущей папке.")

    # ---------------------------------------------------------------- log

    def open_log_popup(self):
        if self._log_popup is not None and self._log_popup.winfo_exists():
            self._log_popup.lift()
            self._log_popup.focus_force()
            return

        popup = ctk.CTkToplevel(self)
        popup.title("Лог")
        popup.geometry("520x360")
        popup.configure(fg_color=COLOR_BG)
        self._log_popup = popup

        header = ctk.CTkFrame(popup, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(
            header, text="ЛОГ", font=ctk.CTkFont(size=10, weight="bold"), text_color=COLOR_MUTED
        ).pack(side="left")
        ctk.CTkButton(
            header, text="очистить", width=70, height=24, corner_radius=CORNER_RADIUS,
            fg_color="transparent", hover_color="#21262d", text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=11), command=self.clear_log
        ).pack(side="right")

        self.log_area = ctk.CTkTextbox(
            popup, fg_color=COLOR_LOG_BG, text_color=COLOR_LOG_TEXT,
            font=ctk.CTkFont(family="Consolas", size=12), corner_radius=CORNER_RADIUS,
            border_width=1, border_color=COLOR_CARD_BORDER
        )
        self.log_area.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.log_area.configure(state="normal")
        for line in self._log_history:
            self.log_area.insert("end", line + "\n")
        self.log_area.configure(state="disabled")
        self.log_area.see("end")

        def on_close():
            self._log_popup = None
            self.log_area = None
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", on_close)

    def log(self, text):
        self._log_history.append(text)
        if len(self._log_history) > MAX_LOG_LINES:
            del self._log_history[:-MAX_LOG_LINES]

        if self.log_area is None:
            return

        self.log_area.configure(state="normal")

        tag_name = f"line_{self._log_tag_counter}"
        self._log_tag_counter += 1

        start_index = self.log_area.index("end-1c")
        self.log_area.insert("end", text + "\n")
        end_index = self.log_area.index("end-1c")

        # Новая строка на мгновение ярче, потом гаснет до обычного цвета —
        # мягкий сигнал "что-то только что произошло", без отвлекающей анимации.
        self.log_area.tag_add(tag_name, start_index, end_index)
        self.log_area.tag_config(tag_name, foreground=COLOR_ACCENT_HOVER)
        self.after(450, lambda t=tag_name: self._settle_log_tag(t))

        self.log_area.see("end")
        self.log_area.configure(state="disabled")

    def _settle_log_tag(self, tag_name):
        try:
            if self.log_area is None:
                return
            self.log_area.configure(state="normal")
            self.log_area.tag_config(tag_name, foreground=COLOR_LOG_TEXT)
            self.log_area.configure(state="disabled")
        except Exception:
            pass

    def clear_log(self):
        self._log_history = []
        if self.log_area is not None:
            self.log_area.configure(state="normal")
            self.log_area.delete("1.0", "end")
            self.log_area.configure(state="disabled")

    # ------------------------------------------------------------ directory

    def load_or_ask_directory(self):
        saved = self.config_data.get("zapret_dir")
        if saved and Path(saved).exists():
            self.set_directory(Path(saved))
        else:
            self.choose_directory()

    def choose_directory(self):
        if self._updating:
            messagebox.showinfo("Идёт обновление", "Дождись окончания обновления zapret.")
            return
        chosen = filedialog.askdirectory(title="Выбери папку с zapret (где general*.bat и service.bat)")
        if chosen:
            self.set_directory(Path(chosen))

    def set_directory(self, path: Path):
        self.zapret_dir = path
        self.dir_label.configure(text=str(path))
        self.config_data["zapret_dir"] = str(path)
        save_config(self.config_data)
        self.install_own_strategies()
        self.refresh_strategies()
        self.update_hint()
        self.refresh_version_info(check_remote=True)

    def install_own_strategies(self):
        # стратегии программы опираются на подпрограммы service.bat от Flowseal
        # (load_game_filter и т.п.), в других сборках они просто не запустятся
        if read_local_version(self.zapret_dir) is None:
            return
        try:
            for name in install_bundled_strategies(self.zapret_dir):
                self.log(f"Добавлена экспериментальная стратегия: {name}")
        except OSError as e:
            self.log(f"Не удалось добавить стратегии программы: {e}")

    def refresh_strategies(self):
        if not self.zapret_dir:
            return
        bat_files = sorted(self.zapret_dir.glob("general*.bat"))
        names = [f.name for f in bat_files]

        if names:
            self.strategy_dropdown.configure_values(names)
            self.log(f"Найдено стратегий: {len(names)}")
        else:
            self.strategy_dropdown.configure_values([""])
            self.log("В выбранной папке не найдено файлов general*.bat")

    # ------------------------------------------------------------- actions

    def start_strategy(self):
        if self._updating:
            messagebox.showinfo("Идёт обновление", "Дождись окончания обновления zapret.")
            return
        if is_process_running(TARGET_PROCESS_NAME):
            messagebox.showinfo(
                "Уже запущено",
                "winws.exe уже работает.\n\nЧтобы запустить другую стратегию — "
                "сначала закрой окно \"zapret: ...\"."
            )
            return

        strategy = self.strategy_dropdown.get()
        if not strategy:
            messagebox.showwarning("Нет стратегии", "Выбери стратегию из списка.")
            return

        bat_path = self.zapret_dir / strategy
        if not bat_path.exists():
            messagebox.showerror("Файл не найден", f"Не найден: {bat_path}")
            return

        self.log(f"Запускаю: {strategy}")
        # Мгновенная обратная связь на клик — показываем, что нажатие услышано,
        # не ждём реального результата (UAC может подвиснуть на секунду-две).
        self.start_btn.configure(state="disabled", text="Запускается…")
        self.update_idletasks()
        try:
            # Запуск через ShellExecute с verb="runas" — это явно вызывает
            # диалог UAC (обычный subprocess.Popen этого не делает и может
            # тихо не сработать, если winws.exe требует повышенных прав).
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", str(bat_path), None, str(self.zapret_dir), 1
            )
            if result <= 32:
                self.log(f"Не удалось запустить (код ошибки {result}) — возможно, UAC отклонён.")
            else:
                self.log("Запущено. Чтобы остановить — закрой открывшееся окно \"zapret: ...\".")
                self.remember_recent(strategy)
        except Exception as e:
            self.log(f"Ошибка запуска: {e}")
            messagebox.showerror("Ошибка", str(e))

    def open_service_bat(self):
        if not self.zapret_dir:
            messagebox.showwarning("Нет папки", "Сначала выбери папку с zapret.")
            return

        service_bat = self.zapret_dir / "service.bat"
        if not service_bat.exists():
            messagebox.showerror("Файл не найден", f"Не найден: {service_bat}")
            return

        self.log("Открываю service.bat (настройки/диагностика zapret)...")
        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", str(service_bat), None, str(self.zapret_dir), 1
            )
            if result <= 32:
                self.log(f"Не удалось открыть service.bat (код ошибки {result}).")
        except Exception as e:
            self.log(f"Ошибка открытия service.bat: {e}")
            messagebox.showerror("Ошибка", str(e))

    # ------------------------------------------------------------- update

    def _run_in_background(self, work, on_ok, on_err):
        """Запускает work(emit) в потоке. Tk не потокобезопасен, поэтому поток
        только кладёт события в очередь, а виджеты трогает главный цикл."""
        events = queue.Queue()

        def runner():
            try:
                events.put(("ok", work(lambda kind, text: events.put((kind, text)))))
            except Exception as e:
                events.put(("err", e))

        threading.Thread(target=runner, daemon=True).start()

        def pump():
            try:
                while True:
                    kind, value = events.get_nowait()
                    if kind == "log":
                        self.log(value)
                    elif kind == "progress":
                        self.update_btn.configure(text=value)
                    elif kind == "ok":
                        on_ok(value)
                        return
                    elif kind == "err":
                        on_err(value)
                        return
            except queue.Empty:
                pass
            self.after(100, pump)

        self.after(100, pump)

    def refresh_version_info(self, check_remote=False):
        local = read_local_version(self.zapret_dir) if self.zapret_dir else None
        if not self.zapret_dir:
            text = ""
        elif local is None:
            text = "версия неизвестна — это не сборка Flowseal"
        elif self._latest_version and is_newer(self._latest_version, local):
            text = f"версия {local}  ·  доступна {self._latest_version}"
        elif self._latest_version:
            text = f"версия {local}  ·  последняя"
        else:
            text = f"версия {local}"
        self.version_label.configure(text=text)

        if self._updating:
            return
        if local and self._latest_version and is_newer(self._latest_version, local):
            self.update_btn.configure(
                text=f"⬇ Обновить до {self._latest_version}",
                fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER, text_color="#0b0d12",
            )
        else:
            self.update_btn.configure(
                text="⬇ Проверить обновления",
                fg_color="#21262d", hover_color="#30363d", text_color=COLOR_TEXT,
            )

        if check_remote and local:
            # тихая проверка при выборе папки: без диалогов, ошибка сети — только строка в логе
            def on_ok(latest):
                self._latest_version = latest
                self.refresh_version_info()

            def on_err(e):
                self.log(f"Не удалось проверить обновления zapret: {e}")

            self._run_in_background(lambda emit: fetch_latest_version(), on_ok, on_err)

    def _set_updating(self, updating):
        self._updating = updating
        self.update_btn.configure(state="disabled" if updating else "normal")
        if updating:
            self.start_btn.configure(state="disabled", text="Обновление…")

    def check_and_update(self):
        if self._updating:
            return
        if not self.zapret_dir:
            messagebox.showwarning("Нет папки", "Сначала выбери папку с zapret.")
            return

        local = read_local_version(self.zapret_dir)
        if local is None:
            messagebox.showerror(
                "Не сборка Flowseal",
                "В service.bat не нашлось строки LOCAL_VERSION — похоже, это не сборка "
                "Flowseal/zapret-discord-youtube.\n\nОбновлять её их релизом небезопасно: "
                "файлы разных сборок перемешаются.",
            )
            return

        self._set_updating(True)
        self.update_btn.configure(text="Проверяю…")
        self.log("Проверяю последнюю версию zapret-discord-youtube…")

        def on_err(e):
            self._set_updating(False)
            self.refresh_version_info()
            self.log(f"Не удалось проверить обновления: {e}")
            messagebox.showerror("Ошибка", f"Не удалось проверить обновления:\n{e}")

        self._run_in_background(
            lambda emit: fetch_latest_version(), lambda latest: self._offer_update(local, latest), on_err
        )

    def _offer_update(self, local, latest):
        self._latest_version = latest
        self._set_updating(False)
        self.refresh_version_info()

        if not is_newer(latest, local):
            self.log(f"Установлена последняя версия: {local}")
            messagebox.showinfo("Обновлений нет", f"У тебя последняя версия zapret: {local}.")
            return

        if not messagebox.askyesno(
            "Доступно обновление",
            f"Установлена версия {local}, доступна {latest}.\n\n"
            f"Скачать релиз с github.com/{FLOWSEAL_REPO} и установить поверх текущей папки?\n\n"
            "Твои списки (*-user.txt), настройки game filter и режим ipset сохранятся. "
            "Если что-то пойдёт не так, изменения откатятся.",
        ):
            return

        if is_process_running(TARGET_PROCESS_NAME):
            messagebox.showwarning(
                "zapret запущен",
                "winws.exe сейчас работает и держит свои файлы.\n\n"
                "Закрой окно \"zapret: ...\" (а если стоит служба — удали её через "
                "service.bat), затем повтори обновление.",
            )
            return

        zapret_dir = self.zapret_dir
        self._set_updating(True)
        self.log(f"Обновляю zapret {local} → {latest}")

        def on_ok(version):
            self._set_updating(False)
            self.log(f"Готово: zapret обновлён до {version}.")
            self.install_own_strategies()
            self.refresh_strategies()
            self.refresh_version_info()
            self.poll_status_once()
            messagebox.showinfo("Готово", f"zapret обновлён до версии {version}.")

        def on_err(e):
            self._set_updating(False)
            self.refresh_version_info()
            self.poll_status_once()
            self.log(f"Ошибка обновления: {e}")
            messagebox.showerror("Обновление не удалось", str(e))

        self._run_in_background(lambda emit: update_zapret(zapret_dir, latest, emit), on_ok, on_err)

    # ------------------------------------------------------------- status

    def update_hint(self):
        if not self.zapret_dir:
            self.hint_card.configure(fg_color=COLOR_WARN_BG, border_color=COLOR_WARN_BORDER)
            self.hint_icon.configure(text="📂")
            self.hint_text.configure(
                text="Выбери папку с zapret, чтобы начать работу.",
                text_color=COLOR_WARN_TEXT,
            )
        elif getattr(self, "_running", False):
            self.hint_card.configure(fg_color=COLOR_WARN_BG, border_color=COLOR_WARN_BORDER)
            self.hint_icon.configure(text="⚠️")
            self.hint_text.configure(
                text="Чтобы остановить стратегию — просто закрой окно \"zapret: ...\", "
                     "которое открылось после запуска.",
                text_color=COLOR_WARN_TEXT,
            )
        else:
            self.hint_card.configure(fg_color=COLOR_READY_BG, border_color=COLOR_READY_BORDER)
            self.hint_icon.configure(text="✅")
            self.hint_text.configure(
                text="Утилита готова к работе.",
                text_color=COLOR_READY_TEXT,
            )

    def poll_status_once(self):
        running = is_process_running(TARGET_PROCESS_NAME)
        was_running = getattr(self, "_running", None)
        self._running = running
        if running:
            self.status_label.configure(text="запущено", text_color=COLOR_ACCENT)
        else:
            self.status_dot.configure(text_color=COLOR_MUTED)
            self.status_label.configure(text="остановлено", text_color=COLOR_MUTED)
            self.top_accent.configure(fg_color=COLOR_CARD_BORDER)

        if self._updating:
            # пока файлы zapret переписываются, запускать стратегию нельзя
            self.start_btn.configure(state="disabled", text="Обновление…")
        elif running:
            # без явного текста кнопка навсегда застревала на "Запускается…"
            self.start_btn.configure(state="disabled", text="●  Работает")
        else:
            self.start_btn.configure(state="normal", text="▶  Запустить")

        if running != was_running:
            self.update_hint()

    def poll_status(self):
        self.poll_status_once()
        self.after(1000, self.poll_status)

    def animate_pulse(self):
        if getattr(self, "_running", False):
            t = (math.sin(self._pulse_phase) + 1) / 2
            c = _lerp_color(COLOR_ACCENT, COLOR_ACCENT_HOVER, t)
            self.status_dot.configure(text_color=c)
            self.top_accent.configure(fg_color=c)
            self._pulse_phase = (self._pulse_phase + 0.35) % (2 * math.pi)
        self.after(70, self.animate_pulse)

    def on_close(self):
        if self._updating:
            # поток обновления — daemon: закрытие окна оборвало бы копирование на полпути
            messagebox.showwarning("Идёт обновление", "Дождись окончания обновления zapret, потом закрывай.")
            return
        self.destroy()


def main():
    if not IS_WINDOWS:
        print(
            "Zapret GUI работает только на Windows: он запускает .bat-стратегии "
            "zapret и хранит настройки в реестре.",
            file=sys.stderr,
        )
        return 1

    app = ZapretGUI()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
