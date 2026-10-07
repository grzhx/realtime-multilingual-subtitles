param()
$ErrorActionPreference = 'Stop'
& (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'initialize.ps1')
