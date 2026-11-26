#!/usr/bin/env bash
# Slot17 setup. Run it from the Slot17 folder as your normal user:
#
#     bash setup.sh              asks what this machine is
#     bash setup.sh car          the computer in the car, with the receiver
#     bash setup.sh server       the display server the phone connects to
#     bash setup.sh car --check  only report what is installed; changes nothing
#     bash setup.sh modem        optional, on the car computer: a ZTE MF833N 4G stick
#     bash setup.sh doctor       one plain verdict per part: why does it not work? Changes nothing
#     bash setup.sh logs         a support package: one zip with what someone needs to review this car computer
#
# Add --help after car, server or modem to see every option.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

role="${1:-}"
case "$role" in
    car|server|modem) shift ;;
    doctor) shift; exec python3 "$HERE/deploy/doctor.py" "$@" ;;
    logs) shift; [ $# -gt 0 ] || set -- --ask; exec python3 -B "$HERE/p0/support.py" "$@" ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "")
        if [ ! -t 0 ]; then
            echo "Say which machine this is: bash setup.sh car, or bash setup.sh server" >&2
            exit 2
        fi
        cat <<'EOF'

Slot17 setup. What is this machine?

  1) The car computer: a Raspberry Pi (or similar) with its radio receiver
  2) The display server: a Linux server the phone and the car computer can reach

EOF
        read -r -p "Type 1 or 2 and press Enter: " choice
        case "$choice" in
            1) role=car ;;
            2) role=server ;;
            *) echo "Please run it again and type 1 or 2." >&2; exit 2 ;;
        esac ;;
    *) echo "Unknown choice '$role'. Use: bash setup.sh car, bash setup.sh server, bash setup.sh modem, bash setup.sh doctor or bash setup.sh logs" >&2; exit 2 ;;
esac

exec bash "$HERE/deploy/setup-$role.sh" "$@"
