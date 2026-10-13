#!/bin/sh
# Graph-MIND, installed in one step on macOS and Linux: no Python needed beforehand.
#
#   curl -LsSf https://raw.githubusercontent.com/goyohan0611-png/graph-mind/main/scripts/install.sh | sh
#
# It installs uv (Astral's installer, to your home folder, no sudo), which brings its own
# Python 3.12; installs Graph-MIND with it; then connects your AI apps (graph-mind-install).
# Arguments go to graph-mind-install (... | sh -s -- --name 고요한). GRAPH_MIND_VERSION pins one.
set -eu

if ! command -v uv >/dev/null 2>&1; then
    echo "Graph-MIND: installing uv (it brings Python)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
    export PATH
fi

SPEC="graph-mind-memory${GRAPH_MIND_VERSION:+==$GRAPH_MIND_VERSION}"
echo "Graph-MIND: installing $SPEC..."
uv tool install --python 3.12 --upgrade "$SPEC"
"$(uv tool dir --bin)/graph-mind-install" "$@"
