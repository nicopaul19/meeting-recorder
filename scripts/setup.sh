#!/bin/bash
# ============================================================
# Meeting Recorder — Setup Script
# Installs dependencies and guides audio device configuration.
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

print_step() { echo -e "${GREEN}▸ $1${NC}"; }
print_warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
print_err()  { echo -e "${RED}✗ $1${NC}"; }

echo "╔══════════════════════════════════════════╗"
echo "║   Meeting Recorder — Setup               ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ──────────────────────────────────────────────
# 1. Check/install Homebrew packages
# ──────────────────────────────────────────────
print_step "Checking Homebrew packages..."

brew_install_if_missing() {
    if brew list "$1" &>/dev/null || brew list --cask "$1" &>/dev/null; then
        echo "  ✓ $1 already installed"
    else
        echo "  Installing $1..."
        if [ "$2" = "cask" ]; then
            brew install --cask "$1"
        else
            brew install "$1"
        fi
        echo "  ✓ $1 installed"
    fi
}

brew_install_if_missing "blackhole-2ch" "cask"
brew_install_if_missing "switchaudio-osx"
brew_install_if_missing "ffmpeg"

echo ""

# ──────────────────────────────────────────────
# 2. Create virtual environment & install deps
# ──────────────────────────────────────────────
print_step "Setting up Python virtual environment..."

if [ ! -d "$SCRIPT_DIR/venv" ]; then
    python3 -m venv "$SCRIPT_DIR/venv"
    echo "  ✓ Virtual environment created"
else
    echo "  ✓ Virtual environment already exists"
fi

source "$SCRIPT_DIR/venv/bin/activate"

print_step "Installing Python dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "  ✓ Python packages installed (in venv)"
echo ""

# ──────────────────────────────────────────────
# 3. Check audio devices
# ──────────────────────────────────────────────
print_step "Checking audio devices..."

# Check BlackHole
if SwitchAudioSource -a -t input 2>/dev/null | grep -qi "blackhole"; then
    echo "  ✓ BlackHole 2ch detected"
    BH_READY=true
else
    print_warn "BlackHole 2ch not detected."
    echo "  You may need to reboot after installation."
    BH_READY=false
fi

# Check Multi-Output Device
if SwitchAudioSource -a -t output 2>/dev/null | grep -qi "multi-output"; then
    echo "  ✓ Multi-Output Device detected"
    MO_READY=true
else
    print_warn "Multi-Output Device not found."
    echo "  You need to create it in Audio MIDI Setup."
    MO_READY=false
fi

echo ""

# ──────────────────────────────────────────────
# 4. Guide Multi-Output Device setup if needed
# ──────────────────────────────────────────────
if [ "$MO_READY" = false ]; then
    echo "╔══════════════════════════════════════════╗"
    echo "║   Audio Device Setup (one-time)          ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    echo "  You need to create a Multi-Output Device so"
    echo "  the app can capture system audio while you"
    echo "  still hear it through your speakers."
    echo ""
    echo "  Steps:"
    echo "  1. Audio MIDI Setup will open automatically"
    echo "  2. Click '+' at bottom-left"
    echo "  3. Select 'Create Multi-Output Device'"
    echo "  4. Check BOTH:"
    echo "     ☑ BlackHole 2ch"
    echo "     ☑ MacBook Air Speakers (or your output device)"
    echo "  5. Close Audio MIDI Setup"
    echo ""
    read -p "Press Enter to open Audio MIDI Setup..." _
    open "/Applications/Utilities/Audio MIDI Setup.app"
    echo ""
    read -p "Press Enter when you've created the Multi-Output Device..." _
    
    # Re-check
    if SwitchAudioSource -a -t output 2>/dev/null | grep -qi "multi-output"; then
        echo "  ✓ Multi-Output Device detected!"
        MO_READY=true
    else
        print_warn "Multi-Output Device still not detected."
        echo "  You can set it up later. The app will guide you."
    fi
    echo ""
fi

# ──────────────────────────────────────────────
# 5. Create data directories
# ──────────────────────────────────────────────
print_step "Creating data directories..."
mkdir -p "$SCRIPT_DIR/data/recordings"
mkdir -p "$SCRIPT_DIR/data/transcripts"
mkdir -p "$SCRIPT_DIR/data/meetings"
echo "  ✓ data/recordings/"
echo "  ✓ data/transcripts/"
echo "  ✓ data/meetings/"
echo ""

# ──────────────────────────────────────────────
# 6. Summary
# ──────────────────────────────────────────────
echo "╔══════════════════════════════════════════╗"
echo "║   Setup Complete                         ║"
echo "╚══════════════════════════════════════════╝"
echo ""

if [ "$BH_READY" = true ] && [ "$MO_READY" = true ]; then
    echo -e "  ${GREEN}✅ Everything is ready!${NC}"
else
    echo -e "  ${YELLOW}⚠ Some items need attention (see above)${NC}"
    if [ "$BH_READY" = false ]; then
        echo "    → Reboot your Mac for BlackHole to appear"
    fi
fi

echo ""
echo "  To start the menubar app:"
echo "    source venv/bin/activate"
echo "    python3 app.py"
echo ""
echo "  The 🎙️ icon will appear in your menu bar."
echo "  Click it → toggle the floating window."
echo "  Right-click → Start Recording, Open Dashboard, Quit."
echo ""
echo "  Dashboard (browse meetings): http://localhost:5555"
echo ""
