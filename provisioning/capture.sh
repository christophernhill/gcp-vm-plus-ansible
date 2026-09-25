#!/usr/bin/env bash
# capture.sh — read-only snapshot of a provisioned VM: what has been
# applied, added, started, and installed since provisioning. Prints a
# sectioned report to stdout and changes nothing on the machine.
#
# "Since provisioning" is anchored at the VM's first boot by default
# (the mtime of /etc/machine-id, which official GCP images generate
# then). Pass any `date -d`-parsable timestamp to override the anchor.
#
# Usage, from the machine that ran tofu apply (login = vm.ssh_user):
#   scp -r provisioning <ssh_user>@$(tofu -chdir=build output -raw public_ip):
#   ssh <ssh_user>@<public_ip> 'sudo bash provisioning/capture.sh' > vm-state.txt
#   ssh <ssh_user>@<public_ip> 'sudo bash provisioning/capture.sh "2026-09-23 12:00"'

# No -e on purpose: a report should keep going past sections that do
# not apply to this distro or image.
set -uo pipefail

export LC_ALL=C SYSTEMD_PAGER=

if [[ $EUID -ne 0 ]]; then
    echo "error: must run as root (try: sudo bash $0)" >&2
    exit 1
fi

if [[ $# -ge 1 ]]; then
    ANCHOR_EPOCH=$(date -d "$1" +%s) || exit 1
    ANCHOR_SOURCE="command line"
else
    ANCHOR_EPOCH=$(stat -c %Y /etc/machine-id)
    ANCHOR_SOURCE="first boot (mtime of /etc/machine-id)"
fi
ANCHOR=$(date -d "@${ANCHOR_EPOCH}" '+%Y-%m-%d %H:%M:%S')

section() { printf '\n===== %s =====\n' "$1"; }

echo "capture.sh report — generated $(date '+%Y-%m-%d %H:%M:%S %Z') on $(hostname)"
echo "showing changes since: ${ANCHOR} (${ANCHOR_SOURCE})"

section "system"
. /etc/os-release && echo "os:      ${PRETTY_NAME}"
echo "kernel:  $(uname -r)"
echo "booted:  $(uptime -s 2>/dev/null || echo unknown)"

section "accounts (uid >= 1000)"
while IFS=: read -r user _ uid _ _ home shell; do
    [[ $uid -ge 1000 && $user != nobody ]] || continue
    echo "${user}  uid=${uid}  shell=${shell}  groups: $(id -nG "$user" 2>/dev/null)"
    if [[ -f ${home}/.ssh/authorized_keys ]]; then
        ssh-keygen -lf "${home}/.ssh/authorized_keys" 2>/dev/null \
            | sed 's/^/    key: /'
    fi
done < /etc/passwd

section "sudoers drop-ins (/etc/sudoers.d)"
for f in /etc/sudoers.d/*; do
    [[ -f $f ]] || continue
    echo "--- ${f}"
    cat "$f"
done

section "packages installed since anchor"
if command -v rpm >/dev/null; then
    rpm -qa --qf '%{INSTALLTIME}\t%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' \
        | awk -F'\t' -v a="$ANCHOR_EPOCH" '$1 >= a' | sort -n \
        | while IFS=$'\t' read -r t pkg; do
              printf '%s  %s\n' "$(date -d "@${t}" '+%Y-%m-%d %H:%M')" "$pkg"
          done
    if command -v dnf >/dev/null; then
        echo "--- recent dnf transactions"
        dnf history list 2>/dev/null | head -12
    fi
elif command -v dpkg >/dev/null; then
    # Young VMs fit in the current log + one rotation; older .gz files
    # are ignored.
    cat /var/log/dpkg.log.1 /var/log/dpkg.log 2>/dev/null \
        | awk -v a="$ANCHOR" '$3 == "install" && ($1 " " $2) >= a {print $1, $2, $4, $6}'
else
    echo "no rpm or dpkg found"
fi

section "services enabled"
systemctl list-unit-files --type=service --state=enabled --no-legend 2>/dev/null

section "services running"
systemctl list-units --type=service --state=running --no-legend 2>/dev/null

section "systemd units added/edited since anchor (/etc/systemd/system)"
find /etc/systemd/system -newermt "$ANCHOR" \( -type f -o -type l \) 2>/dev/null | sort

section "timers and cron"
systemctl list-timers --all --no-legend 2>/dev/null
for spool in /var/spool/cron /var/spool/cron/crontabs; do
    [[ -d $spool ]] || continue
    for f in "$spool"/*; do
        [[ -f $f ]] || continue
        echo "--- user crontab: ${f##*/}"
        cat "$f"
    done
done

section "listening sockets"
ss -tulpn 2>/dev/null

section "network"
if command -v ip >/dev/null; then
    ip -brief address
    echo "--- policy routing rules"
    ip rule list
    echo "--- routes"
    ip route list
else
    echo "iproute2 not installed"
fi

section "local firewall"
if systemctl is-active --quiet firewalld 2>/dev/null; then
    firewall-cmd --list-all 2>/dev/null
elif command -v nft >/dev/null && [[ -n $(nft list ruleset 2>/dev/null) ]]; then
    nft list ruleset
else
    echo "none active (ingress filtering is done by the GCP VPC firewall)"
fi

section "files changed in /etc since anchor"
find /etc -xdev -type f -newermt "$ANCHOR" 2>/dev/null | sort

section "files added under /usr/local since anchor"
find /usr/local -xdev -type f -newermt "$ANCHOR" 2>/dev/null | sort

section "recent logins"
last -w -n 15 2>/dev/null

echo
echo "done: report is read-only; nothing on the VM was modified"
