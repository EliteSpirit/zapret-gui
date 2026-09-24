"""
Проверка остановки zapret перед обновлением на Windows-раннере: запускаем
подставной winws.exe (копию ping.exe) и ждём, что stop_zapret его завершит.
На раннерах GitHub UAC выключен, поэтому runas проходит без окна.

  python tests/stop_test.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    fake = tmp / gui.TARGET_PROCESS_NAME
    shutil.copy(r"C:\Windows\System32\PING.EXE", fake)
    proc = subprocess.Popen([str(fake), "-n", "120", "127.0.0.1"], stdout=subprocess.DEVNULL)
    try:
        assert gui.is_process_running(gui.TARGET_PROCESS_NAME), "подставной winws.exe не запустился"
        was = gui.stop_zapret(lambda kind, text: print(f"  [{kind}] {text}"))
        assert was["winws"], "stop_zapret не заметил запущенный winws.exe"
        assert not gui.is_process_running(gui.TARGET_PROCESS_NAME), "winws.exe всё ещё жив"
        # второй вызов, когда всё уже остановлено, ничего не делает и UAC не спрашивает
        assert gui.stop_zapret(lambda *a: None) == {"service": False, "winws": False}
    except AssertionError as e:
        print(f"FAIL: {e}")
        return 1
    finally:
        proc.kill()
    print("ok: stop_zapret остановил winws.exe")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
