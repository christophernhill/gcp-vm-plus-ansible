#!/usr/bin/env bash
# setup0.sh — initial account provisioning for the Rocky 10 VM.
#
# Creates the admin accounts listed in USERS with:
#   - sudo privileges (wheel group + passwordless sudoers drop-in)
#   - SSH-key-only access: the password is locked, so until an admin
#     sets one, the matching key in keys/<username>.pub is the only
#     way in
#
# Expects one public key per user next to this script: keys/<username>.pub
#
# Usage, from the machine that ran tofu apply:
#   scp -r provisioning rocky@$(tofu -chdir=build output -raw public_ip):
#   ssh rocky@<public_ip> 'sudo bash provisioning/setup0.sh'
#
# Idempotent: safe to re-run; authorized_keys is overwritten from keys/.

set -euo pipefail

USERS=(lincolnb tloizou)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY_DIR="${SCRIPT_DIR}/keys"

if [[ $EUID -ne 0 ]]; then
    echo "error: must run as root (try: sudo bash $0)" >&2
    exit 1
fi

# Verify every key is present before changing anything.
for user in "${USERS[@]}"; do
    if [[ ! -s "${KEY_DIR}/${user}.pub" ]]; then
        echo "error: missing or empty public key: ${KEY_DIR}/${user}.pub" >&2
        exit 1
    fi
done

for user in "${USERS[@]}"; do
    if id "$user" &>/dev/null; then
        echo "user ${user} already exists; updating"
    else
        useradd -m -s /bin/bash "$user"
        echo "created user ${user}"
    fi

    usermod -aG wheel "$user"

    # Lock the password: key auth still works (sshd uses PAM on Rocky),
    # but password login is impossible until an admin sets one.
    passwd -l "$user" >/dev/null

    home="$(getent passwd "$user" | cut -d: -f6)"
    install -d -m 700 -o "$user" -g "$user" "${home}/.ssh"
    install -m 600 -o "$user" -g "$user" \
        "${KEY_DIR}/${user}.pub" "${home}/.ssh/authorized_keys"
    restorecon -R "${home}/.ssh" 2>/dev/null || true
done

# The accounts have locked passwords, so wheel's default password-prompting
# sudo would be unusable — grant NOPASSWD explicitly.
SUDOERS_FILE=/etc/sudoers.d/90-setup0-admins
for user in "${USERS[@]}"; do
    echo "${user} ALL=(ALL) NOPASSWD:ALL"
done > "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE"

echo "done: ${USERS[*]} have sudo and SSH-key-only access"
