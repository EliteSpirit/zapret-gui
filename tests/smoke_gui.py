"""
Смоук-тест: окно создаётся, стартовый экран и анимации отрабатывают пару
секунд без исключений, окно закрывается. Только для Windows (реестр, UAC),
поэтому гоняется в Windows-джобе CI и перед сборкой релиза.

  python tests/smoke_gui.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui  # noqa: E402

RUN_MS = 3000


def main() -> int:
    errors = []
    app = gui.ZapretGUI()
    # исключения в after-колбэках tkinter по умолчанию только печатает, а нам нужно их поймать
    app.report_callback_exception = lambda exc, val, tb: errors.append(f"{exc.__name__}: {val}")
    app.after(RUN_MS, app.destroy)
    app.mainloop()
    if errors:
        print("FAIL:", *errors, sep="\n  ")
        return 1
    print(f"ok: окно отработало {RUN_MS} мс без ошибок")
    return 0


if __name__ == "__main__":
    # консоль Windows-раннера в cp1252 и падает на кириллице в print
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
