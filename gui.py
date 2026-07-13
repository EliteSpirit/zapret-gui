"""
Zapret GUI — простая графическая обёртка поверх уже установленного zapret.
Версия с современным дизайном на CustomTkinter.

ВАЖНО: этот файл НЕ содержит никакого кода для работы с сетевыми пакетами.
Он только запускает уже существующие .bat-файлы стратегий (general*.bat)
из папки zapret, которую ты сам укажешь при первом запуске. Всю реальную
работу с трафиком по-прежнему делает winws.exe из твоей установки zapret —
этот скрипт им не заменяет и не модифицирует.

Остановка стратегии делается вручную — закрытием окна "zapret: ...",
которое открывает сам .bat. Автоматическая остановка сознательно убрана:
у .bat-скриптов есть свой цикл перезапуска процесса, из-за которого
попытка "убить" процесс программно ни к чему не приводит — окно всё
равно нужно закрывать самому.

Установка зависимости (один раз):
  pip install customtkinter

Запуск:
  python gui.py

Сборка в .exe: см. build.bat рядом с этим файлом.

ВАЖНО про права администратора:
  winws.exe и WinDivert обычно требуют прав администратора для работы.
  Запуск здесь сделан через ShellExecute с verb="runas", поэтому Windows
  сама покажет диалог UAC при нажатии "Запустить".
"""

import ctypes
import json
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

# Когда собрано в .exe через PyInstaller, __file__ указывает на временную
# папку распаковки — конфиг вместо этого кладём рядом с самим .exe.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

CONFIG_FILE = APP_DIR / "config.json"
TARGET_PROCESS_NAME = "winws.exe"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# --- палитра ---
COLOR_BG = "#0b0d12"
COLOR_CARD = "#141821"
COLOR_CARD_BORDER = "#242a36"
COLOR_ACCENT = "#3fb950"
COLOR_ACCENT_HOVER = "#4fd463"
COLOR_TEXT = "#e6e8eb"
COLOR_MUTED = "#7d8590"
COLOR_LOG_BG = "#080a0d"
COLOR_LOG_TEXT = "#c9d1d9"
COLOR_WARN_BG = "#2b2411"
COLOR_WARN_BORDER = "#5a4a12"
COLOR_WARN_TEXT = "#e3b341"

CREATE_NO_WINDOW = 0x08000000
CORNER_RADIUS = 10


def _lerp_color(c1: str, c2: str, t: float) -> str:
    c1, c2 = c1.lstrip("#"), c2.lstrip("#")
    r1, g1, b1 = int(c1[0:2], 16), int(c1[2:4], 16), int(c1[4:6], 16)
    r2, g2, b2 = int(c2[0:2], 16), int(c2[2:4], 16), int(c2[4:6], 16)
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(config):
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


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


class ZapretGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Zapret GUI")
        self.geometry("700x560")
        self.minsize(580, 480)
        self.configure(fg_color=COLOR_BG)

        self.config_data = load_config()
        self.zapret_dir = None

        self.build_ui()
        self.load_or_ask_directory()
        self._pulse_phase = 0
        self.poll_status()
        self.animate_pulse()

    def build_ui(self):
        self.grid_columnconfigure(0, weight=1)

        # --- Header ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(24, 16))
        header.grid_columnconfigure(0, weight=1)

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_row, text="🛡️", font=ctk.CTkFont(size=26)
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

        # status badge, top-right
        self.status_badge = ctk.CTkFrame(
            header, corner_radius=999, fg_color="#1c2128", border_width=1, border_color=COLOR_CARD_BORDER
        )
        self.status_badge.grid(row=0, column=1, sticky="e")
        badge_inner = ctk.CTkFrame(self.status_badge, fg_color="transparent")
        badge_inner.pack(padx=14, pady=6)
        self.status_dot = ctk.CTkLabel(badge_inner, text="●", text_color=COLOR_MUTED, font=ctk.CTkFont(size=13))
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_label = ctk.CTkLabel(
            badge_inner, text="остановлено", text_color=COLOR_MUTED, font=ctk.CTkFont(size=12)
        )
        self.status_label.pack(side="left")

        # --- Directory card ---
        dir_card = self._card()
        dir_card.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 14))
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

        # --- Strategy card ---
        strategy_card = self._card()
        strategy_card.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 14))
        strategy_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            strategy_card, text="СТРАТЕГИЯ", font=ctk.CTkFont(size=10, weight="bold"), text_color=COLOR_MUTED
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 4))

        self.strategy_var = tk.StringVar(value="")
        self.strategy_menu = ctk.CTkOptionMenu(
            strategy_card, variable=self.strategy_var, values=[""],
            corner_radius=CORNER_RADIUS, fg_color="#21262d", button_color="#30363d",
            button_hover_color="#3a4048", dropdown_fg_color="#1c2128",
        )
        self.strategy_menu.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))

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

        # --- Stop hint card ---
        hint_card = ctk.CTkFrame(
            self, corner_radius=CORNER_RADIUS, fg_color=COLOR_WARN_BG,
            border_width=1, border_color=COLOR_WARN_BORDER
        )
        hint_card.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 14))
        hint_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(hint_card, text="⚠️", font=ctk.CTkFont(size=16)).grid(
            row=0, column=0, padx=(14, 8), pady=12
        )
        ctk.CTkLabel(
            hint_card,
            text="Чтобы остановить стратегию — просто закрой окно \"zapret: ...\", "
                 "которое открылось после запуска.",
            font=ctk.CTkFont(size=12), text_color=COLOR_WARN_TEXT,
            anchor="w", justify="left", wraplength=520
        ).grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=12)

        # --- Log card ---
        log_card = self._card()
        log_card.grid(row=4, column=0, sticky="nsew", padx=24, pady=(0, 24))
        self.grid_rowconfigure(4, weight=1)
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            log_card, text="ЛОГ", font=ctk.CTkFont(size=10, weight="bold"), text_color=COLOR_MUTED
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 6))

        self.log_area = ctk.CTkTextbox(
            log_card, fg_color=COLOR_LOG_BG, text_color=COLOR_LOG_TEXT,
            font=ctk.CTkFont(family="Consolas", size=12), corner_radius=CORNER_RADIUS,
            border_width=1, border_color=COLOR_CARD_BORDER
        )
        self.log_area.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.log_area.configure(state="disabled")

    def _card(self):
        return ctk.CTkFrame(
            self, corner_radius=CORNER_RADIUS, fg_color=COLOR_CARD,
            border_width=1, border_color=COLOR_CARD_BORDER
        )

    def log(self, text):
        self.log_area.configure(state="normal")
        self.log_area.insert("end", text + "\n")
        self.log_area.see("end")
        self.log_area.configure(state="disabled")

    def load_or_ask_directory(self):
        saved = self.config_data.get("zapret_dir")
        if saved and Path(saved).exists():
            self.set_directory(Path(saved))
        else:
            self.choose_directory()

    def choose_directory(self):
        chosen = filedialog.askdirectory(title="Выбери папку с zapret (где general*.bat)")
        if chosen:
            self.set_directory(Path(chosen))

    def set_directory(self, path: Path):
        self.zapret_dir = path
        self.dir_label.configure(text=str(path))
        self.config_data["zapret_dir"] = str(path)
        save_config(self.config_data)
        self.refresh_strategies()

    def refresh_strategies(self):
        if not self.zapret_dir:
            return
        bat_files = sorted(self.zapret_dir.glob("general*.bat"))
        names = [f.name for f in bat_files]

        if names:
            self.strategy_menu.configure(values=names)
            self.strategy_var.set(names[0])
            self.log(f"Найдено стратегий: {len(names)}")
        else:
            self.strategy_menu.configure(values=[""])
            self.strategy_var.set("")
            self.log("В выбранной папке не найдено файлов general*.bat")

    def start_strategy(self):
        if is_process_running(TARGET_PROCESS_NAME):
            messagebox.showinfo(
                "Уже запущено",
                "winws.exe уже работает.\n\nЧтобы запустить другую стратегию — сначала закрой окно \"zapret: ...\"."
            )
            return

        strategy = self.strategy_var.get()
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

    def poll_status(self):
        running = is_process_running(TARGET_PROCESS_NAME)
        self._running = running
        if running:
            self.status_label.configure(text="запущено", text_color=COLOR_ACCENT)
            self.start_btn.configure(state="disabled")
        else:
            self.status_dot.configure(text_color=COLOR_MUTED)
            self.status_label.configure(text="остановлено", text_color=COLOR_MUTED)
            self.start_btn.configure(state="normal", text="▶  Запустить")

        self.after(1000, self.poll_status)

    def animate_pulse(self):
        import math
        if getattr(self, "_running", False):
            t = (math.sin(self._pulse_phase) + 1) / 2
            self.status_dot.configure(text_color=_lerp_color(COLOR_ACCENT, COLOR_ACCENT_HOVER, t))
            self._pulse_phase += 0.35
        self.after(70, self.animate_pulse)

    def on_close(self):
        self.destroy()


def main():
    app = ZapretGUI()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
