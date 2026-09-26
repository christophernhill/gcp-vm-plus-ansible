#!/usr/bin/env bash
# capture.sh — read-only snapshot of a provisioned VM: what has been
# applied, added, started, and installed since provisioning, with
# enough detail to reconfigure a new VM the same way. Prints a
# sectioned report to stdout and changes nothing on the machine.
# Contents of changed config files are included comment-stripped;
# files whose names suggest secrets (keys, passwords) are listed but
# their contents withheld.
#
# "Since provisioning" is anchored, in order of preference, at: a
# `date -d`-parsable timestamp passed as $1; the mtime of
# /etc/google_instance_id (written at a GCP instance's first boot);
# the mtime of /var/lib/cloud/instance (cloud-init's per-instance
# directory, created at first boot — the AWS anchor); or the mtime of
# /etc/machine-id (which can predate first boot when it is baked into
# the image).
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
elif [[ -f /etc/google_instance_id ]]; then
    ANCHOR_EPOCH=$(stat -c %Y /etc/google_instance_id)
    ANCHOR_SOURCE="instance first boot (mtime of /etc/google_instance_id)"
elif [[ -d /var/lib/cloud/instance ]]; then
    # Cloud-init's per-instance directory (a symlink to
    # instances/<id>); created at first boot. The AWS anchor.
    ANCHOR_EPOCH=$(stat -c %Y /var/lib/cloud/instance/)
    ANCHOR_SOURCE="instance first boot (mtime of /var/lib/cloud/instance, cloud-init)"
else
    ANCHOR_EPOCH=$(stat -c %Y /etc/machine-id)
    ANCHOR_SOURCE="first boot (mtime of /etc/machine-id)"
fi
ANCHOR=$(date -d "@${ANCHOR_EPOCH}" '+%Y-%m-%d %H:%M:%S')

section() { printf '\n===== %s =====\n' "$1"; }

# Files whose contents must never be printed, by name.
SECRET_NAME_RE='pass|secret|token|shadow|private|credential|\.key$|_key$'
# And a second line of defense for individual lines inside files.
REDACT_SED='s/((password|passwd|psk|secret|token)[[:space:]]*[=:]).*/\1 [withheld]/I'

MAX_FILE_LINES=80

# show_file <path>: print a config file comment-stripped, redacted,
# capped at MAX_FILE_LINES lines.
show_file() {
    local f=$1 body lines
    echo "--- ${f}"
    if grep -Eiq "$SECRET_NAME_RE" <<<"$f"; then
        echo "    [contents withheld: name suggests secret material]"
        return
    fi
    if [[ ! -r $f ]]; then
        echo "    [unreadable]"
        return
    fi
    if ! grep -Iq . "$f" 2>/dev/null; then
        echo "    [binary or empty]"
        return
    fi
    body=$(grep -vE '^[[:space:]]*([#;]|$)' "$f" | sed -E "$REDACT_SED")
    lines=$(wc -l <<<"$body")
    head -n "$MAX_FILE_LINES" <<<"$body" | sed 's/^/    /'
    if (( lines > MAX_FILE_LINES )); then
        echo "    [... $((lines - MAX_FILE_LINES)) more non-comment lines]"
    fi
}

echo "capture.sh report — generated $(date '+%Y-%m-%d %H:%M:%S %Z') on $(hostname)"
echo "showing changes since: ${ANCHOR} (${ANCHOR_SOURCE})"

section "system"
. /etc/os-release && echo "os:      ${PRETTY_NAME}"
echo "kernel:  $(uname -r)"
echo "cmdline: $(cat /proc/cmdline 2>/dev/null)"
echo "selinux: $(getenforce 2>/dev/null || echo n/a)"
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
echo "--- sessions and lingering (loginctl)"
loginctl list-users --no-legend 2>/dev/null

