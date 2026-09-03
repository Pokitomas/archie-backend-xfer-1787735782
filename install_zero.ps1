param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Apply) {
    Write-Host 'ARCHIE Zero installer is dry-run by default.'
    Write-Host 'It will run legacy_cleanup.ps1 -Apply, self-test computer.py, then install the tested local kernel.'
    Write-Host 'Re-run with -Apply to mutate this machine.'
    & (Join-Path $here 'legacy_cleanup.ps1')
    exit 0
}

$python = (Get-Command python.exe -ErrorAction SilentlyContinue)
if (-not $python) { $python = (Get-Command py.exe -ErrorAction SilentlyContinue) }
if (-not $python) { throw 'Python is required for the current ARCHIE Zero kernel.' }
$pythonExe = $python.Source

# Prove the replacement before removing the observed legacy backend.
& $pythonExe (Join-Path $here 'computer.py') self-test
if ($LASTEXITCODE -ne 0) { throw 'computer.py self-test failed; legacy backend left untouched.' }

& (Join-Path $here 'legacy_cleanup.ps1') -Apply

$dest = Join-Path $env:LOCALAPPDATA 'ARCHIE-Zero'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -Force (Join-Path $here 'computer.py') (Join-Path $dest 'computer.py')
Copy-Item -Force (Join-Path $here 'resident.py') (Join-Path $dest 'resident.py')
Copy-Item -Force (Join-Path $here 'PROTOCOL.md') (Join-Path $dest 'PROTOCOL.md')

& $pythonExe (Join-Path $dest 'computer.py') self-test
if ($LASTEXITCODE -ne 0) { throw 'installed kernel self-test failed.' }

$receipt = [ordered]@{
    schema = 'archie-zero-install/v1'
    installed_at = [DateTimeOffset]::Now.ToString('o')
    root = $dest
    old_backend_cleanup = 'applied'
    computer_self_test = 'pass'
    autostart = $false
    cloud_command_mesh = $false
}
$receipt | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $dest 'INSTALL_RECEIPT.json')
Write-Host "ARCHIE Zero installed at $dest"
Write-Host 'No hidden startup task, Run key, tunnel, relay, or arbitrary remote command worker was installed.'
