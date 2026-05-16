@echo off
setlocal enabledelayedexpansion

:: VNT Build Script
:: Usage:
::   make.bat          - Build all executables without deploying
::   make.bat -i       - Build all executables and deploy via SCP
::   make.bat -u       - Skip compilation, update version and deploy via SCP
::
:: Automatically discovers UPX and 7-Zip installations

:: Check for flags first
set "DO_INSTALL=false"
set "SKIP_COMPILE=false"
for %%a in (%*) do (
    if "%%a"=="-i" set "DO_INSTALL=true"
    if "%%a"=="-u" set "SKIP_COMPILE=true"
)

:: Find UPX directory
set "UPX_DIR="

:: First, try to find UPX in PATH
for /f "usebackq tokens=*" %%i in (`where upx 2^>nul`) do (
    set "UPX_PATH=%%i"
    for %%j in ("!UPX_PATH!") do set "UPX_DIR=%%~dpj"
    echo Found UPX in PATH at !UPX_DIR!
    goto :found_upx
)

:: Search common UPX installation directories
for /d %%d in ("C:\Program Files\upx-*") do (
    if exist "%%d\upx.exe" (
        set "UPX_DIR=%%d"
        echo Found UPX at !UPX_DIR!
        goto :found_upx
    )
)

if exist "D:\" (
    for /d %%d in ("D:\Program Files\upx-*") do (
        if exist "%%d\upx.exe" (
            set "UPX_DIR=%%d"
            echo Found UPX at !UPX_DIR!
            goto :found_upx
        )
    )
)

for /d %%d in ("C:\upx-*") do (
    if exist "%%d\upx.exe" (
        set "UPX_DIR=%%d"
        echo Found UPX at !UPX_DIR!
        goto :found_upx
    )
)

if exist "D:\" (
    for /d %%d in ("D:\upx-*") do (
        if exist "%%d\upx.exe" (
            set "UPX_DIR=%%d"
            echo Found UPX at !UPX_DIR!
            goto :found_upx
        )
    )
)

for /d %%d in ("%USERPROFILE%\upx-*") do (
    if exist "%%d\upx.exe" (
        set "UPX_DIR=%%d"
        echo Found UPX at !UPX_DIR!
        goto :found_upx
    )
)

:: If still not found, check current directory
if exist ".\upx.exe" (
    set "UPX_DIR=."
    echo Found UPX in current directory
    goto :found_upx
)

:found_upx
if not defined UPX_DIR (
    echo Warning: UPX not found, builds will proceed without compression
    set "UPX_PARAM="
) else (
    echo Using UPX directory: !UPX_DIR!
    set "UPX_PARAM=--upx-dir=!UPX_DIR!"
)

:: Find 7-Zip executable
set "SEVENZIP_PATH="

:: First, try to find 7-Zip in PATH
for /f "usebackq tokens=*" %%i in (`where 7z 2^>nul ^|^| echo notfound`) do (
    if not "%%i"=="notfound" (
        set "SEVENZIP_PATH=%%i"
        echo Found 7-Zip in PATH at !SEVENZIP_PATH!
        goto :found_sevenzip
    )
)

:: Search common 7-Zip installation directories
set "temp_path=C:\Program Files\7-Zip\7z.exe"
if exist "!temp_path!" (
    set "SEVENZIP_PATH=!temp_path!"
    echo Found 7-Zip at !SEVENZIP_PATH!
    goto :found_sevenzip
)

if exist "D:\" (
    set "temp_path=D:\Program Files\7-Zip\7z.exe"
    if exist "!temp_path!" (
        set "SEVENZIP_PATH=!temp_path!"
        echo Found 7-Zip at !SEVENZIP_PATH!
        goto :found_sevenzip
    )
)

set "temp_path=C:\Program Files (x86)\7-Zip\7z.exe"
if exist "!temp_path!" (
    set "SEVENZIP_PATH=!temp_path!"
    echo Found 7-Zip at !SEVENZIP_PATH!
    goto :found_sevenzip
)

