param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$env:PIP_CACHE_DIR = Join-Path $ProjectRoot 'cache\pip'
$env:TEMP = Join-Path $ProjectRoot 'cache\tmp'
$env:TMP = $env:TEMP
$env:HF_HOME = Join-Path $ProjectRoot 'cache\huggingface'
$env:HF_HUB_CACHE = Join-Path $env:HF_HOME 'hub'
$env:HF_XET_CACHE = Join-Path $env:HF_HOME 'xet'
New-Item -ItemType Directory -Force -Path $env:PIP_CACHE_DIR,$env:TEMP,$env:HF_HOME,(Join-Path $ProjectRoot 'logs') | Out-Null
Start-Transcript -Path (Join-Path $ProjectRoot 'logs\bootstrap.log') -Append | Out-Null
try {
    $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $bundled = Join-Path $ProjectRoot 'runtime\python\python.exe'
        if (Test-Path -LiteralPath $bundled) {
            & $bundled -m venv (Join-Path $ProjectRoot '.venv')
        } else {
            & py -3 -m venv (Join-Path $ProjectRoot '.venv')
        }
        if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed' }
    }
    & $python -c 'import sys, tkinter; assert sys.version_info >= (3, 11), "Python 3.11 or newer required"'
    if ($LASTEXITCODE -ne 0) { throw 'Python with Tkinter is required' }
    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'pip installation failed' }
    & $python -m pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
    if ($LASTEXITCODE -ne 0) { throw 'CUDA PyTorch installation failed' }
    & $python -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & $python (Join-Path $ProjectRoot 'download_models.py')
    if ($LASTEXITCODE -ne 0) { throw 'Model download or checksum verification failed' }
    & (Join-Path $ProjectRoot 'setup_gguf.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'llama.cpp installation failed' }
    & $python -c 'import torch, faster_whisper, soundcard, tkinter; assert torch.cuda.is_available(), "CUDA is unavailable; install a compatible NVIDIA driver"'
    if ($LASTEXITCODE -ne 0) { throw 'CUDA/dependency validation failed' }
    $marker = @{ version = '0.2.0'; initialized = (Get-Date).ToString('o') }
    $marker | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ProjectRoot 'cache\initialized.json') -Encoding UTF8
    Write-Host 'Initialization completed. All files are stored inside this directory.'
} finally {
    Stop-Transcript | Out-Null
}
