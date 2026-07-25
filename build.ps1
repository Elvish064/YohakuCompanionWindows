$ErrorActionPreference = "Stop"

if ($env:CONDA_DEFAULT_ENV -ne "yohaku-companion-win") {
    Write-Warning "建议先激活 conda 环境：conda activate yohaku-companion-win"
}

$env:PYINSTALLER_CONFIG_DIR = Join-Path $env:TEMP "YohakuCompanion-PyInstaller"
python -m PyInstaller --noconfirm --clean YohakuCompanion.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$selfTest = Start-Process `
    -FilePath ".\dist\YohakuCompanion\YohakuCompanion.exe" `
    -ArgumentList "--self-test" `
    -Wait `
    -PassThru `
    -WindowStyle Hidden
if ($selfTest.ExitCode -ne 0) { exit $selfTest.ExitCode }

Write-Host "构建及自检完成：dist\YohakuCompanion\YohakuCompanion.exe"
