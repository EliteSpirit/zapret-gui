"""
Генерирует version-файл для PyInstaller (`--version-file`), чтобы у exe в
свойствах были ProductName, FileVersion и т.д. SignPath проверяет эти поля
при подписи.

  python tools/version_file.py v0.1.0 > version.txt
"""

import sys

version = sys.argv[1].lstrip("v")
parts = [int(p) for p in (version.split(".") + ["0", "0", "0"])[:4]]
nums = ", ".join(map(str, parts))

print(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({nums}), prodvers=({nums})),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'EliteSpirit'),
      StringStruct('FileDescription', 'Zapret GUI'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'ZapretGUI'),
      StringStruct('LegalCopyright', 'MIT License, EliteSpirit'),
      StringStruct('OriginalFilename', 'ZapretGUI.exe'),
      StringStruct('ProductName', 'Zapret GUI'),
      StringStruct('ProductVersion', '{version}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)""")
