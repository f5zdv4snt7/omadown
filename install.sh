#!/usr/bin/env bash
# omadown installer for Omarchy (Arch + Hyprland).
#
#   ./install.sh                 install, or UPDATE when already installed
#   ./install.sh --status        detect an existing install and report details
#   ./install.sh --no-deps       skip package installation
#   ./install.sh --no-integrations
#   ./install.sh --uninstall
#
# Design notes:
#   * Python code is copied to ~/.local/share/omadown/ (package: omadown/) and
#     launched through a wrapper in ~/.local/bin. We never touch pip: Arch marks
#     Python as externally-managed, and a download tool should not fight the
#     package manager.
#   * Omarchy integration files are written to ~/.config/omarchy/extensions/ so
#     the distribution's own defaults are left untouched.
#   * DANGER ZONE: ~/.local/share/omadown is BOTH the code directory (omadown/
#     package) AND the runtime data directory (omadown.db = queue + history,
#     run/ fallback when XDG_RUNTIME_DIR is unset). Neither the update path nor
#     --uninstall may rm -rf the directory as a whole; only the package
#     subdirectory is code.
#
# Detection & update behaviour:
#   * Re-running this installer on an existing install is an UPDATE. The
#     installed version is read from ~/.local/share/omadown/omadown/__init__.py
#     and compared with the source version; the daemon is stopped, the code is
#     swapped, files the new version no longer ships are pruned (tracked in
#     ~/.local/share/omadown/install-manifest.txt), and the daemon is restarted
#     if it was running. config.toml, the queue and the history are never
#     touched.
#   * ./install.sh --status prints the same detection as a report and exits 0
#     when omadown is installed, 1 when it is not -- usable from scripts.
#
# ---------------------------------------------------------------------------
# HYPRLAND CONFIG IS LUA NOW -- DO NOT WRITE .conf FILES
#
# Omarchy migrated ~/.config/hypr/ from plain Hyprland config (hyprland.conf +
# source <file>.conf) to Lua modules (hyprland.lua + require("hypr.*")). The
# ~/.config/omarchy/extensions/ directory is NOT part of the Hyprland config
# chain any more -- it holds menu .jsonc files and .sh hooks only, and nothing
# sources *.conf from it. An omadown.conf dropped there is silently ignored,
# which is why "Super+D does nothing" was the bug.
#
# So this installer installs integrations/omarchy/omadown.lua and wires it in via
# ~/.config/hypr/hyprland.lua. It supports BOTH Omarchy generations:
#   * Lua configs   -> `require("hypr.omadown")` (or dofile fallback)
#   * legacy .conf  -> `source = ~/.config/omadown/omadown.conf` (generated)
# ---------------------------------------------------------------------------

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omadown"
BIN_DIR="$HOME/.local/bin"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omadown"
OMARCHY_EXT="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/extensions"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
HYPR_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hypr"
MANIFEST="$LIB_DIR/install-manifest.txt"

WRAPPERS=(omadown omadown-waybar omadown-tui omadown-menu
          omadown-notify-status omadown-clipboard-add)

DEPS=(yt-dlp ffmpeg wl-clipboard libnotify python)
WITH_DEPS=1
WITH_INTEGRATIONS=1
ACTION="install"

# NOTE: helpers are defined before the argument loop on purpose -- the loop
# itself calls usage(), and bash only knows a function after its definition has
# been executed.
log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
omadown installer for Omarchy (Arch + Hyprland).

  ./install.sh                 安装；若检测到已安装则自动进入更新模式
  ./install.sh --status        检测是否已安装并报告安装详情
                               （已安装退出码 0，未安装退出码 1）
  ./install.sh --no-deps       跳过系统依赖安装
  ./install.sh --no-integrations  跳过 Omarchy 集成
  ./install.sh --uninstall     卸载（保留配置、下载队列与历史记录）
  ./install.sh -h | --help     显示本帮助

更新模式（重复执行安装脚本时自动触发）：
  * 读取已安装版本并与源码版本对比；
  * 更新前停止守护进程，完成后按原状态恢复；
  * 按安装清单清理新版本不再提供的旧文件；
  * 不覆盖 config.toml，不动下载队列与历史记录。
