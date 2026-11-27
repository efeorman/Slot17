# Shared helpers for the setup scripts. Sourced, not run.

YES="${YES:-0}"
BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
if [ ! -t 1 ]; then BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""; fi

step() { printf '\n%s== %s ==%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '%s  ok:%s %s\n' "$GREEN" "$RESET" "$*"; }
note() { printf '%s  note:%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '\n%sStopped:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# ask_yes "Question" default(y|n) -> returns 0 for yes. With --yes, takes the default.
ask_yes() {
    local question="$1" default="$2" answer hint
    if [ "$default" = y ]; then hint="Y/n"; else hint="y/N"; fi
    if [ "$YES" = 1 ] || [ ! -t 0 ]; then
        [ "$default" = y ]
        return
    fi
    while true; do
        read -r -p "  $question [$hint] " answer
        answer="${answer:-$default}"
        case "$answer" in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
        esac
    done
}

# ask_text "Question" -> prints the answer. Empty when not interactive.
ask_text() {
    local answer=""
    if [ "$YES" != 1 ] && [ -t 0 ]; then
        read -r -p "  $1 " answer
    fi
    printf '%s' "$answer"
}

need_debian() {
    command -v apt-get >/dev/null || die "this needs a Debian-based system (Raspberry Pi OS, Debian or Ubuntu)."
    command -v systemctl >/dev/null || [ "${NO_SERVICES:-0}" = 1 ] || die "this needs systemd."
}

need_normal_user() {
    [ "$(id -u)" != 0 ] || die "run this as your normal user, not as root or with sudo. It asks for your password when it needs it."
    if ! sudo -n true 2>/dev/null; then
        echo "  Some steps need administrator rights, so sudo may ask for your password."
        sudo -v || die "this user can't use sudo."
    fi
}

apt_install() {
    step "Installing packages"
    echo "  $*"
    # Everything already there (the Pi image has these): nothing to fetch, so a first start without internet goes on
    # (found in review, 26 November 2026: apt-get update ran, and failed, even then).
    local p missing=0
    for p in "$@"; do
        [ "$(dpkg-query -W -f='${Status}' "$p" 2>/dev/null)" = "install ok installed" ] || missing=1
    done
    if [ "$missing" = 0 ]; then ok "already installed"; return 0; fi
    sudo env DEBIAN_FRONTEND=noninteractive apt-get update -qq || die "apt-get update failed. Is the internet connection working?"
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null || die "installing packages failed."
    ok "packages installed"
}

python_ok() {
    python3 - <<'EOF' 2>/dev/null
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
EOF
}
