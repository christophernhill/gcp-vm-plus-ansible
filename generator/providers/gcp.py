"""GCP provider: presets, RAM inference, GCP-only checks, template context.

Provider-hook error convention: raise ValueError for fatal problems (the
core turns it into a clean exit) and print to stderr for warnings.
"""

import re
import sys
from pathlib import Path

NAME = "gcp"
TEMPLATE_SUBDIR = "gcp"

REQUIRED_KEYS = [
    ("gcp", "project_id"),
    ("gcp", "region"),
    ("gcp", "zone"),
]

# vm.os presets: image project, image family, default ssh_user. For any
# GCP public image not listed here, set vm.image + vm.ssh_user instead.
OS_PRESETS = {
    "rocky-10":         ("rocky-linux-cloud", "rocky-linux-10", "rocky"),
    "rocky-9":          ("rocky-linux-cloud", "rocky-linux-9", "rocky"),
    "almalinux-10":     ("almalinux-cloud", "almalinux-10", "almalinux"),
    "almalinux-9":      ("almalinux-cloud", "almalinux-9", "almalinux"),
    "centos-stream-10": ("centos-cloud", "centos-stream-10", "centos"),
    "centos-stream-9":  ("centos-cloud", "centos-stream-9", "centos"),
    "fedora-44":        ("fedora-cloud", "fedora-cloud-44-x86-64", "fedora"),
    "fedora-43":        ("fedora-cloud", "fedora-cloud-43-x86-64", "fedora"),
    "ubuntu-24.04":     ("ubuntu-os-cloud", "ubuntu-2404-lts-amd64", "ubuntu"),
    "ubuntu-22.04":     ("ubuntu-os-cloud", "ubuntu-2204-lts", "ubuntu"),
    "debian-13":        ("debian-cloud", "debian-13", "debian"),
    "debian-12":        ("debian-cloud", "debian-12", "debian"),
}

# GB of RAM per vCPU for the common predefined machine-type families.
GB_PER_VCPU = {"standard": 4, "highmem": 8, "highcpu": 1}
MACHINE_TYPE_RE = re.compile(r"^[a-z][a-z0-9]*-(standard|highmem|highcpu)-(\d+)$")

# Cap on vm.external_ip_count (NIC IP + protocol-forwarded extras).
MAX_EXTERNAL_IPS = 8

# Cap on vm.nic_count. GCP allows up to 10 vNICs (fewer on small
# machine types: 2-10 vCPUs get one vNIC per vCPU).
MAX_NICS = 8


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
    project, family, user = OS_PRESETS[vm["os"]]
    vm["image"] = {"project": project, "family": family}
    vm.setdefault("ssh_user", user)


def machine_ram_gb(machine_type: str) -> int | None:
    """RAM inferred from the machine-type name; None means cannot tell."""
    match = MACHINE_TYPE_RE.match(machine_type)
    if not match:
        return None
    family, vcpus = match.group(1), int(match.group(2))
    return vcpus * GB_PER_VCPU[family]


def validate(cfg: dict) -> None:
    """GCP-only checks: image shape, nic_count <= vCPUs, placeholder warning."""
    vm = cfg["vm"]
    image = vm["image"]
    if not isinstance(image, dict) or not all(
        isinstance(image.get(k), str) and image[k] for k in ("project", "family")
    ):
        raise ValueError(
            "vm.image must set both project and family (or use a vm.os preset)"
        )

    match = MACHINE_TYPE_RE.match(vm["machine_type"])
    if match and vm["nic_count"] > int(match.group(2)):
        raise ValueError(
            f"vm.nic_count is {vm['nic_count']}, but {vm['machine_type']} has only "
            f"{match.group(2)} vCPUs (GCP allows at most one vNIC per vCPU)"
        )

    if cfg["gcp"]["project_id"] == "my-gcp-project":
        print(
            "warning: gcp.project_id is still the placeholder 'my-gcp-project'",
            file=sys.stderr,
        )


def render_context(cfg: dict) -> dict:
    """Provider-specific additions to the Jinja context."""
    template_dir = Path(__file__).resolve().parent.parent / "templates" / TEMPLATE_SUBDIR
    return {"startup_script": (template_dir / "policy-routing.sh").read_text().rstrip()}
