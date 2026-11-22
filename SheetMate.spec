# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('icon.png', '.'), ('icon.ico', '.'), ('templates', 'templates')]
binaries = []
hiddenimports = ['openpyxl', 'PIL', 'typing_extensions', 'uiautomation',
                 'comtypes', 'pywinauto', 'pyautogui', 'pyscreeze',
                 'pymsgbox', 'pytweening', 'pygetwindow', 'pyperclip',
                 'mouseinfo', 'cv2', 'numpy', 'win32api', 'win32con',
                 'win32gui', 'win32process', 'neu_ui', 'sheetmate_ui', 'ios_ui', 'team_hub', 'mobile_bridge']
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
for pkg in ('uiautomation', 'comtypes', 'pywinauto', 'pyscreeze'):
    tmp_ret = collect_all(pkg)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['auto_report.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['comtypes.test', 'pywinauto.tests', 'tkinter.test'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SheetMate',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
    contents_directory='_internal',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='SheetMate_bundle',
)
