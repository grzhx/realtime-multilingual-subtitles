param(
    [string]$PythonHome,
    [Parameter(Mandatory=$true)][string]$IsccPath
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot
if (-not $PythonHome) {
    $PythonHome = & py -3.14 -c 'import sys; print(sys.base_prefix)'
}
$python = Join-Path $PythonHome 'python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Python 3.14 x64 required for release builds' }
$env:TEMP = Join-Path $ProjectRoot 'cache\tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
& $python (Join-Path $ProjectRoot 'packaging\stage.py') --python-home $PythonHome
if ($LASTEXITCODE -ne 0) { throw 'Release staging failed' }
& $IsccPath (Join-Path $ProjectRoot 'packaging\installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed' }
Get-ChildItem (Join-Path $ProjectRoot 'dist') -File | ForEach-Object {
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower()
    Write-Host ($_.Name + '  SHA-256: ' + $hash)
}
