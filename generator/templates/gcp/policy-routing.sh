#!/usr/bin/env bash
# Source-based policy routing for multi-NIC GCE VMs. Without this,
# replies to traffic arriving on a secondary NIC leave via the default
# route on nic0 with the wrong source address and are dropped, so the
# secondary NICs' external IPs would be unreachable from outside.
# Injected as startup-script metadata by generator/generate.py when
# vm.nic_count > 1; runs on every boot. Use $var, never dollar-brace
# expansion, or OpenTofu would try to interpolate it in the heredoc.
set -euo pipefail

MD=http://169.254.169.254/computeMetadata/v1/instance/network-interfaces
HDR="Metadata-Flavor: Google"

i=0
while curl -sf -H "$HDR" "$MD/$i/mac" >/dev/null; do
    if [ "$i" -gt 0 ]; then
        mac=$(curl -sf -H "$HDR" "$MD/$i/mac")
        ip4=$(curl -sf -H "$HDR" "$MD/$i/ip")
        gw=$(curl -sf -H "$HDR" "$MD/$i/gateway")
        dev=""
        for d in /sys/class/net/*; do
            if [ "$(cat "$d/address" 2>/dev/null)" = "$mac" ]; then
                dev=$(basename "$d")
            fi
        done
        if [ -n "$dev" ]; then
            table=$((100 + i))
            ip route replace default via "$gw" dev "$dev" onlink table "$table"
            ip rule del from "$ip4/32" 2>/dev/null || true
            ip rule add from "$ip4/32" lookup "$table" priority $((10000 + i))
        fi
    fi
    i=$((i + 1))
done
