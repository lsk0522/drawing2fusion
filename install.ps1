# Drawing2Fusion add-in installer (Windows)
# Links addin\Drawing2Fusion into Fusion's AddIns folder with a directory junction,
# so edits in this repo take effect after Stop -> Run in Fusion (no copying).
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1            # install (junction)
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Copy      # install (plain copy)
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall

param([switch]$Copy, [switch]$Uninstall)
$ErrorActionPreference = "Stop"

$src = Join-Path $PSScriptRoot "addin\Drawing2Fusion"
$addins = Join-Path $env:APPDATA "Autodesk\Autodesk Fusion 360\API\AddIns"
$dst = Join-Path $addins "Drawing2Fusion"

if (Test-Path $dst) {
    $item = Get-Item $dst -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        cmd /c rmdir "$dst" | Out-Null          # removes the junction only, not the repo files
    } else {
        Remove-Item $dst -Recurse -Force
    }
    Write-Host "removed old $dst"
}
if ($Uninstall) { Write-Host "uninstalled."; exit 0 }

New-Item -ItemType Directory -Force -Path $addins | Out-Null
if ($Copy) {
    Copy-Item $src $dst -Recurse
    Write-Host "copied add-in to $dst"
} else {
    New-Item -ItemType Junction -Path $dst -Target $src | Out-Null
    Write-Host "linked $dst -> $src"
}
Write-Host ""
Write-Host "Next: Fusion -> Shift+S -> Add-Ins -> Drawing2Fusion -> Run"
Write-Host "Then: python -m d2f ping"
