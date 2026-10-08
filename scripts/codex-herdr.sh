# Source from ~/.bash_aliases to keep Codex hooks attached to their Herdr pane.
# Codex 0.161+ otherwise runs hooks in a shared daemon's inherited environment.
codex() {
    if [[ "${HERDR_ENV:-}" == "1" ]]; then
        command codex --no-daemon "$@"
    else
        command codex "$@"
    fi
}
