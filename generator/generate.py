#!/usr/bin/env python3
"""Render OpenTofu HCL for a Rocky Linux 10 GCP VM from a YAML config.

Usage:
    python3 generator/generate.py [--config config/vm.yaml] [--out build]
"""

import argparse
import re
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# GB of RAM per vCPU for the common predefined machine-type families.
GB_PER_VCPU = {"standard": 4, "highmem": 8, "highcpu": 1}
MACHINE_TYPE_RE = re.compile(r"^[a-z][a-z0-9]*-(standard|highmem|highcpu)-(\d+)$")

# Cap on vm.external_ip_count (NIC IP + protocol-forwarded extras).
MAX_EXTERNAL_IPS = 8

# Cap on vm.nic_count. GCP allows up to 10 vNICs (fewer on small
# machine types: 2-10 vCPUs get one vNIC per vCPU).
MAX_NICS = 8

# OpenSSH public-key line: key type, base64 blob, optional comment.
PUBLIC_KEY_RE = re.compile(r"^(sk-)?(ssh|ecdsa)-[a-z0-9@.-]+\s+\S+", re.IGNORECASE)

REQUIRED_KEYS = [
    ("gcp", "project_id"),
    ("gcp", "region"),
    ("gcp", "zone"),
    ("network", "name"),
    ("network", "ssh_source_ranges"),
    ("vm", "name"),
    ("vm", "machine_type"),
    ("vm", "boot_disk_gb"),
    ("vm", "boot_disk_type"),
    ("vm", "image"),
    ("vm", "ssh_user"),
]


def fail(msg: str) -> None:
    sys.exit(f"error: {msg}")


def load_config(path: Path) -> dict:
    try:
        cfg = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        fail(f"config file not found: {path}")
    except yaml.YAMLError as exc:
        fail(f"could not parse {path}: {exc}")
    if not isinstance(cfg, dict):
        fail(f"{path} must contain a YAML mapping")
    return cfg


def validate(cfg: dict) -> None:
    for section, key in REQUIRED_KEYS:
        if key not in cfg.get(section, {}):
            fail(f"missing required setting: {section}.{key}")

    vm = cfg["vm"]
    min_gb = vm.get("min_memory_gb", 64)
    machine_type = vm["machine_type"]
    match = MACHINE_TYPE_RE.match(machine_type)
    if match:
        family, vcpus = match.group(1), int(match.group(2))
        ram_gb = vcpus * GB_PER_VCPU[family]
        if ram_gb < min_gb:
            fail(
                f"machine_type {machine_type} has {ram_gb} GB RAM, "
                f"below the required {min_gb} GB"
            )
    else:
        print(
            f"warning: cannot infer RAM for machine_type {machine_type!r}; "
            f"verify it has at least {min_gb} GB",
            file=sys.stderr,
        )

    if vm["boot_disk_gb"] < 250:
        fail(f"boot_disk_gb is {vm['boot_disk_gb']}, below the required 250")

    ip_count = vm.setdefault("external_ip_count", 1)
    if (
        isinstance(ip_count, bool)
        or not isinstance(ip_count, int)
        or not 1 <= ip_count <= MAX_EXTERNAL_IPS
    ):
        fail(
            f"vm.external_ip_count is {ip_count!r}; "
            f"must be an integer between 1 and {MAX_EXTERNAL_IPS}"
        )

    nic_count = vm.setdefault("nic_count", 1)
    if (
        isinstance(nic_count, bool)
        or not isinstance(nic_count, int)
        or not 1 <= nic_count <= MAX_NICS
    ):
        fail(
            f"vm.nic_count is {nic_count!r}; "
            f"must be an integer between 1 and {MAX_NICS}"
        )
    if match and nic_count > int(match.group(2)):
        fail(
            f"vm.nic_count is {nic_count}, but {machine_type} has only "
            f"{match.group(2)} vCPUs (GCP allows at most one vNIC per vCPU)"
        )

    if cfg["gcp"]["project_id"] == "my-gcp-project":
        print(
            "warning: gcp.project_id is still the placeholder 'my-gcp-project'",
            file=sys.stderr,
        )

    if not cfg["network"]["ssh_source_ranges"]:
        fail("network.ssh_source_ranges must list at least one CIDR")


def key_file_path(setting: str) -> Path:
    """Expand ~ and resolve relative paths against the repo root."""
    path = Path(setting).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def resolve_ssh_public_keys(vm: dict) -> list[str]:
    """Merge keys from all configured sources, in order, deduplicated."""
    keys: list[str] = []

    inline = vm.get("ssh_public_key")
    if inline:
        keys.append(inline.strip())

    for setting, multi in [("ssh_public_key_file", False),
                           ("ssh_public_keys_file", True)]:
        if not vm.get(setting):
            continue
        path = key_file_path(vm[setting])
        if not path.is_file():
            fail(f"vm.{setting}: file not found: {path}")
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            keys.append(line)
            if not multi:
                break

    for key in keys:
        if not PUBLIC_KEY_RE.match(key):
            fail(f"this does not look like an OpenSSH public key: {key!r}")

    deduped = list(dict.fromkeys(keys))
    if not deduped:
        fail(
            "no SSH public keys found: set vm.ssh_public_key, "
            "vm.ssh_public_key_file, or add keys to vm.ssh_public_keys_file"
        )
    return deduped


def render(cfg: dict, out_dir: Path) -> Path:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    hcl = env.get_template("main.tf.j2").render(
        **cfg,
        ssh_public_keys=resolve_ssh_public_keys(cfg["vm"]),
        startup_script=(TEMPLATE_DIR / "policy-routing.sh").read_text().rstrip(),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "main.tf"
    out_file.write_text(hcl)
    return out_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "config" / "vm.yaml"
    )
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "build")
    args = parser.parse_args()

    cfg = load_config(args.config)
    validate(cfg)
    out_file = render(cfg, args.out)
    print(f"wrote {out_file}")
    print(f"next: tofu -chdir={args.out} init && tofu -chdir={args.out} apply")


if __name__ == "__main__":
    main()