EOF
}

for arg in "$@"; do
  case "$arg" in
    --no-deps)          WITH_DEPS=0 ;;
    --no-integrations)  WITH_INTEGRATIONS=0 ;;
    --status)           ACTION="status" ;;
    --uninstall)        ACTION="uninstall" ;;
    -h|--help)          usage; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# ------------------------------------------------------------------- detection
# Everything about "is omadown already installed, and which version" lives here
# so --status and the update path can never drift apart.

pkg_version() {
  # Print __version__ from an omadown/__init__.py (source or installed copy).
  [ -f "$1" ] || return 0
  sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -n 1
}

have() { if [ -e "$1" ] || [ -L "$1" ]; then printf '有'; else printf '无'; fi; }

db_count() {
  # Row count of the queue/history DB, opened read-only (safe while running).
  python3 - "$LIB_DIR/omadown.db" <<'PY' 2>/dev/null || printf '?'
import sqlite3, sys
try:
    con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
    print("%d 条记录" % con.execute("select count(*) from downloads").fetchone()[0])
except Exception:
    print("?")
PY
}

detect_install() {
  # Sets INSTALLED_VERSION ("" when unknown) and ALREADY_INSTALLED (0/1).
  INSTALLED_VERSION="$(pkg_version "$LIB_DIR/omadown/__init__.py")"
  ALREADY_INSTALLED=0
  if [ -n "$INSTALLED_VERSION" ] || [ -d "$LIB_DIR/omadown" ] \
     || [ -f "$BIN_DIR/omadown" ] || [ -f "$MANIFEST" ]; then
    ALREADY_INSTALLED=1
  fi
}

report_status() {
  local src_v inst_v on_path w
  src_v="$(pkg_version "$SRC_DIR/omadown/__init__.py")"
  detect_install
  inst_v="$INSTALLED_VERSION"
  log "omadown 安装状态"
  printf '  源码版本:        %s\n' "${src_v:-unknown}"
  if [ "$ALREADY_INSTALLED" = 1 ]; then
    if [ -n "$inst_v" ]; then
      printf '  安装状态:        已安装 (v%s)\n' "$inst_v"
    else
      printf '  安装状态:        已安装（版本未知，可能是不完整安装）\n'
    fi
    if [ -n "$inst_v" ] && [ -n "$src_v" ] && [ "$inst_v" != "$src_v" ]; then
      printf '  可更新:          是 (v%s -> v%s)，重新运行 install.sh 即可\n' "$inst_v" "$src_v"
    elif [ -n "$inst_v" ] && [ "$inst_v" = "$src_v" ]; then
      printf '  可更新:          否（与源码同版本）\n'
    fi
  else
    printf '  安装状态:        未安装\n'
  fi
  printf '  代码目录:        %s [%s]\n' "$LIB_DIR/omadown" "$(have "$LIB_DIR/omadown")"
  printf '  安装清单:        %s [%s]\n' "$MANIFEST" "$(have "$MANIFEST")"
  if [ -f "$LIB_DIR/omadown.db" ]; then
    printf '  队列/历史库:     %s [%s]\n' "$LIB_DIR/omadown.db" "$(db_count)"
  else
    printf '  队列/历史库:     %s [无]\n' "$LIB_DIR/omadown.db"
  fi
  printf '  配置文件:        %s [%s]\n' "$CFG_DIR/config.toml" "$(have "$CFG_DIR/config.toml")"
  printf '  Waybar 片段:     %s [%s]\n' "$CFG_DIR/waybar-module.jsonc" "$(have "$CFG_DIR/waybar-module.jsonc")"
  for w in "${WRAPPERS[@]}"; do
    printf '  启动器 %-22s [%s]\n' "$w" "$(have "$BIN_DIR/$w")"
  done
  printf '  桌面入口:        omadown.desktop [%s]  omadown-add.desktop [%s]\n' \
    "$(have "$DESKTOP_DIR/omadown.desktop")" "$(have "$DESKTOP_DIR/omadown-add.desktop")"
  if [ -f "$SYSTEMD_DIR/omadown.service" ]; then
    if command -v systemctl >/dev/null 2>&1; then
      printf '  systemd 服务:    已安装 (%s / %s)\n' \
        "$(systemctl --user is-enabled omadown.service 2>/dev/null || echo unknown)" \
        "$(systemctl --user is-active omadown.service 2>/dev/null || echo inactive)"
    else
      printf '  systemd 服务:    单元文件已安装（systemctl 不可用）\n'
    fi
  else
    printf '  systemd 服务:    未安装\n'
  fi
  if [ -f "$HYPR_DIR/hyprland.lua" ] && grep -q 'omadown' "$HYPR_DIR/hyprland.lua" 2>/dev/null; then
    printf '  Hyprland 接入:   %s/hyprland.lua (Lua 模块)\n' "$HYPR_DIR"
  elif [ -f "$HYPR_DIR/hyprland.conf" ] && grep -q 'omadown' "$HYPR_DIR/hyprland.conf" 2>/dev/null; then
    printf '  Hyprland 接入:   %s/hyprland.conf (legacy source)\n' "$HYPR_DIR"
  else
    printf '  Hyprland 接入:   未检测到\n'
  fi
  printf '  Lua 模块:        %s [%s]\n' "$HYPR_DIR/omadown.lua" "$(have "$HYPR_DIR/omadown.lua")"
  printf '  旧版 extensions: %s [%s]\n' "$OMARCHY_EXT/omadown.conf" "$(have "$OMARCHY_EXT/omadown.conf")"
  on_path="$(command -v omadown 2>/dev/null || true)"
  if [ -z "$on_path" ]; then
    printf '  PATH 中的 omadown: 无\n'
  elif [ "$on_path" = "$BIN_DIR/omadown" ]; then
    printf '  PATH 中的 omadown: %s (本安装)\n' "$on_path"
  else
    printf '  PATH 中的 omadown: %s (另一个安装，会遮蔽 %s)\n' "$on_path" "$BIN_DIR/omadown"
  fi
}

