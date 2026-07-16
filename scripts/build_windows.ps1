param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

& $Python -c "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ is required'"
& $Python -m pip install -e ".[build]"

$EntryPoint = Join-Path $Root "src\collection_manager\__main__.py"
$Spec = Join-Path $Root "src\collection_manager\pysidedeploy.spec"
$BuildDir = Join-Path $Root "dist\windows"
$ZipPath = Join-Path $Root "dist\CollectionManager-windows-portable.zip"
$IconPath = Join-Path $BuildDir "CollectionManager.ico"

New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
& $Python scripts/make_icon.py assets/collection-manager.svg $IconPath
if (Test-Path $Spec) { Remove-Item $Spec -Force }
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }

& pyside6-deploy $EntryPoint --init

$Content = Get-Content $Spec -Raw
$EscapedBuildDir = $BuildDir.Replace("\", "/")
$Content = $Content -replace "(?m)^title\s*=.*$", "title = CollectionManager"
$Content = $Content -replace "(?m)^exec_directory\s*=.*$", "exec_directory = $EscapedBuildDir"
$EscapedIconPath = $IconPath.Replace("\", "/")
$Content = $Content -replace "(?m)^icon\s*=.*$", "icon = $EscapedIconPath"
$Content = $Content -replace "(?m)^packages\s*=.*$", "packages = Nuitka==4.1.3"
$Content = $Content -replace "(?m)^mode\s*=\s*onefile\s*$", "mode = standalone"
$Utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($Spec, $Content, $Utf8WithoutBom)

& pyside6-deploy -c $Spec

$DeployDirectory = Get-ChildItem $BuildDir -Directory |
    Where-Object { $_.Name -like "*.dist" } |
    Select-Object -First 1
if (-not $DeployDirectory) {
    throw "pyside6-deploy completed without producing a standalone .dist directory."
}

# Alembic loads revision modules from disk at runtime. Keep the same relative layout as the
# source checkout so Database can upgrade existing portable libraries beside the executable.
Copy-Item (Join-Path $Root "alembic.ini") $DeployDirectory.FullName -Force
$DeployedMigrations = Join-Path $DeployDirectory.FullName "migrations"
if (Test-Path $DeployedMigrations) { Remove-Item $DeployedMigrations -Recurse -Force }
Copy-Item (Join-Path $Root "migrations") $DeployedMigrations -Recurse -Force
Get-ChildItem $DeployedMigrations -Directory -Recurse -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem $DeployedMigrations -File -Recurse -Filter "*.pyc" |
    Remove-Item -Force

$RequiredMigration = Join-Path $DeployedMigrations "versions\0002_collection_kinds.py"
if (-not (Test-Path (Join-Path $DeployDirectory.FullName "alembic.ini")) -or
    -not (Test-Path $RequiredMigration)) {
    throw "The portable build is missing required database migration assets."
}

Compress-Archive -Path (Join-Path $DeployDirectory.FullName "*") -DestinationPath $ZipPath
Remove-Item $Spec -Force
Write-Host "Portable build: $ZipPath"
