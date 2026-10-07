param(
    [string]$Config = "config.toml"
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
# Keep every model and library cache inside the project directory.
$env:HF_HOME = Join-Path $ProjectRoot "cache\huggingface"
$env:HUGGINGFACE_HUB_CACHE = Join-Path $ProjectRoot "cache\huggingface\hub"
$env:HF_HUB_CACHE = $env:HUGGINGFACE_HUB_CACHE
$env:HF_XET_CACHE = Join-Path $ProjectRoot "cache\huggingface\xet"
$env:TRANSFORMERS_CACHE = Join-Path $ProjectRoot "cache\huggingface\transformers"
$env:TORCH_HOME = Join-Path $ProjectRoot "cache\torch"
$env:CUDA_CACHE_PATH = Join-Path $ProjectRoot "cache\cuda"
$env:XDG_CACHE_HOME = Join-Path $ProjectRoot "cache\xdg"
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$env:PIP_CACHE_DIR = Join-Path $ProjectRoot "cache\pip"
$env:TEMP = Join-Path $ProjectRoot "cache\tmp"
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:HF_HOME,$env:HUGGINGFACE_HUB_CACHE,$env:TRANSFORMERS_CACHE,$env:TORCH_HOME,$env:CUDA_CACHE_PATH,$env:XDG_CACHE_HOME,$env:PIP_CACHE_DIR,$env:TEMP | Out-Null
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "py" }
& $python -m realtime_subtitle.app --config $Config
exit $LASTEXITCODE
