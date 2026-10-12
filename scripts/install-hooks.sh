#!/bin/sh
# Installs the repository's git hooks into .git/hooks. Run once after cloning.
set -eu
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
echo "hooks installed (core.hooksPath = .githooks)"
