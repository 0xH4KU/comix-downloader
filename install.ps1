# ============================================================================
# comix-downloader — Windows PowerShell install script
#
# Usage (PowerShell):
#   irm https://raw.githubusercontent.com/0xH4KU/comix-downloader/main/install.ps1 | iex
#
# Options:
#   .\install.ps1 -Uninstall    Remove comix-dl completely
#   .\install.ps1 -y            Non-interactive mode
#
# After install:  comix-dl
# Uninstall:      comix-dl-uninstall  (or: install.ps1 -Uninstall)
# ============================================================================

param(
    [switch]$Uninstall,
    [switch]$y,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

# -- Config -------------------------------------------------------------------
$REPO = "https://github.com/0xH4KU/comix-downloader.git"
$INSTALL_DIR = if ($env:COMIX_INSTALL_DIR) { $env:COMIX_INSTALL_DIR } else { "$env:LOCALAPPDATA\comix-dl" }
$BIN_DIR = if ($env:COMIX_BIN_DIR) { $env:COMIX_BIN_DIR } else { "$env:LOCALAPPDATA\comix-dl\bin" }
$VENV_DIR = "$INSTALL_DIR\.venv"
$MIN_PYTHON = "3.11"
$TOTAL_STEPS = 5

function Write-Info  { param($msg) Write-Host "[INFO] " -ForegroundColor Cyan -NoNewline; Write-Host $msg }
function Write-Ok    { param($msg) Write-Host "[ OK ] " -ForegroundColor Green -NoNewline; Write-Host $msg }
function Write-Warn  { param($msg) Write-Host "[WARN] " -ForegroundColor Yellow -NoNewline; Write-Host $msg }
function Write-Err   { param($msg) Write-Host "[ERROR] " -ForegroundColor Red -NoNewline; Write-Host $msg; exit 1 }
function Write-Step  { param($n, $msg) Write-Host "`n[$n/$TOTAL_STEPS] " -ForegroundColor Cyan -NoNewline; Write-Host $msg -ForegroundColor White }

# -- Help ---------------------------------------------------------------------
if ($Help) {
    Write-Host "Usage: install.ps1 [-Uninstall] [-y] [-Help]"
    Write-Host ""
    Write-Host "  -Uninstall    Remove comix-dl completely"
    Write-Host "  -y            Non-interactive mode (skip confirmations)"
    Write-Host "  -Help         Show this help"
    exit 0
}

# -- Uninstall ----------------------------------------------------------------
if ($Uninstall) {
    Write-Host "Uninstalling comix-dl..." -ForegroundColor White
    if (Test-Path $INSTALL_DIR) { Remove-Item -Recurse -Force $INSTALL_DIR }
    if (Test-Path "$BIN_DIR\comix-dl.cmd") { Remove-Item -Force "$BIN_DIR\comix-dl.cmd" }
    if (Test-Path "$BIN_DIR\comix-dl-uninstall.cmd") { Remove-Item -Force "$BIN_DIR\comix-dl-uninstall.cmd" }
    Write-Host "Done. Config at $env:APPDATA\comix-dl\ was preserved." -ForegroundColor Green
    Write-Host "To remove config too: Remove-Item -Recurse $env:APPDATA\comix-dl\"
    exit 0
}

# -- Banner -------------------------------------------------------------------
Write-Host ""
Write-Host "  ██████╗ ██████╗ ███╗   ███╗██╗██╗  ██╗" -ForegroundColor Cyan
Write-Host " ██╔════╝██╔═══██╗████╗ ████║██║╚██╗██╔╝" -ForegroundColor Cyan
Write-Host " ██║     ██║   ██║██╔████╔██║██║ ╚███╔╝ " -ForegroundColor Cyan
Write-Host " ██║     ██║   ██║██║╚██╔╝██║██║ ██╔██╗ " -ForegroundColor Cyan
Write-Host " ╚██████╗╚██████╔╝██║ ╚═╝ ██║██║██╔╝ ██╗" -ForegroundColor Cyan
Write-Host "  ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝╚═╝  ╚═╝" -ForegroundColor Cyan
Write-Host ""
Write-Host "One-click installer (Windows)" -ForegroundColor White
Write-Host ""

# -- Pre-flight ---------------------------------------------------------------

# Check git
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Err "git is required. Install: https://git-scm.com/download/win"
}

# Find Python >= 3.11
$pythonCmd = $null
foreach ($cmd in @("python3", "python", "py")) {
    try {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver) {
            $parts = $ver.Split(".")
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 11) {
                $pythonCmd = $cmd
                break
            }
        }
    } catch { continue }
}

if (-not $pythonCmd) {
    Write-Err "Python >= $MIN_PYTHON is required. Install: https://www.python.org/downloads/"
}

$pythonVer = & $pythonCmd --version 2>&1
Write-Ok "Python: $pythonVer ($pythonCmd)"