if [ "$ACTION" = "status" ]; then
  detect_install
  report_status
  if [ "$ALREADY_INSTALLED" = 1 ]; then exit 0; else exit 1; fi
fi

# ------------------------------------------------------------- hyprland surgery
# NOTE: these helpers are defined BEFORE the uninstall block on purpose -- an
# earlier revision called them from --uninstall before their definitions, which
# made every uninstall die with "command not found" half-way through.

# Remove a previously-appended loader line, so re-running the installer is
# idempotent instead of stacking duplicates.
#
# The installer appends exactly two lines to the END of hyprland.lua:
#     -- omadown: keybindings and window rules
#     require("hypr.omadown")
# so removal is a whole-block match on the tail, not a per-line filter. A
# per-line filter can't tell "our loader line" from a user line that happens to
# mention omadown, and it also cannot safely drop the preceding comment.
hypr_remove_loader_line() {
  [ -f "$HYPR_DIR/hyprland.lua" ] || return 0
  python3 - "$HYPR_DIR/hyprland.lua" <<'PY'
import os
import re
import sys
import tempfile

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    text = fh.read()

# An appended block: optional leading blank lines, the banner comment, then one
# loader line. Anchored to the end of the file so only what we appended is
# removed and nothing the user wrote earlier is touched.
block = re.compile(
    r"(?:\n)*"
    r"--\s*omadown:.*\n"
    r"(?:require\(\"hypr\.omadown\"\)"
    r"|dofile\(.*omadown\.lua\")\)?"
    r"\s*\n*\Z"
)
new = block.sub("\n", text)
if new != text:
    with tempfile.NamedTemporaryFile(
        "w", dir=os.path.dirname(path), delete=False, encoding="utf-8"
    ) as tf:
        tf.write(new)
        tmp = tf.name
    os.replace(tmp, path)
    print("removed omadown loader block from " + path)
PY
}

