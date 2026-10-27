#!/bin/sh
# Turnbook, installed in one step on macOS and Linux: no Python needed beforehand.
#
#   curl -LsSf https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.sh | sh
#
# It installs uv (Astral's installer, to your home folder, no sudo), which brings its own
# Python 3.12; installs Turnbook with it; then connects your AI apps (turnbook-install).
# Arguments go to turnbook-install (... | sh -s -- --name "Your Name"). GRAPH_MIND_VERSION pins one.
set -eu

if ! command -v uv >/dev/null 2>&1; then
    echo "Turnbook: installing uv (it brings Python)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
    export PATH
fi

SPEC="turnbook${GRAPH_MIND_VERSION:+==$GRAPH_MIND_VERSION}"
# the name before 0.9.44: its commands would clash with these; the memory itself is kept
uv tool list 2>/dev/null | grep -q "^graph-mind-memory " && uv tool uninstall graph-mind-memory >/dev/null
echo "Turnbook: installing $SPEC..."
uv tool install --python 3.12 --upgrade "$SPEC"
"$(uv tool dir --bin)/turnbook-install" "$@"
