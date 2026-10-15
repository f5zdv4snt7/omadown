#!/bin/sh
# Launch the omadown queue UI in a terminal that Hyprland will float.
#
# Every Omarchy release ships a floating-terminal launcher, but the exact name has
# moved around between versions, so we probe for the known spellings and fall back
# to whatever terminal is installed.
#
# ARGV SPLITTING (this bit caused a silent "Super+D does nothing" bug):
#   omarchy-launch-tui takes `<command> [args...]` and forwards them to
#   `xdg-terminal-exec -e "$1" "${@:2}"`, which execs the terminal with argv
#   [prog, arg1, arg2, ...]. So `omadown` and `tui` MUST be separate argv
#   entries. Writing `omarchy-launch-tui "omadown tui"` (one quoted word)
#   hands the terminal a single argument containing a space, so the terminal
#   tries to exec a binary literally named "omadown tui" and dies with ENOENT.
#   Keep them unquoted and separate -- never recombine them into $PROGRAM.
#
# APP-ID: omarchy-launch-tui derives its app-id from `basename $1`, i.e.
# `org.omarchy.omadown`, which is what the Hyprland window rule matches. Passing
# --app-id explicitly keeps it stable if this wrapper is ever renamed.
set -e

APP_ID=org.omarchy.omadown

if command -v omarchy-launch-tui >/dev/null 2>&1; then
    exec omarchy-launch-tui --app-id="$APP_ID" omadown tui
fi

for launcher in \
    omarchy-launch-floating-tui \
    omarchy-launch-floating-terminal-with-presentation \
    omarchy-launch-floating-terminal
do
    if command -v "$launcher" >/dev/null 2>&1; then
        exec "$launcher" omadown tui
    fi
done

# Raw-terminal fallback. Match on class AND title so the window rule catches it
# whichever of the two the terminal actually reports to Hyprland.
for term in ghostty alacritty kitty foot wezterm; do
    if command -v "$term" >/dev/null 2>&1; then
        case "$term" in
            ghostty)  exec ghostty --title=omadown --class="$APP_ID" -e omadown tui ;;
            alacritty) exec alacritty --title omadown --class "$APP_ID,$APP_ID" -e omadown tui ;;
            kitty)    exec kitty --title omadown --class "$APP_ID" omadown tui ;;
            foot)     exec foot --app-id="$APP_ID" --title omadown omadown tui ;;
            wezterm)  exec wezterm start --class "$APP_ID" omadown tui ;;
        esac
    fi
done

# No terminal found: run the plain CLI so the keybinding still does something useful.
exec omadown status