# Drop the `source = .../omadown/omadown.conf` line (plus its banner comment)
# that the legacy (.conf) branch appended to hyprland.conf. Needed when a
# machine migrates from the legacy config to the Lua config, and on uninstall.
hypr_remove_conf_source_line() {
  [ -f "$HYPR_DIR/hyprland.conf" ] || return 0
  python3 - "$HYPR_DIR/hyprland.conf" <<'PY'
import os
import re
import sys
import tempfile

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    text = fh.read()

pattern = re.compile(
    r"(?:\n)*"
    r"#\s*omadown:.*\n"
    r"source\s*=\s*.*omadown\.conf\s*\n*"
)
new = pattern.sub("\n", text)
if new != text:
    with tempfile.NamedTemporaryFile(
        "w", dir=os.path.dirname(path), delete=False, encoding="utf-8"
    ) as tf:
        tf.write(new)
        tmp = tf.name
    os.replace(tmp, path)
    print("removed omadown source line from " + path)
PY
}

# Strip a bindings block that an older version of the installer told the user to
# paste into ~/.config/hypr/bindings.lua by hand.
hypr_remove_pasted_block() {
  [ -f "$HYPR_DIR/bindings.lua" ] || return 0
  python3 - "$HYPR_DIR/bindings.lua" <<'PY'
import sys, os, re, tempfile
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
# The pasted block always starts with this banner comment.
pattern = re.compile(
    r"\n-- =+\n-- omadown.*?\n(?:-- =+\n)?.*?(?=\n-- =+\n-- (?:FIX|Personal|Logitech)|\Z)",
    re.S,
)
new = pattern.sub("\n", text)
if new != text:
    with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path),
                                     delete=False, encoding="utf-8") as tf:
        tf.write(new)
        tmp = tf.name
    os.replace(tmp, path)
PY
}

# --------------------------------------------------------------------- uninstall
if [ "$ACTION" = "uninstall" ]; then
  detect_install
  if [ "$ALREADY_INSTALLED" = 0 ]; then
    log "未检测到已安装的 omadown，无需卸载"
    exit 0
  fi
  log "检测到已安装的 omadown${INSTALLED_VERSION:+ v$INSTALLED_VERSION}，开始卸载"

  log "停止并禁用守护进程"
  systemctl --user disable --now omadown.service 2>/dev/null || true

  log "移除文件"
  for w in "${WRAPPERS[@]}"; do
    rm -f "$BIN_DIR/$w"
  done
  rm -f "$OMARCHY_EXT/omadown.lua" "$OMARCHY_EXT/omadown-menu.sh"
  rm -f "$SYSTEMD_DIR/omadown.service"
  rm -f "$DESKTOP_DIR/omadown.desktop" "$DESKTOP_DIR/omadown-add.desktop"
  rm -f "$CFG_DIR/omadown.conf" "$CFG_DIR/waybar-module.jsonc"
  rm -f "$LIB_DIR/README.md" "$LIB_DIR/omadown.py" "$MANIFEST"
  # Code only. ~/.local/share/omadown also holds omadown.db (queue + history),
  # which is user data and must survive an uninstall.
  rm -rf "$LIB_DIR/omadown" "$LIB_DIR/run"

  # Drop the loader line we appended to hyprland.lua, and remove the Lua module
  # if it was installed under ~/.config/hypr/.
  if [ -f "$HYPR_DIR/hyprland.conf" ]; then
    hypr_remove_conf_source_line
  fi
  if [ -f "$HYPR_DIR/hyprland.lua" ]; then
    hypr_remove_loader_line
  fi
  rm -f "$HYPR_DIR/omadown.lua"

  # Legacy installs put a dead .conf in the extensions dir -- clean that up too.
  rm -f "$OMARCHY_EXT/omadown.conf"

  # Remove the keybindings that an earlier version of this installer may have
  # asked the user to paste into ~/.config/hypr/bindings.lua by hand.
  hypr_remove_pasted_block

  systemctl --user daemon-reload 2>/dev/null || true
  log "已卸载。配置与下载记录保留在 $CFG_DIR/config.toml 与 $LIB_DIR/omadown.db"
  exit 0
fi

# ------------------------------------------------------------------------ checks
command -v python3 >/dev/null 2>&1 || die "缺少 python3"
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "需要 Python 3.11+（当前 ${PYVER}）。tomllib 依赖 3.11。"
log "Python $PYVER"

