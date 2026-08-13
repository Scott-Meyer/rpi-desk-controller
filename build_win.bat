@echo off
setlocal
cd /d "%~dp0"

echo === Building Standalone Windows Executable for Desk Agent ===
python -m pip install --upgrade pip
if errorlevel 1 exit /b %errorlevel%

python -m pip install -e ".[desktop,build]"
if errorlevel 1 exit /b %errorlevel%

if not exist "build" mkdir "build"
python -m piplicenses --with-license-file --with-notice-file --no-license-path --format=plain-vertical --ignore-packages desk-controller --output-file "build\THIRD_PARTY_LICENSES.txt"
if errorlevel 1 exit /b %errorlevel%

python -m PyInstaller --clean --noconfirm DeskAgent-Windows.spec
if errorlevel 1 exit /b %errorlevel%

if not exist "dist\DeskAgent-Windows.exe" (
    echo ERROR: dist\DeskAgent-Windows.exe was not created.
    exit /b 1
)

for %%F in ("dist\DeskAgent-Windows.exe") do if %%~zF EQU 0 (
    echo ERROR: dist\DeskAgent-Windows.exe is empty.
    exit /b 1
)

echo === Build Complete! Executable saved to dist/DeskAgent-Windows.exe ===
