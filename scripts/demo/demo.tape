# README demo — regenerate with `make demo` (needs `brew install vhs`).
#
# Runs against the throwaway HOME built by setup.sh, so nothing from the
# recorder's own history or config appears on screen.

Output doc/images/demo.gif

Require bash
Set Shell bash
Set FontSize 16
Set Width 1100
Set Height 560
Set Padding 16
Set TypingSpeed 60ms
Set Theme "Catppuccin Mocha"

Hide
Type "export HOME=/private/tmp/pitash-demo PS1='$ ' && cd ~/webapp && clear && $PITASH"
Enter
Sleep 2s
Ctrl+L
Show
Sleep 800ms

# ── TAB completion: subcommands with descriptions, narrowed by typing ──────
Type "git "
Tab
Sleep 1.2s
Type "sw"
Sleep 800ms
Down
Sleep 400ms
Enter
Sleep 400ms
Tab
Sleep 1.2s
Down
Down
Sleep 600ms
Enter
Sleep 400ms
Enter
Sleep 1.2s

# ── Multi-select flag picker: Space toggles, short flags merge ─────────────
Type "ls -"
Tab
Sleep 1.2s
Type "a"
Sleep 400ms
Type "a"
Sleep 400ms
Space
Sleep 500ms
Type "l"
Sleep 400ms
Space
Sleep 600ms
Enter
Sleep 600ms
Enter
Sleep 1.5s

# ── Context switching with a live process ──────────────────────────────────
Type "make build"
Enter
Sleep 3s
Ctrl+]
Sleep 1.2s
Ctrl+N
Sleep 400ms
Type "review"
Enter
Sleep 1s
Type "git log --oneline"
Enter
Sleep 1.5s
Ctrl+]
Sleep 1.5s
Down
Sleep 1.5s
Enter
Sleep 3s
