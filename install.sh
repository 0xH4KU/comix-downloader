#!/usr/bin/env bash
# ============================================================================
# comix-downloader — One-click install script
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/0xH4KU/comix-downloader/main/install.sh | bash
#   # or
#   wget -qO- https://raw.githubusercontent.com/0xH4KU/comix-downloader/main/install.sh | bash
#
# After install:  comix-dl
# Uninstall:      comix-dl-uninstall
# ============================================================================

set -euo pipefail

# -- Colors -------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# -- Config -------------------------------------------------------------------
REPO="https://github.com/0xH4KU/comix-downloader.git"
INSTALL_DIR="${COMIX_INSTALL_DIR:-$HOME/.local/share/comix-dl}"
BIN_DIR="${COMIX_BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="$INSTALL_DIR/.venv"
MIN_PYTHON="3.11"

# -- Pre-flight checks -------------------------------------------------------

echo -e "\n${BOLD}${CYAN}"
echo '  ██████╗ ██████╗ ███╗   ███╗██╗██╗  ██╗'
echo ' ██╔════╝██╔═══██╗████╗ ████║██║╚██╗██╔╝'
echo ' ██║     ██║   ██║██╔████╔██║██║ ╚███╔╝ '
echo ' ██║     ██║   ██║██║╚██╔╝██║██║ ██╔██╗ '
echo ' ╚██████╗╚██████╔╝██║ ╚═╝ ██║██║██╔╝ ██╗'
echo '  ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚═╝╚═╝  ╚═╝'
echo -e "${NC}"
echo -e "${BOLD}One-click installer${NC}\n"

# Detect OS
OS="$(uname -s)"
case "$OS" in
    Linux)  info "Detected OS: Linux" ;;
    Darwin) info "Detected OS: macOS" ;;
    *)      error "Unsupported OS: $OS (only Linux and macOS are supported)" ;;
esac

# Require git
command -v git &>/dev/null || error "git is required. Please install it first."

