# -*- mode: python ; coding: utf-8 -*-
import sys
import os

block_cipher = None

# Директория проекта
project_dir = os.path.abspath(os.getcwd())

datas = [
    (os.path.join(project_dir, 'static'), 'static'),
]

# Добавляем .env или .env.example если существуют
if os.path.exists(os.path.join(project_dir, '.env.example')):
    datas.append((os.path.join(project_dir, '.env.example'), '.'))

hidden_imports = [
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'fastapi',
    'starlette',
    'starlette.middleware',
    'starlette.middleware.cors',
    'pydantic',
    'pydantic_core',
    'google',
    'google.genai',
    'requests',
    'dotenv',
    'sqlite3',
    'playwright',
    'playwright.sync_api',
    'webview',
    'webview.platforms.cocoa',
    'webview.platforms.edgechromium',
    'webview.platforms.winforms',
    'objc',
    'AppKit',
    'Foundation',
    'WebKit',
]

a = Analysis(
    ['desktop.py'],
    pathex=[project_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'numpy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HH_AI_Applier',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_dir, 'assets', 'icon.ico') if os.path.exists(os.path.join(project_dir, 'assets', 'icon.ico')) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='HH_AI_Applier',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='HH_AI_Applier.app',
        icon=os.path.join(project_dir, 'assets', 'icon.icns') if os.path.exists(os.path.join(project_dir, 'assets', 'icon.icns')) else None,
        bundle_identifier='com.hh.jobapplier',
        info_plist={
            'CFBundleName': 'HH AI Applier',
            'CFBundleDisplayName': 'HH.ru AI Job Applier',
            'CFBundleVersion': '1.0.0',
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': 'True',
            'NSRequiresAquaSystemAppearance': 'False',
        }
    )
