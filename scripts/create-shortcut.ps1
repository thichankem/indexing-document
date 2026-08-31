# Creates the "docindex" and "docindex output" shortcuts on the Desktop.
# Run it with: right-click -> Run with PowerShell (or double-click create-shortcut.bat)

$ErrorActionPreference = 'Stop'
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir

# Prefer pythonw.exe so no console window sits behind the GUI.
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $python) { Write-Error "Python was not found on PATH."; exit 1 }

$desktop = [Environment]::GetFolderPath('Desktop')
if (-not (Test-Path $desktop)) { Write-Error "Desktop folder was not found."; exit 1 }

# Shortcut 1: open the docindex GUI
$linkPath = Join-Path $desktop 'docindex.lnk'
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($linkPath)
$sc.TargetPath       = $python
$sc.Arguments        = '-m docindex.gui'
$sc.WorkingDirectory = $projectDir
$sc.Description      = 'docindex - clean documents and chunk them for RAG'
$sc.WindowStyle      = 1

# Use the bundled icon when present, otherwise fall back to the Python icon.
$icon = Join-Path $projectDir 'assets\docindex.ico'
if (Test-Path $icon) { $sc.IconLocation = $icon } else { $sc.IconLocation = "$python,0" }
$sc.Save()

# Shortcut 2: open the output folder
$outputDir = Join-Path $projectDir 'output'
$outputLinkPath = Join-Path $desktop 'docindex output.lnk'
$sc2 = $shell.CreateShortcut($outputLinkPath)
$sc2.TargetPath  = $outputDir
$sc2.Description = 'docindex output folder'
$sc2.Save()

Write-Host ""
Write-Host "Created shortcuts:" -ForegroundColor Green
Write-Host "   $linkPath"
Write-Host "   $outputLinkPath"
Write-Host ""
Write-Host "- Double-click 'docindex' to open the GUI"
Write-Host "- Double-click 'docindex output' to browse the results"
Write-Host ""