# --------------------------------------------------------------- update preamble
# Compare the installed copy with the source and switch into update mode. This
# is the "is it already installed" check the installer runs on every invocation.
SRC_VERSION="$(pkg_version "$SRC_DIR/omadown/__init__.py")"
[ -n "$SRC_VERSION" ] || SRC_VERSION="unknown"

NEW_FILES=()          # every path this run installs; feeds the manifest
PRUNED=0              # stale files removed from a previous install
DAEMON_WAS_ACTIVE=0   # restore the daemon after swapping the code

detect_install
if [ "$ALREADY_INSTALLED" = 1 ]; then
  if [ -n "$INSTALLED_VERSION" ] && [ "$INSTALLED_VERSION" != "$SRC_VERSION" ]; then
    log "检测到已安装 omadown v${INSTALLED_VERSION}，进入更新模式（目标 v${SRC_VERSION}）"
  elif [ -n "$INSTALLED_VERSION" ]; then
    log "检测到已安装 omadown v${INSTALLED_VERSION}（与源码同版本，按重装/修复处理）"
  else
    log "检测到已存在的 omadown 安装（版本未知），按更新处理"
  fi
  log "更新模式：config.toml、下载队列与历史记录原样保留"
  if [ -f "$LIB_DIR/omadown.db" ]; then
    log "现有下载记录 $(db_count)，更新后仍在 $LIB_DIR/omadown.db"
  fi
  if command -v systemctl >/dev/null 2>&1 \
     && systemctl --user is-active --quiet omadown.service 2>/dev/null; then
    DAEMON_WAS_ACTIVE=1
    log "停止 omadown 守护进程（更新完成后自动恢复）"
    systemctl --user stop omadown.service 2>/dev/null || true
  fi
else
  log "未检测到已有安装，执行全新安装"
fi

if [ "$WITH_DEPS" = 1 ]; then
  if command -v omarchy-pkg-install >/dev/null 2>&1; then
    log "通过 omarchy-pkg-install 安装依赖: ${DEPS[*]}"
    omarchy-pkg-install "${DEPS[@]}"
  else
    warn "未找到 omarchy-pkg-install（不是在 Omarchy 上？）"
    warn "请手动安装: pacman -S ${DEPS[*]}"
  fi
fi

# ------------------------------------------------------------------- python side
log "安装 Python 包到 $LIB_DIR"
mkdir -p "$LIB_DIR" "$BIN_DIR"
rm -rf "$LIB_DIR/omadown" "$LIB_DIR/omadown.py"
cp -r "$SRC_DIR/omadown" "$LIB_DIR/omadown"
find "$LIB_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
NEW_FILES+=("$LIB_DIR/omadown")

# The systemd unit points Documentation= at this file; ship it next to the code.
if [ -f "$SRC_DIR/README.md" ]; then
  install -Dm644 "$SRC_DIR/README.md" "$LIB_DIR/README.md"
  NEW_FILES+=("$LIB_DIR/README.md")
fi

write_wrapper() {
  local name="$1"
  cat > "$BIN_DIR/$name" <<EOF
#!/bin/sh
# generated by omadown install.sh
PYTHONPATH="$LIB_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec python3 -m omadown "\$@"
EOF
  chmod +x "$BIN_DIR/$name"
  NEW_FILES+=("$BIN_DIR/$name")
}

write_wrapper omadown

cat > "$BIN_DIR/omadown-waybar" <<EOF
#!/bin/sh
exec "$BIN_DIR/omadown" status --waybar "\$@"
EOF
chmod +x "$BIN_DIR/omadown-waybar"
NEW_FILES+=("$BIN_DIR/omadown-waybar")

cat > "$BIN_DIR/omadown-notify-status" <<'EOF'
#!/bin/sh
# Show the queue summary as a desktop notification.
summary="$(omadown status 2>/dev/null | tail -1)"
if command -v notify-send >/dev/null 2>&1; then
  notify-send -a omadown -i folder-download "omadown" "${summary:-队列为空}"
else
  printf '%s\n' "${summary:-队列为空}"
fi
EOF
chmod +x "$BIN_DIR/omadown-notify-status"
NEW_FILES+=("$BIN_DIR/omadown-notify-status")

