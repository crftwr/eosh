#!/usr/bin/env bash
# Build a throwaway HOME for recording the README demo (see demo.tape).
#
# The recording must not show the author's real history, config or paths, so
# pitash runs against a fresh HOME holding the starter config and a small git
# project to complete against.
set -euo pipefail

DEMO_HOME=${DEMO_HOME:-/private/tmp/pitash-demo}
rm -rf "$DEMO_HOME"
mkdir -p "$DEMO_HOME/webapp"
cd "$DEMO_HOME/webapp"

git init -q -b main
git config user.name demo
git config user.email demo@example.com
printf 'build:\n\t@for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do echo "[$$i/20] compiling module_$$i.o"; sleep 1; done\n\ntest:\n\t@echo ok\n\nlint:\n\t@echo ok\n' > Makefile
echo "# webapp" > README.md
mkdir -p src && echo "print('hi')" > src/app.py
git add -A && git commit -qm "Initial commit"
for b in feature/login feature/search fix/timeout release/1.2; do git branch "$b"; done
