#!/usr/bin/env bash
# Secondary-address setup and source-based policy routing for EC2
# instances with extra Elastic IPs or multiple ENIs. Nothing on a stock
# AMI configures secondary private addresses inside the guest, so their
# Elastic IPs would never answer; and without per-interface routing,
# replies to traffic arriving on a secondary ENI leave via the default
# route on the primary interface with the wrong source address and are
# dropped. Installed as a per-boot systemd unit by the user-data script
# from generator/providers/aws.py when vm.nic_count > 1 or
# vm.external_ip_count > 1. Use $var, never dollar-brace expansion, or
# OpenTofu would try to interpolate it in the heredoc.
set -euo pipefail

# IMDSv2 wants a session token first; interfaces are indexed by MAC
# address, and the gateway is not exposed (by convention it is the
# first host address of the subnet's CIDR block).
TOK=$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
MD=http://169.254.169.254/latest/meta-data/network/interfaces/macs

imds() { curl -sf -H "X-aws-ec2-metadata-token: $TOK" "$MD/$1"; }

for macpath in $(imds ""); do
    mac=$(echo "$macpath" | tr -d /)
    devnum=$(imds "$mac/device-number")
    ip4s=$(imds "$mac/local-ipv4s")
    cidr=$(imds "$mac/subnet-ipv4-cidr-block")
    gw=$(echo "$cidr" | awk -F'[./]' '{print $1"."$2"."$3"."($4+1)}')
    prefix=$(echo "$cidr" | cut -d/ -f2)
    dev=""
    for d in /sys/class/net/*; do
        if [ "$(cat "$d/address" 2>/dev/null)" = "$mac" ]; then
            dev=$(basename "$d")
        fi
    done
    if [ -z "$dev" ]; then
        continue
    fi
    ip link set "$dev" up
    # Configure every private address IMDS reports for this interface.
    # The primary is usually present already (DHCP); the secondary
    # addresses behind the extra Elastic IPs never are.
    for ip4 in $ip4s; do
        if ! ip -4 addr show dev "$dev" | grep -qw "$ip4"; then
            ip addr add "$ip4/$prefix" dev "$dev"
        fi
    done
    # Non-primary interfaces get their own routing table and a source
    # rule, so replies leave through the interface they arrived on.
    if [ "$devnum" != "0" ]; then
        ip4=$(echo "$ip4s" | head -n1)
        i=$devnum
        table=$((100 + i))
        ip route replace default via "$gw" dev "$dev" onlink table "$table"
        ip rule del from "$ip4/32" 2>/dev/null || true
        ip rule add from "$ip4/32" lookup "$table" priority $((10000 + i))
    fi
done