cat > "$BIN_DIR/omadown-clipboard-add" <<'EOF'
#!/bin/sh
# Feed the current clipboard into the queue (daemon picks it up if it is running).
exec omadown paste --force
EOF
chmod +x "$BIN_DIR/omadown-clipboard-add"
NEW_FILES+=("$BIN_DIR/omadown-clipboard-add")

# ------------------------------------------------------- hyprland config wiring
# Everything below is about making the keybindings actually register. There are
# three generations of Omarchy to cope with, and the install must not silently
# do nothing on any of them:
#
#   A. Lua config (current)      hyprland.lua + require("hypr.*")
#   B. Lua config, no bootstrap  hyprland.lua that does not set up package.path
#   C. legacy plain config       hyprland.conf + source <file>.conf
#
# Detect which one we have and wire accordingly. Never write into a .conf file in
# the extensions dir: nothing loads those any more.

if [ "$WITH_INTEGRATIONS" = 1 ]; then
  log "安装 Omarchy 集成"
  mkdir -p "$BIN_DIR" "$CFG_DIR" "$OMARCHY_EXT" "$SYSTEMD_DIR" "$DESKTOP_DIR"
  install -Dm755 "$SRC_DIR/integrations/omarchy/omadown-tui.sh" "$BIN_DIR/omadown-tui"
  NEW_FILES+=("$BIN_DIR/omadown-tui")
  install -Dm755 "$SRC_DIR/integrations/omarchy/omadown-menu.sh" "$BIN_DIR/omadown-menu"
  NEW_FILES+=("$BIN_DIR/omadown-menu")
  install -Dm644 "$SRC_DIR/integrations/omarchy/omadown-menu.sh" "$OMARCHY_EXT/omadown-menu.sh"
  NEW_FILES+=("$OMARCHY_EXT/omadown-menu.sh")
  install -Dm644 "$SRC_DIR/integrations/omarchy/omadown.desktop" "$DESKTOP_DIR/omadown.desktop"
  NEW_FILES+=("$DESKTOP_DIR/omadown.desktop")
  install -Dm644 "$SRC_DIR/integrations/omarchy/omadown-add.desktop" "$DESKTOP_DIR/omadown-add.desktop"
  NEW_FILES+=("$DESKTOP_DIR/omadown-add.desktop")
  install -Dm644 "$SRC_DIR/integrations/omarchy/omadown.service" "$SYSTEMD_DIR/omadown.service"
  NEW_FILES+=("$SYSTEMD_DIR/omadown.service")
  install -Dm644 "$SRC_DIR/integrations/omarchy/waybar-module.jsonc" "$CFG_DIR/waybar-module.jsonc"
  NEW_FILES+=("$CFG_DIR/waybar-module.jsonc")

  # Retire the legacy .conf: it never loaded, and leaving it around invites
  # someone to "fix" the keybinding by editing a dead file.
  if [ -f "$OMARCHY_EXT/omadown.conf" ]; then
    warn "移除旧的 ~/.config/omarchy/extensions/omadown.conf（新版 Omarchy 不加载 .conf，绑定全部失效）"
    rm -f "$OMARCHY_EXT/omadown.conf"
  fi

  # --- wire the keybindings into the Hyprland config -------------------------
  if [ -f "$HYPR_DIR/hyprland.conf" ] && [ ! -f "$HYPR_DIR/hyprland.lua" ]; then
    # (C) legacy plain config: source a generated .conf.
    log "检测到旧版 hyprland.conf，写入 $CFG_DIR/omadown.conf 并 source"
    mkdir -p "$CFG_DIR"
    cat > "$CFG_DIR/omadown.conf" <<'CONF'
# omadown: generated by install.sh for a legacy (non-Lua) Hyprland config.
$omadownTui = omadown-tui
$omadownClip = omadown-clipboard-add

bindd = SUPER, D, omadown 队列, exec, $omadownTui
bindd = SUPER SHIFT, D, 下载剪贴板链接, exec, $omadownClip
bindd = SUPER ALT, D, omadown 状态, exec, omadown-notify-status

