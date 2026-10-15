#!/bin/sh
# omadown action picker.
#
# Two roles in one file:
#   1. installed as `omadown-menu`  -> standalone picker (walker / fzf)
#   2. sourced from ~/.config/omarchy/extensions/menu.sh by Omarchy's own menu,
#      which calls every defined `%s_menu()`-style hook it can find.
#
# The standalone picker is the guaranteed path: it only needs walker or fzf, both
# of which an Omarchy install already has.

omadown_menu() {
    omadown-menu
}

_omadown_actions() {
    cat <<'ACTIONS'
tui|打开队列界面|omadown-tui
clip|下载剪贴板链接|omadown-clipboard-add
status|查看状态|omadown status; printf '\n按回车关闭'; read _
retry|重试失败的任务|omadown retry && omadown-notify-status
cancel|取消所有任务|omadown cancel && omadown-notify-status
clear|清理已完成记录|omadown clear && omadown-notify-status
open|打开下载目录|xdg-open "$(omadown config --get general.download_dir)"
config|编辑配置|omadown config --edit
doctor|环境检查|omadown doctor; printf '\n按回车关闭'; read _
restart|重启守护进程|systemctl --user restart omadown.service
ACTIONS
}

_pick() {
    if command -v walker >/dev/null 2>&1; then
        walker --dmenu --width 520 --minheight 1 --placeholder "omadown" 2>/dev/null
    elif command -v fzf >/dev/null 2>&1; then
        fzf --prompt='omadown > ' --height=60% --reverse --no-info
    elif command -v rofi >/dev/null 2>&1; then
        rofi -dmenu -p omadown
    else
        return 1
    fi
}

main() {
    menu="$(_omadown_actions | sed 's/^[^|]*|//; s/|.*$//')" || return 1
    [ -n "$menu" ] || return 0
    command="$(_omadown_actions | awk -F'|' -v want="$menu" '$2 == want { print $3 }')"
    [ -n "$command" ] || return 0
    # The action runs in a detached shell: the UI is usually a launcher we want gone.
    if command -v setsid >/dev/null 2>&1; then
        setsid sh -c "$command" >/dev/null 2>&1 &
    else
        sh -c "$command" >/dev/null 2>&1 &
    fi
}

# Sourced by Omarchy's menu -> export only the hook. Executed directly -> show picker.
case "${0##*/}" in
    omadown-menu) main ;;
    *) : ;;
esac