# Check Chrome
$chromePath = $null
$chromeLocations = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
foreach ($loc in $chromeLocations) {
    if (Test-Path $loc) { $chromePath = $loc; break }
}
if (-not $chromePath) {
    Write-Warn "Google Chrome not found."
    Write-Warn "comix-dl requires Chrome for Cloudflare bypass."
    Write-Warn "Install: https://www.google.com/chrome/"
    if (-not $y) {
        $ans = Read-Host "Continue without Chrome? [y/N]"
        if ($ans -notmatch '^[Yy]$') { exit 1 }
    }
    $chromePath = "(not found)"
}
Write-Ok "Chrome: $chromePath"

# -- Install ------------------------------------------------------------------

# Step 1: Clone/update
Write-Step 1 "Fetching source code..."
if (Test-Path "$INSTALL_DIR\.git") {
    Write-Info "Existing installation found, updating..."
    try {
        git -C $INSTALL_DIR pull --ff-only 2>$null
    } catch {
        Write-Warn "git pull failed, re-cloning..."
        Remove-Item -Recurse -Force $INSTALL_DIR
        git clone --depth 1 $REPO $INSTALL_DIR
    }
} else {
    if (Test-Path $INSTALL_DIR) { Remove-Item -Recurse -Force $INSTALL_DIR }
    git clone --depth 1 $REPO $INSTALL_DIR
}
Write-Ok "Source code ready"

# Step 2: Virtual environment
Write-Step 2 "Creating virtual environment..."
& $pythonCmd -m venv $VENV_DIR --clear
Write-Ok "Virtual environment created"

# Step 3: Dependencies
Write-Step 3 "Installing dependencies..."
& "$VENV_DIR\Scripts\pip.exe" install --upgrade pip setuptools wheel -q 2>$null
& "$VENV_DIR\Scripts\pip.exe" install -e $INSTALL_DIR -q 2>$null
Write-Ok "Dependencies installed"

# Step 4: Playwright
Write-Step 4 "Installing Playwright Chromium..."
& "$VENV_DIR\Scripts\playwright.exe" install chromium 2>$null
Write-Ok "Playwright Chromium installed"

# Step 5: Create commands
Write-Step 5 "Creating global commands..."
New-Item -ItemType Directory -Force -Path $BIN_DIR | Out-Null

# comix-dl.cmd wrapper
@"
@echo off
"$VENV_DIR\Scripts\python.exe" -m comix_dl %*
"@ | Set-Content "$BIN_DIR\comix-dl.cmd" -Encoding ASCII

# Uninstall script
@"
@echo off
echo Uninstalling comix-dl...
rmdir /s /q "$INSTALL_DIR"
del "$BIN_DIR\comix-dl.cmd"
del "$BIN_DIR\comix-dl-uninstall.cmd"
echo Done. Config at %APPDATA%\comix-dl\ was preserved.
"@ | Set-Content "$BIN_DIR\comix-dl-uninstall.cmd" -Encoding ASCII

Write-Ok "Created: $BIN_DIR\comix-dl.cmd"

# -- Ensure PATH --------------------------------------------------------------
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$BIN_DIR*") {
    Write-Warn "$BIN_DIR is not in your PATH."
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$BIN_DIR", "User")
    $env:Path = "$env:Path;$BIN_DIR"
    Write-Ok "Added $BIN_DIR to user PATH"
    Write-Warn "You may need to restart your terminal for PATH changes to take effect."
}

# -- Verify -------------------------------------------------------------------
try {
    $ver = & "$BIN_DIR\comix-dl.cmd" --version 2>&1
    Write-Ok "Verified: $ver"
} catch {
    Write-Warn "Verification failed — comix-dl may require a terminal restart."
}

# -- Done! --------------------------------------------------------------------
Write-Host ""
Write-Host "Installation complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  Usage:" -ForegroundColor White
Write-Host "    comix-dl                   # Interactive menu" -ForegroundColor Cyan
Write-Host "    comix-dl `"manga name`"      # Quick search" -ForegroundColor Cyan
Write-Host "    comix-dl download URL      # Non-interactive download" -ForegroundColor Cyan
Write-Host "    comix-dl info URL          # Show manga info" -ForegroundColor Cyan
Write-Host "    comix-dl list              # List downloaded manga" -ForegroundColor Cyan
Write-Host "    comix-dl history           # Download history" -ForegroundColor Cyan
Write-Host "    comix-dl clean             # Clean up raw images" -ForegroundColor Cyan
Write-Host "    comix-dl doctor            # Check environment" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Paths:" -ForegroundColor White
Write-Host "    Install:  $INSTALL_DIR"
Write-Host "    Command:  $BIN_DIR\comix-dl.cmd"
Write-Host "    Config:   $env:APPDATA\comix-dl\"
Write-Host ""
Write-Host "  Update:    " -NoNewline; Write-Host "Re-run this script"
Write-Host "  Uninstall: " -NoNewline; Write-Host "comix-dl-uninstall"
Write-Host ""
