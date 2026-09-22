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

    if cfg["gcp"]["project_id"] == "my-gcp-project":
        print(
            "warning: gcp.project_id is still the placeholder 'my-gcp-project'",
            file=sys.stderr,
        )

    if not cfg["network"]["ssh_source_ranges"]:
        fail("network.ssh_source_ranges must list at least one CIDR")


def resolve_ssh_public_key(vm: dict) -> str:
    key = vm.get("ssh_public_key")
    if key:
        return key.strip()
    key_file = vm.get("ssh_public_key_file")
    if not key_file:
        fail("set vm.ssh_public_key or vm.ssh_public_key_file")
    path = Path(key_file).expanduser()
    if not path.is_file():
        fail(f"ssh public key file not found: {path}")
    return path.read_text().strip()


def render(cfg: dict, out_dir: Path) -> Path:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    hcl = env.get_template("main.tf.j2").render(
        **cfg, ssh_public_key=resolve_ssh_public_key(cfg["vm"])
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
