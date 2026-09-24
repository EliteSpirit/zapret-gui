@echo off
REM Сборка Zapret GUI в один .exe файл через PyInstaller.
REM Запусти этот файл (двойным кликом или из командной строки) в этой же папке.

echo Устанавливаю зависимости (если ещё не установлены)...
pip install --upgrade pyinstaller
pip install --upgrade -r requirements.txt

echo.
echo Собираю gui.exe...
python -m PyInstaller --noconfirm --onefile --windowed --name "ZapretGUI" --collect-all customtkinter --collect-all easing_functions --add-data "strategies;strategies" gui.py

echo.
echo Готово. Файл находится в папке dist\ZapretGUI.exe
pause