section "sudoers drop-ins (/etc/sudoers.d)"
for f in /etc/sudoers.d/*; do
    [[ -f $f ]] || continue
    echo "--- ${f}"
    cat "$f"
done

section "packages"
if command -v rpm >/dev/null; then
    echo "--- installed since anchor"
    rpm -qa --qf '%{INSTALLTIME}\t%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' \
        | awk -F'\t' -v a="$ANCHOR_EPOCH" '$1 >= a' | sort -n \
        | while IFS=$'\t' read -r t pkg; do
              printf '%s  %s\n' "$(date -d "@${t}" '+%Y-%m-%d %H:%M')" "$pkg"
          done
    if command -v dnf >/dev/null; then
        echo "--- enabled repositories"
        dnf -C repolist --enabled 2>/dev/null
        echo "--- user-installed packages (replay with: dnf install ...)"
        dnf -C history userinstalled 2>/dev/null
        echo "--- all dnf transactions"
        dnf -C history list 2>/dev/null
    fi
elif command -v dpkg >/dev/null; then
    echo "--- installed since anchor"
    # Young VMs fit in the current log + one rotation; older .gz files
    # are ignored.
    cat /var/log/dpkg.log.1 /var/log/dpkg.log 2>/dev/null \
        | awk -v a="$ANCHOR" '$3 == "install" && ($1 " " $2) >= a {print $1, $2, $4, $6}'
    echo "--- apt sources"
    grep -rhE '^[[:space:]]*(deb |URIs:)' \
        /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null
    echo "--- manually installed packages (replay with: apt install ...)"
    apt-mark showmanual 2>/dev/null | sort
else
    echo "no rpm or dpkg found"
fi

section "services enabled"
systemctl list-unit-files --type=service --state=enabled --no-legend 2>/dev/null
echo "--- units whose state differs from the vendor preset"
systemctl list-unit-files --no-legend 2>/dev/null | awk '$3 != "" && $3 != "-" && $2 != $3'

section "services running"
systemctl list-units --type=service --state=running --no-legend 2>/dev/null

section "systemd units added/edited since anchor (/etc/systemd/system)"
find /etc/systemd/system -newermt "$ANCHOR" \( -type f -o -type l \) 2>/dev/null | sort
# Locally added/edited units and drop-ins are what a rebuild must
# recreate — symlinks above are just enablement, so print real files.
find /etc/systemd/system -newermt "$ANCHOR" -type f 2>/dev/null | sort \
    | while read -r f; do show_file "$f"; done

section "containers"
FOUND_CONTAINERS=
if command -v machinectl >/dev/null; then
    FOUND_CONTAINERS=1
    echo "--- machinectl list"
    machinectl list --no-legend 2>/dev/null
    echo "--- machinectl images"
    machinectl list-images --no-legend 2>/dev/null
fi
for f in /etc/systemd/nspawn/*.nspawn; do
    [[ -f $f ]] || continue
    FOUND_CONTAINERS=1
    show_file "$f"
done
if command -v docker >/dev/null; then
    FOUND_CONTAINERS=1
    echo "--- docker ps -a"
    docker ps -a 2>/dev/null
    echo "--- docker images"
    docker images 2>/dev/null
fi
if command -v podman >/dev/null; then
    FOUND_CONTAINERS=1
    echo "--- podman ps -a"
    podman ps -a 2>/dev/null
fi
if command -v lxc >/dev/null; then
    FOUND_CONTAINERS=1
    echo "--- lxc list"
    lxc list 2>/dev/null
fi
[[ -n $FOUND_CONTAINERS ]] || echo "no container runtime found"

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
    echo "default zone: $(firewall-cmd --get-default-zone 2>/dev/null)"
    echo "active zones: $(firewall-cmd --get-active-zones 2>/dev/null | tr '\n' ' ')"
    for zone in $(firewall-cmd --get-active-zones 2>/dev/null | awk 'NR % 2 == 1'); do
        echo "--- zone: ${zone}"
        firewall-cmd --list-all --zone="$zone" 2>/dev/null
    done
elif command -v nft >/dev/null && [[ -n $(nft list ruleset 2>/dev/null) ]]; then
    nft list ruleset
else
    echo "none active (ingress filtering is done by the GCP VPC firewall)"
fi

section "storage"
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS 2>/dev/null
echo "--- /etc/fstab (comments stripped)"
grep -vE '^[[:space:]]*(#|$)' /etc/fstab 2>/dev/null
echo "--- network/auto filesystems mounted"
findmnt -rn -t nfs,nfs4,cifs,autofs 2>/dev/null

# Generated, cache, identity, and account files: listed as a count
# only, and never shown in the contents section (accounts have their
# own section; hostname/resolv.conf are instance-specific).
NOISE_RE='^/etc/(pki/|selinux/targeted/|authselect/|udev/hwdb\.bin$|ld\.so\.cache$|machine-id$|google_instance_id$|adjtime$|hostname$|resolv\.conf$|ssh/moduli$|ssh/ssh_host|xml/catalog$|aliases\.cdb$|mail/|(passwd|group|shadow|gshadow|subuid|subgid)-?$)|\.(bin|cdb|cache)$'

section "files changed in /etc since anchor"
CHANGED_ETC=$(find /etc -xdev -type f -newermt "$ANCHOR" 2>/dev/null | sort)
grep -Ev "$NOISE_RE" <<<"$CHANGED_ETC"
NOISE_COUNT=$(grep -Ec "$NOISE_RE" <<<"$CHANGED_ETC")
(( NOISE_COUNT > 0 )) && echo "[+ ${NOISE_COUNT} generated/cache/identity files omitted]"

section "contents of changed config files (comments stripped, secrets withheld)"
grep -Ev "$NOISE_RE" <<<"$CHANGED_ETC" | grep -v '^/etc/systemd/system/' \
    | while read -r f; do [[ -n $f ]] && show_file "$f"; done

for dir in /usr/local /opt; do
    section "files added under ${dir} since anchor"
    ADDED=$(find "$dir" -xdev -type f -newermt "$ANCHOR" 2>/dev/null | sort)
    head -n 200 <<<"$ADDED"
    ADDED_COUNT=$(grep -c . <<<"$ADDED")
    (( ADDED_COUNT > 200 )) && echo "[+ $((ADDED_COUNT - 200)) more files]"
done

section "recent logins"
last -w -n 15 2>/dev/null

echo
echo "done: report is read-only; nothing on the VM was modified"