if exist "D:\" (
    set "temp_path=D:\Program Files (x86)\7-Zip\7z.exe"
    if exist "!temp_path!" (
        set "SEVENZIP_PATH=!temp_path!"
        echo Found 7-Zip at !SEVENZIP_PATH!
        goto :found_sevenzip
    )
)

set "temp_path="

:found_sevenzip
if not defined SEVENZIP_PATH (
    echo Error: 7-Zip not found. Please install 7-Zip or add it to PATH.
    exit /b 1
)

echo.
echo Building with parameters:
echo UPX Parameter: !UPX_PARAM!
echo 7-Zip Path: !SEVENZIP_PATH!
echo.


:: If skip compile flag is set, perform update workflow
if "!SKIP_COMPILE!"=="true" (
    echo Skipping compilation step...

    :: Check if vnt_helper.py exists (needed for version extraction)
    if not exist .\vnt_helper.py (
        echo Error: .\vnt_helper.py not found. Cannot proceed with update.
        exit /b 1
    )

    :: Check if vnt_helper.zip exists in dist
    if not exist .\dist\vnt_helper.zip (
        echo Error: .\dist\vnt_helper.zip not found. Cannot proceed with update.
        exit /b 1
    )
    echo Found .\dist\vnt_helper.zip

    :: Run update_version.yaml.py to update version.yaml
    echo Updating version.yaml...
    python .\update_version.yaml.py

    :: Copy version file to res directory
    copy .\dist\version.yaml .\res

    :: Set DO_INSTALL to true to proceed with deployment
    set "DO_INSTALL=true"
) ELSE (
    :: Perform full compilation workflow when not skipping
    :: Build vnt_service
    echo Building vnt_service...
    if defined UPX_DIR (
        pyinstaller --upx-dir="!UPX_DIR!" --clean .\vnt_service.spec
    ) else (
        pyinstaller --clean .\vnt_service.spec
    )
    if errorlevel 1 (
        echo Error building vnt_service
        exit /b 1
    )
    if exist .\dist\vnt_service.exe (
        copy .\dist\vnt_service.exe .
        copy .\dist\vnt_service.exe .\res
    ) else (
        echo Error: vnt_service.exe not found after build
        exit /b 1
    )

    :: Build vnt_updater
    echo Building vnt_updater...
    if defined UPX_DIR (
        pyinstaller --upx-dir="!UPX_DIR!" --clean .\vnt_updater.spec
    ) else (
        pyinstaller --clean .\vnt_updater.spec
    )
    if errorlevel 1 (
        echo Error building vnt_updater
        exit /b 1
    )
    if exist .\dist\vnt_updater.exe (
        copy .\dist\vnt_updater.exe .\res
    ) else (
        echo Error: vnt_updater.exe not found after build
        exit /b 1
    )

    :: Build vnt_helper
    echo Building vnt_helper...
    if defined UPX_DIR (
        pyinstaller --upx-dir="!UPX_DIR!" --clean .\vnt_helper.spec
    ) else (
        pyinstaller --clean .\vnt_helper.spec
    )
    if errorlevel 1 (
        echo Error building vnt_helper
        exit /b 1
    )
    if exist .\dist\vnt_helper.exe (
        copy .\dist\vnt_helper.exe .\
    ) else (
        echo Error: vnt_helper.exe not found after build
        exit /b 1
    )

    :: Create ZIP archive using 7-Zip
    echo Creating ZIP archive...
    "!SEVENZIP_PATH!" a -tzip .\dist\vnt_helper.zip .\dist\vnt_helper.exe

    :: Update version file
    python .\update_version.yaml.py

    :: Copy version file to res directory
    copy .\dist\version.yaml .\res
)

if "!DO_INSTALL!"=="true" (
    echo Deploying files via SCP...
    scp ./dist/vnt_helper.zip pi@pi.russ:/var/www/files
    scp ./dist/vnt_helper.zip russ@ecserver.russ:/home/russ/updatefolder/001
    scp ./dist/version.yaml russ@ecserver.russ:/home/russ/updatefolder/001
    echo Deployment completed!
) else (
    echo Build completed successfully!
    echo To deploy files, run: make.bat -i
)