# Tao shortcut "docindex" va "mo ketqua" ngoai Desktop
# Chay: chuot phai -> Run with PowerShell (hoac nhay dup tao-shortcut.bat)

$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Uu tien pythonw.exe de khong hien cua so console den phia sau giao dien
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $python) { Write-Error "Khong tim thay Python trong PATH."; exit 1 }

$desktop = [Environment]::GetFolderPath('Desktop')
if (-not (Test-Path $desktop)) { Write-Error "Khong tim thay thu muc Desktop."; exit 1 }

# Shortcut 1: mo giao dien docindex
$linkPath = Join-Path $desktop 'docindex.lnk'
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($linkPath)
$sc.TargetPath       = $python
$sc.Arguments        = '-m docindex.gui'
$sc.WorkingDirectory = $projectDir
$sc.Description      = 'docindex - lam sach tai lieu va chia chunk cho RAG'
$sc.WindowStyle      = 1

# Dung icon rieng neu co, khong thi lay icon cua Python
$icon = Join-Path $projectDir 'docindex.ico'
if (Test-Path $icon) { $sc.IconLocation = $icon } else { $sc.IconLocation = "$python,0" }
$sc.Save()

# Shortcut 2: mo thu muc output/ket qua
$outputDir = Join-Path $projectDir 'output'
$outputLinkPath = Join-Path $desktop 'Ket qua.lnk'
$sc2 = $shell.CreateShortcut($outputLinkPath)
$sc2.TargetPath = $outputDir
$sc2.Description = 'Thư mục kết quả từ docindex'
$sc2.Save()

Write-Host ""
Write-Host "Da tao cac shortcut:" -ForegroundColor Green
Write-Host "   $linkPath"
Write-Host "   $outputLinkPath"
Write-Host ""
Write-Host "- Nhay dup vao 'docindex' de mo giao dien"
Write-Host "- Nhay dup vao 'Ket qua' de xem cac file ket qua"
Write-Host ""
