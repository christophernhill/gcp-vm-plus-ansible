"""AWS provider: presets, RAM inference, AWS-only checks, template context.

Provider-hook error convention: raise ValueError for fatal problems (the
core turns it into a clean exit) and print to stderr for warnings.
"""

import re
import sys
from pathlib import Path

NAME = "aws"
TEMPLATE_SUBDIR = "aws"

REQUIRED_KEYS = [
    ("aws", "region"),
    ("aws", "availability_zone"),
]

# Placeholder for AMI owner IDs that have not been verified against the
# live AWS API yet (design doc section 9, item 2). validate() refuses any
# image that uses it, so an unverified preset cannot be rendered for
# deployment by accident. All current presets are verified (2026-09-26);
# keep this pattern for any future preset whose owner is unconfirmed.
UNVERIFIED_AMI_OWNER = "000000000000-unverified"

# vm.os presets: image dict + default ssh_user. The image dict is either
# {"ssm_parameter": ...} (Ubuntu, Debian, and Amazon Linux publish their
# latest AMI IDs in public SSM parameters) or {"ami_owner": ...,
# "ami_name_filter": ...} (Marketplace/community images). SSM paths,
# owner IDs, and name filters verified against us-east-1 on 2026-09-26,
# and every default ssh_user confirmed by booting the AMI (design doc
# §9). Note the AlmaLinux/CentOS names contain literal spaces. Rocky
# images carry Marketplace product codes: the first apply needs the
# product's subscription accepted in the AWS console (once per account),
# and the Rocky product does not support burstable (t2/t3) instance
# types. Alma, CentOS Stream, and Fedora are plain community AMIs (no
# product codes).
OS_PRESETS = {
    "ubuntu-24.04": (
        {"ssm_parameter": "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"},
        "ubuntu",
    ),
    "ubuntu-22.04": (
        {"ssm_parameter": "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id"},
        "ubuntu",
    ),
    "debian-13": (
        {"ssm_parameter": "/aws/service/debian/release/13/latest/amd64"},
        "admin",
    ),
    "debian-12": (
        {"ssm_parameter": "/aws/service/debian/release/12/latest/amd64"},
        "admin",
    ),
    "amazon-linux-2023": (
        {"ssm_parameter": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"},
        "ec2-user",
    ),
    "rocky-10": (
        {"ami_owner": "679593333241",
         "ami_name_filter": "Rocky-10-EC2-Base-*x86_64*"},
        "rocky",
    ),
    "rocky-9": (
        {"ami_owner": "679593333241",
         "ami_name_filter": "Rocky-9-EC2-Base-*x86_64*"},
        "rocky",
    ),
    "almalinux-10": (
        {"ami_owner": "764336703387",
         "ami_name_filter": "AlmaLinux OS 10.* x86_64"},
        "ec2-user",  # confirmed by boot test 2026-09-26
    ),
    "almalinux-9": (
        {"ami_owner": "764336703387",
         "ami_name_filter": "AlmaLinux OS 9.* x86_64"},
        "ec2-user",  # confirmed by boot test 2026-09-26 (10; 9 same vendor)
    ),
    "centos-stream-10": (
        {"ami_owner": "125523088429",
         "ami_name_filter": "CentOS Stream 10 x86_64*"},
        "ec2-user",  # confirmed by boot test 2026-09-26 (NOT "centos")
    ),
    "centos-stream-9": (
        {"ami_owner": "125523088429",
         "ami_name_filter": "CentOS Stream 9 x86_64*"},
        "ec2-user",  # confirmed by boot test 2026-09-26 (10; 9 same vendor)
    ),
    "fedora-44": (
        {"ami_owner": "125523088429",
         "ami_name_filter": "Fedora-Cloud-Base-AmazonEC2.x86_64-44-*"},
        "fedora",  # confirmed by boot test 2026-09-26
    ),
    "fedora-43": (
        {"ami_owner": "125523088429",
         "ami_name_filter": "Fedora-Cloud-Base-AmazonEC2.x86_64-43-*"},
        "fedora",  # confirmed by boot test 2026-09-26 (44; 43 same vendor)
    ),
}

# RAM inference: EC2 names encode family and a size word but not memory,
# so estimate from static tables (design doc section 4.1). Anything the
# tables cannot resolve returns None and the core warns instead of
# failing, same as an unparseable GCP machine type.
GB_PER_VCPU = {"m": 4, "t": 4, "r": 8, "c": 2}
VCPUS = {
    "large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16,
    "8xlarge": 32, "12xlarge": 48, "16xlarge": 64, "24xlarge": 96,
}
MACHINE_TYPE_RE = re.compile(r"^([a-z])[a-z0-9]*\.([a-z0-9]+)$")

# Cap on vm.external_ip_count. Verified 2026-09-26: this account's
# Elastic IP quota is 5 per region (the AWS default; design doc section
# 9, item 5). Every external address here is an EIP, including the
# primary's when ENIs are explicit.
MAX_EXTERNAL_IPS = 5

# Cap on vm.nic_count. ENI limits depend on the instance type (verified
# 2026-09-26: m5.4xlarge allows 8 ENIs and 30 IPv4 addresses per ENI —
# design doc section 9, item 3), so validate() only warns; this cap is
# just a sanity bound.
MAX_NICS = 8


def _core():
    """Import generate.py lazily to avoid a circular import at load time.

    The core owns the SSH-key merge and open_ports parsing; provider
    hooks call back into it for those. Importing inside the function is
    safe because by the time any hook runs, generate.py is fully loaded.
    """
    import generate
    return generate


def apply_os_preset(cfg: dict) -> None:
    """Expand vm.os into vm.image and a default vm.ssh_user."""
    vm = cfg.get("vm")
    if not isinstance(vm, dict) or "os" not in vm:
        return
    if vm["os"] not in OS_PRESETS:
        raise ValueError(
            f"unknown vm.os {vm['os']!r}; valid presets: "
            + ", ".join(sorted(OS_PRESETS))
        )
    if "image" in vm:
        raise ValueError(
            "vm.os and vm.image are mutually exclusive: drop one "
            "(use vm.image only for images without a preset)"
        )
    image, user = OS_PRESETS[vm["os"]]
    vm["image"] = dict(image)
    vm.setdefault("ssh_user", user)


def machine_ram_gb(machine_type: str) -> int | None:
    """RAM estimated from the instance-type name; None means cannot tell."""
    match = MACHINE_TYPE_RE.match(machine_type)
    if not match:
        return None
    family, size = match.group(1), match.group(2)
    if family not in GB_PER_VCPU or size not in VCPUS:
        return None
    return VCPUS[size] * GB_PER_VCPU[family]


def _valid_image_shape(image) -> bool:
    """vm.image is exactly one of: {ami_id}, {ssm_parameter}, or
    {ami_owner, ami_name_filter} — all values non-empty strings."""
    if not isinstance(image, dict):
        return False
    keys = set(image)
    if keys not in ({"ami_id"}, {"ssm_parameter"}, {"ami_owner", "ami_name_filter"}):
        return False
    return all(isinstance(image[k], str) and image[k] for k in keys)


def validate(cfg: dict) -> None:
    """AWS-only checks and advisories."""
    vm = cfg["vm"]
    image = vm["image"]
    if not _valid_image_shape(image):
        raise ValueError(
            "vm.image must set exactly one of: ami_id; ssm_parameter; or "
            "ami_owner + ami_name_filter (or use a vm.os preset)"
        )
    if image.get("ami_owner") == UNVERIFIED_AMI_OWNER:
        which = f"preset {vm['os']}" if "os" in vm else "this vm.image"
        raise ValueError(
            f"{which} needs verification (design doc §9): the AMI owner "
            "ID is a placeholder. Confirm the owner ID, name filter, and "
            "default login user with the AWS API, then update "
            "generator/providers/aws.py"
        )

    if vm["nic_count"] > 1:
        print(
            f"warning: vm.nic_count is {vm['nic_count']}; AWS ENI limits "
            f"depend on the instance type — verify {vm['machine_type']!r} "
            "supports that many network interfaces",
            file=sys.stderr,
        )

    # The merged keys travel inside an HCL heredoc (user_data); a key
    # containing ${ would be interpolated by OpenTofu and corrupt it.
    for key in _core().resolve_ssh_public_keys(vm):
        if "${" in key:
            raise ValueError(
                "SSH public keys must not contain '${' — they are "
                "embedded in an HCL heredoc that OpenTofu would interpolate"
            )


def _open_port_rules(cfg: dict) -> list[dict]:
    """Structured open_ports for the template: from/to as integers."""
    by_proto = _core().parse_open_ports(cfg["network"])
    rules = []
    for proto in ("tcp", "udp"):
        for port_str in by_proto[proto]:
            low, _, high = port_str.partition("-")
            rules.append({
                "proto": proto,
                "from": int(low),
                "to": int(high) if high else int(low),
            })
    return rules


def render_context(cfg: dict) -> dict:
    """Provider-specific additions to the Jinja context.

    user_data is a bash cloud-init script that installs the merged
    authorized_keys for vm.ssh_user (creating the user if the AMI has no
    such account) and, when extra external IPs or NICs are configured,
    installs a per-boot systemd unit running templates/aws/policy-routing.sh.
    Embedded via an HCL heredoc, so: $var only, never dollar-brace.
    """
    vm = cfg["vm"]
    keys = _core().resolve_ssh_public_keys(vm)

    lines = [
        "#!/usr/bin/env bash",
        "# cloud-init user-data: install the merged authorized_keys for the",
        "# base login, then (when extra Elastic IPs or ENIs are configured)",
        "# install the per-boot policy-routing unit. Use $var, never",
        "# dollar-brace expansion, or OpenTofu would try to interpolate it",
        "# in the heredoc.",
        "set -euo pipefail",
        "",
        f"user={vm['ssh_user']}",
        'if ! getent passwd "$user" >/dev/null; then',
        '    useradd -m -s /bin/bash "$user"',
        "fi",
        'home=$(getent passwd "$user" | cut -d: -f6)',
        'install -d -m 700 -o "$user" -g "$user" "$home/.ssh"',
        "cat > \"$home/.ssh/authorized_keys\" <<'KEYS'",
        *keys,
        "KEYS",
        'chmod 600 "$home/.ssh/authorized_keys"',
        'chown "$user:$user" "$home/.ssh/authorized_keys"',
    ]

    if vm["nic_count"] > 1 or vm["external_ip_count"] > 1:
        script_path = (
            Path(__file__).resolve().parent.parent
            / "templates" / TEMPLATE_SUBDIR / "policy-routing.sh"
        )
        script = script_path.read_text().rstrip()
        lines += [
            "",
            "cat > /usr/local/sbin/policy-routing.sh <<'SCRIPT'",
            *script.splitlines(),
            "SCRIPT",
            "chmod 755 /usr/local/sbin/policy-routing.sh",
            "",
            "cat > /etc/systemd/system/policy-routing.service <<'UNIT'",
            "[Unit]",
            "Description=Secondary-IP and source-based policy routing for extra EIPs/ENIs",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
            "ExecStart=/usr/local/sbin/policy-routing.sh",
            "RemainAfterExit=yes",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "UNIT",
            "systemctl daemon-reload",
            "systemctl enable --now policy-routing.service",
        ]

    # Single-line HCL expressions that depend on the image shape and the
    # NIC/EIP wiring case; supplying them here keeps the template free of
    # fragile inline conditionals.
    image = vm["image"]
    if "ssm_parameter" in image:
        ami_expression = "data.aws_ssm_parameter.ami.value"
    elif "ami_owner" in image:
        ami_expression = "data.aws_ami.ami.id"
    else:
        ami_expression = f'"{image["ami_id"]}"'

    if vm["nic_count"] == 1 and vm["external_ip_count"] == 1:
        public_ip_expr = "aws_instance.vm.public_ip"
        public_ips_expr = "[aws_instance.vm.public_ip]"
    else:
        # Explicit ENIs get no auto-assigned public IP, so every public
        # address is an Elastic IP. (aws_eip's "address" attribute is
        # null for VPC-domain EIPs; "public_ip" is the one to use.)
        public_ip_expr = "aws_eip.primary[0].public_ip"
        if vm["nic_count"] > 1:
            public_ips_expr = (
                "concat(aws_eip.primary[*].public_ip, "
                "aws_eip.secondary[*].public_ip)"
            )
        else:
            public_ips_expr = "aws_eip.primary[*].public_ip"

    return {
        "user_data": "\n".join(lines),
        "open_port_rules": _open_port_rules(cfg),
        "ami_expression": ami_expression,
        "public_ip_expr": public_ip_expr,
        "public_ips_expr": public_ips_expr,
        # The full quoted HCL string for the ssh_command output; built
        # here because the literal "${" of HCL interpolation collides
        # with Jinja's "{{" when written inline in the template.
        "ssh_command_expr": f'"ssh {vm["ssh_user"]}@${{{public_ip_expr}}}"',
    }
