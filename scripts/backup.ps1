# Zip data/, persona.yaml, and config.yaml. Never includes .env.
param(
    [string]$Root = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path -LiteralPath $Root)) {
    throw "Backup root does not exist: $Root"
}
$Root = (Resolve-Path -LiteralPath $Root).Path
$backupDir = Join-Path $Root "backups"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$zipPath = Join-Path $backupDir "$stamp.zip"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

$include = @()
foreach ($name in @("data", "persona.yaml", "config.yaml")) {
    $path = Join-Path $Root $name
    if (Test-Path -LiteralPath $path) {
        $include += $path
    }
}
if ($include.Count -eq 0) {
    throw "Nothing to back up under $Root"
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($path in $include) {
        $leaf = Split-Path -Leaf $path
        if ((Get-Item -LiteralPath $path).PSIsContainer) {
            $files = Get-ChildItem -LiteralPath $path -Recurse -File
            foreach ($file in $files) {
                if ($file.Name -eq ".env") { continue }
                $relative = $file.FullName.Substring($path.Length).TrimStart("\", "/")
                $entry = ($leaf + "/" + $relative) -replace "\\", "/"
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file.FullName, $entry) | Out-Null
            }
        } else {
            if ($leaf -eq ".env") { continue }
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $path, $leaf) | Out-Null
        }
    }
} finally {
    $zip.Dispose()
}

$existing = @(Get-ChildItem -LiteralPath $backupDir -Filter "*.zip" -File | Sort-Object LastWriteTime -Descending)
if ($existing.Count -gt 10) {
    $existing | Select-Object -Skip 10 | Remove-Item -Force
}
