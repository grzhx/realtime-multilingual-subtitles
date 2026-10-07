$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:HF_HOME = Join-Path $ProjectRoot "cache\huggingface"
$env:HF_HUB_CACHE = Join-Path $env:HF_HOME "hub"
$env:HF_XET_CACHE = Join-Path $env:HF_HOME "xet"
$env:TEMP = Join-Path $ProjectRoot "cache\tmp"
$env:TMP = $env:TEMP
$downloadDir = Join-Path $ProjectRoot "cache\downloads"
$runtimeDir = Join-Path $ProjectRoot "runtime\llama.cpp"
New-Item -ItemType Directory -Force -Path $downloadDir,$runtimeDir,$env:TEMP | Out-Null
$packages = @(
    @{ Name = "llama-b11461-bin-win-cuda-12.4-x64.zip"; Hash = "bce20c3417ffe3f4d81b837c3c0b0a77f409d43afe0012870b89ad6a0c79b021" },
    @{ Name = "cudart-llama-bin-win-cuda-12.4-x64.zip"; Hash = "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6" }
)
foreach ($package in $packages) {
    $zip = Join-Path $downloadDir $package.Name
    if (-not (Test-Path -LiteralPath $zip)) {
        Invoke-WebRequest -Uri ("https://github.com/ggml-org/llama.cpp/releases/download/b11461/" + $package.Name) -OutFile $zip
    }
    if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower() -ne $package.Hash) {
        throw "SHA-256 mismatch: $zip"
    }
    Expand-Archive -LiteralPath $zip -DestinationPath $runtimeDir -Force
}
Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/ggml-org/llama.cpp/b11461/LICENSE' -OutFile (Join-Path $runtimeDir 'LICENSE-llama.cpp.txt')