windowrule = float, match:class:^(omadown)$
windowrule = size 1000 640, match:class:^(omadown)$
windowrule = center, match:class:^(omadown)$
windowrule = float, match:class:^(org\.omarchy\.omadown)$
windowrule = size 1000 640, match:class:^(org\.omarchy\.omadown)$
windowrule = center, match:class:^(org\.omarchy\.omadown)$
CONF
    NEW_FILES+=("$CFG_DIR/omadown.conf")
    if ! grep -q 'omadown.conf' "$HYPR_DIR/hyprland.conf"; then
      printf '\n# omadown: keybindings and window rules\nsource = %s\n' "$CFG_DIR/omadown.conf" \
        >> "$HYPR_DIR/hyprland.conf"
      log "已追加 source 行到 $HYPR_DIR/hyprland.conf"
    fi

  elif [ -f "$HYPR_DIR/hyprland.lua" ]; then
    # (A)/(B) Lua config: install the Lua module and require it.
    install -Dm644 "$SRC_DIR/integrations/omarchy/omadown.lua" "$HYPR_DIR/omadown.lua"
    NEW_FILES+=("$HYPR_DIR/omadown.lua")
    log "已安装 Lua 模块 $HYPR_DIR/omadown.lua"

    # An install made while the machine still had a legacy hyprland.conf
    # generated $CFG_DIR/omadown.conf and sourced it from there. On the Lua
    # branch that file is dead weight pointing at stale bindings -- retire it
    # together with the source line so nothing dangles.
    if [ -f "$CFG_DIR/omadown.conf" ]; then
      warn "移除旧版生成的 $CFG_DIR/omadown.conf（已改用 Lua 模块）"
      rm -f "$CFG_DIR/omadown.conf"
      hypr_remove_conf_source_line
    fi

    # Does this config set up ~/.config/?.lua on package.path (Omarchy's
    # bootstrap.lua does)? If yes, `require("hypr.omadown")` resolves. If not,
    # fall back to dofile with an absolute path.
    hypr_remove_loader_line
    if grep -q 'package\.path' "$HYPR_DIR/hyprland.lua" 2>/dev/null \
       || grep -q 'bootstrap\.lua' "$HYPR_DIR/hyprland.lua" 2>/dev/null; then
      loader='-- omadown: keybindings and window rules
require("hypr.omadown")'
    else
      loader="-- omadown: keybindings and window rules