# Find Python >= 3.11
find_python() {
    for cmd in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver="$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'  2>/dev/null)" || continue
            local major minor
            major="${ver%%.*}"
            minor="${ver##*.}"
            if [[ "$major" -ge 3 ]] && [[ "$minor" -ge 11 ]]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON_CMD=$(find_python) || error "Python >= $MIN_PYTHON is required but not found.\n  Install: https://www.python.org/downloads/"
PYTHON_VER="$($PYTHON_CMD --version 2>&1)"
success "Python: $PYTHON_VER ($PYTHON_CMD)"

# Find Chrome
find_chrome() {
    if [[ "$OS" == "Darwin" ]]; then
        local mac_chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        [[ -f "$mac_chrome" ]] && echo "$mac_chrome" && return 0
    fi
    for cmd in google-chrome google-chrome-stable chromium-browser chromium; do
        command -v "$cmd" &>/dev/null && echo "$cmd" && return 0
    done
    return 1
}

CHROME_PATH=$(find_chrome) || {
    warn "Google Chrome not found."
    warn "comix-dl requires Chrome for Cloudflare bypass."
    if [[ "$OS" == "Darwin" ]]; then
        warn "Install: brew install --cask google-chrome"
    else
        warn "Install: https://www.google.com/chrome/"
    fi
    echo ""
    read -rp "Continue without Chrome? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 1
    CHROME_PATH="(not found)"
}
success "Chrome: $CHROME_PATH"

# -- Install ------------------------------------------------------------------

info "Installing to: $INSTALL_DIR"

# Clone or update
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Existing installation found, updating…"
    git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || {
        warn "git pull failed, re-cloning…"
        rm -rf "$INSTALL_DIR"
        git clone --depth 1 "$REPO" "$INSTALL_DIR"
    }
else
    [[ -d "$INSTALL_DIR" ]] && rm -rf "$INSTALL_DIR"
    git clone --depth 1 "$REPO" "$INSTALL_DIR"
fi
success "Source code ready"

# Create venv
info "Creating virtual environment…"
"$PYTHON_CMD" -m venv "$VENV_DIR" --clear
source "$VENV_DIR/bin/activate"
success "Virtual environment created"

# Install dependencies
info "Installing dependencies…"
pip install --upgrade pip setuptools wheel -q
pip install -e "$INSTALL_DIR" -q
success "Dependencies installed"

# Install Playwright Chromium
info "Installing Playwright Chromium (this may take a moment)…"
playwright install chromium 2>/dev/null || {
    "$VENV_DIR/bin/python" -m playwright install chromium
}
success "Playwright Chromium installed"

deactivate

# -- Create global commands ---------------------------------------------------

mkdir -p "$BIN_DIR"

# comix-dl wrapper
cat > "$BIN_DIR/comix-dl" << 'WRAPPER'
#!/usr/bin/env bash
# Auto-generated by comix-dl installer
INSTALL_DIR="PLACEHOLDER_INSTALL_DIR"
exec "$INSTALL_DIR/.venv/bin/python" -m comix_dl "$@"
WRAPPER

# Replace placeholder with actual path
if [[ "$OS" == "Darwin" ]]; then
    sed -i '' "s|PLACEHOLDER_INSTALL_DIR|$INSTALL_DIR|g" "$BIN_DIR/comix-dl"
else
    sed -i "s|PLACEHOLDER_INSTALL_DIR|$INSTALL_DIR|g" "$BIN_DIR/comix-dl"
fi
chmod +x "$BIN_DIR/comix-dl"
success "Created: $BIN_DIR/comix-dl"

# Uninstall script
cat > "$BIN_DIR/comix-dl-uninstall" << UNINSTALL
#!/usr/bin/env bash
echo "Uninstalling comix-dl…"
rm -rf "$INSTALL_DIR"
rm -f "$BIN_DIR/comix-dl"
rm -f "$BIN_DIR/comix-dl-uninstall"
echo "Done. Config at ~/.config/comix-dl/ was preserved."
echo "To remove config too: rm -rf ~/.config/comix-dl/"
UNINSTALL
chmod +x "$BIN_DIR/comix-dl-uninstall"

# -- Ensure PATH --------------------------------------------------------------

ensure_path() {
    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        warn "$BIN_DIR is not in your PATH."

        local shell_rc=""
        case "$(basename "$SHELL")" in
            zsh)  shell_rc="$HOME/.zshrc" ;;
            bash) shell_rc="$HOME/.bashrc" ;;
            fish) shell_rc="$HOME/.config/fish/config.fish" ;;
            *)    shell_rc="$HOME/.profile" ;;
        esac

        local path_line='export PATH="$HOME/.local/bin:$PATH"'
        if [[ -n "$shell_rc" ]]; then
            if ! grep -qF '.local/bin' "$shell_rc" 2>/dev/null; then
                echo "" >> "$shell_rc"
                echo "# Added by comix-dl installer" >> "$shell_rc"
                echo "$path_line" >> "$shell_rc"
                success "Added $BIN_DIR to PATH in $shell_rc"
                warn "Run: source $shell_rc  (or open a new terminal)"
            else
                info "$BIN_DIR already referenced in $shell_rc"
            fi
        fi
    fi
}

ensure_path

# -- Done! --------------------------------------------------------------------

echo ""
echo -e "${GREEN}${BOLD}✓ Installation complete!${NC}"
echo ""
echo -e "  ${BOLD}Usage:${NC}"
echo -e "    ${CYAN}comix-dl${NC}                  # Interactive menu"
echo -e "    ${CYAN}comix-dl \"manga name\"${NC}     # Quick search"
echo -e "    ${CYAN}comix-dl download URL${NC}     # Non-interactive download"
echo -e "    ${CYAN}comix-dl doctor${NC}           # Check environment"
echo ""
echo -e "  ${BOLD}Paths:${NC}"
echo -e "    Install:  $INSTALL_DIR"
echo -e "    Command:  $BIN_DIR/comix-dl"
echo -e "    Config:   ~/.config/comix-dl/"
echo ""
echo -e "  ${BOLD}Update:${NC}    Re-run this script"
echo -e "  ${BOLD}Uninstall:${NC} comix-dl-uninstall"
echo ""
