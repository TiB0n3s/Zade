param(
    [string]$Name = "Zade",
    [string]$Location = "Desktop"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Target = Join-Path $Root "Start-Zade.cmd"
if (-not (Test-Path -LiteralPath $Target -PathType Leaf)) {
    throw "Missing launcher: $Target"
}

$Shell = New-Object -ComObject WScript.Shell
$Folder = switch ($Location.ToLowerInvariant()) {
    "startmenu" { [Environment]::GetFolderPath("StartMenu") }
    default { [Environment]::GetFolderPath("Desktop") }
}
$ShortcutPath = Join-Path $Folder "$Name.lnk"
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Target
$Shortcut.WorkingDirectory = $Root
$Shortcut.IconLocation = "$env:SystemRoot\System32\SHELL32.dll,220"
$Shortcut.Description = "Start the local Zade AI co-founder kernel and UI."
$Shortcut.Save()

Write-Output "Shortcut installed: $ShortcutPath"
