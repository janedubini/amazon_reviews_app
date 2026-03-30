#!/bin/bash
#
# Amazon Reviews Scraper — Lokaler Start (macOS)
# Doppelklick startet den Server mit sichtbarem Browser.
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/backend"

echo "========================================"
echo "  Amazon Reviews Scraper (Lokal)"
echo "========================================"
echo ""

# Python prüfen
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "FEHLER: Python ist nicht installiert!"
    echo "Bitte installiere Python 3.10+ von https://python.org"
    read -p "Drücke Enter..."
    exit 1
fi

echo "Python: $($PYTHON --version)"

# venv erstellen (einmalig)
if [ ! -d "venv" ]; then
    echo "Erstelle virtuelle Umgebung (einmalig)..."
    $PYTHON -m venv venv
fi

source venv/bin/activate

echo "Prüfe Abhängigkeiten..."
pip install -q -r requirements.txt 2>/dev/null

# Playwright-Browser (einmalig)
if [ ! -d "$HOME/Library/Caches/ms-playwright" ] && [ ! -d "$HOME/.cache/ms-playwright" ]; then
    echo "Installiere Browser (einmalig, 1-2 Min)..."
    playwright install chromium
fi

# LOKALER MODUS: Browser wird sichtbar geöffnet
export HEADLESS=false
export PORT=8000

echo ""
echo "Server startet auf: http://localhost:8000"
echo "Modus: Lokal (Browser sichtbar)"
echo "Zum Beenden: Ctrl+C"
echo "========================================"

sleep 1
open "http://localhost:8000" 2>/dev/null &

$PYTHON server.py