dofile(os.getenv(\"HOME\") .. \"/.config/hypr/omadown.lua\")"
    fi
    printf '\n%s\n' "$loader" >> "$HYPR_DIR/hyprland.lua"
    log "已在 $HYPR_DIR/hyprland.lua 追加 omadown 加载行"

    # Remove any block an older install asked the user to paste by hand: the
    # module owns the bindings now, and duplicates are confusing.
    if grep -q 'omadown' "$HYPR_DIR/bindings.lua" 2>/dev/null; then
      warn "从 $HYPR_DIR/bindings.lua 移除手工粘贴的 omadown 绑定（改由 omadown.lua 提供）"
      hypr_remove_pasted_block
    fi
  else
    warn "未找到 $HYPR_DIR/hyprland.conf 或 hyprland.lua，跳过键位绑定"
    warn "请手动把 integrations/omarchy/omadown.lua 接进你的 Hyprland 配置"
  fi

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
  fi
  systemctl --user daemon-reload 2>/dev/null || true
fi

# ------------------------------------------------------------------------ config
if [ ! -f "$CFG_DIR/config.toml" ]; then
  log "写入默认配置 $CFG_DIR/config.toml"
  mkdir -p "$CFG_DIR"
  PYTHONPATH="$LIB_DIR" python3 -c 'from omadown.config import Config; Config().save()'
else
  log "保留已有配置 $CFG_DIR/config.toml"
fi
# config.toml is user data: always keep it in the manifest so pruning can never
# remove it, no matter which version installed it.
NEW_FILES+=("$CFG_DIR/config.toml")

# ------------------------------------------------------------ stale file pruning
# The manifest records every path a previous run installed. Anything in it that
# this run did NOT re-install is a file the new version no longer ships (renamed
# wrapper, retired integration) -- remove it so updates do not accumulate junk.
prune_stale_files() {
  [ -f "$MANIFEST" ] || return 0
  local f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in /*) ;; *) continue ;; esac   # never trust a relative path
    # Sections skipped with --no-integrations keep their old files.
    if [ "$WITH_INTEGRATIONS" = 0 ]; then
      case "$f" in
        "$BIN_DIR"/omadown-tui|"$BIN_DIR"/omadown-menu|"$OMARCHY_EXT"/*|\
        "$DESKTOP_DIR"/omadown*.desktop|"$SYSTEMD_DIR"/omadown.service|\
        "$CFG_DIR"/waybar-module.jsonc|"$CFG_DIR"/omadown.conf|"$HYPR_DIR"/omadown.lua)
          continue ;;
      esac
    fi
    if ! printf '%s\n' ${NEW_FILES[@]+"${NEW_FILES[@]}"} | grep -Fxq -- "$f"; then
      if [ -e "$f" ] || [ -L "$f" ]; then
        log "清理旧版本文件: $f"
        rm -rf -- "$f"
        PRUNED=$((PRUNED + 1))
      fi
    fi
  done < "$MANIFEST"
}

write_manifest() {
  local files=() f
  if [ "$WITH_INTEGRATIONS" = 0 ] && [ -f "$MANIFEST" ]; then
    # Integrations were not touched this run: carry their manifest entries over
    # so a --no-integrations update does not orphan them.
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      { [ -e "$f" ] || [ -L "$f" ]; } && files+=("$f")
    done < "$MANIFEST"
  fi
  files+=(${NEW_FILES[@]+"${NEW_FILES[@]}"})
  printf '%s\n' ${files[@]+"${files[@]}"} | sort -u > "$MANIFEST"
}

prune_stale_files
write_manifest
if [ "$PRUNED" -gt 0 ]; then log "共清理 $PRUNED 个旧版本文件"; fi

# ---------------------------------------------------------------------- sanity
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR 不在 PATH 中，请加入 shell 配置" ;;
esac

log "自检"
PYTHONPATH="$LIB_DIR" python3 -m omadown doctor || true

# Verify the keybindings actually registered, and say so -- a binding that fails
# to register is the exact bug this file exists to prevent.
if command -v hyprctl >/dev/null 2>&1; then
  log "校验键位是否生效"
  if hyprctl -j binds 2>/dev/null | grep -q 'omadown'; then
    log "键位已注册（Super+D 等）"
  else
    warn "键位未注册。若 Hyprland 尚未重载配置，请执行: hyprctl reload"
  fi
fi

# ------------------------------------------------------------------ daemon back
if [ "$DAEMON_WAS_ACTIVE" = 1 ]; then
  log "恢复 omadown 守护进程"
  systemctl --user start omadown.service 2>/dev/null \
    || warn "守护进程启动失败，可手动执行: systemctl --user start omadown.service"
fi

# ---------------------------------------------------------------------- summary
if [ "$ALREADY_INSTALLED" = 1 ]; then
  if [ "$DAEMON_WAS_ACTIVE" = 1 ]; then
    daemon_note="守护进程已按原状态恢复运行"
  else
    daemon_note="守护进程未自动启动（更新前也未运行）；需要: systemctl --user enable --now omadown.service"
  fi
  cat <<EOF

更新完成（v${INSTALLED_VERSION:-unknown} -> v${SRC_VERSION}）${PRUNED:+，清理旧文件 $PRUNED 个}。

  已保留: $CFG_DIR/config.toml（配置）、$LIB_DIR/omadown.db（队列与历史）
  $daemon_note

  验证: omadown status / Super+D
EOF
else
  cat <<EOF

完成。

  下一步
    systemctl --user enable --now omadown.service     # 常驻队列 + 剪贴板监听
    omadown add 'https://...' -f                      # 试跑一个
    Super + D                                         # 打开队列界面
    Super + Shift + D                                 # 下载剪贴板里的链接

  Waybar 模块配置片段已复制到 $CFG_DIR/waybar-module.jsonc
  Hyprland 键位与窗口规则: $HYPR_DIR/omadown.lua
  以后重复执行本脚本即为更新（自动保留配置与队列），./install.sh --status 可查看安装状态
EOF
fi
