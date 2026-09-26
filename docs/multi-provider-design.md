# Multi-provider design: AWS as an alternate provider

**Status: design only.** Nothing in this document is implemented. The
generator today targets GCP exclusively, and the deployed GCP VM must keep
working untouched throughout any implementation of this design.

The back-compat contract, stated up front:

- A config with no `provider:` key is a GCP config. The deployed VM's
  local `config/vm.yaml` must keep rendering **byte-identical**
  `build/main.tf` after every phase of this design (verified with `diff`
  and a `tofu plan` no-op).
- All existing commands, file locations, and workflows stay valid verbatim.
  AWS is additive.

## 1. Goals and non-goals

Goals:

- Let the same YAML-driven generator deploy the same *shape* of VM
  (guardrailed RAM/disk, own network, default-deny + SSH + `open_ports`,
  multi-key SSH, extra external IPs, multi-NIC) on **AWS or GCP**.
- Common functions live in one core location; provider-specific code,
  templates, and config sections are cleanly modularized.
- Per-provider documentation of the different use modes.

Non-goals:

- No remote OpenTofu state; state stays local, one dir per deployment.
- No move to tofu variables — YAML remains the parameterization layer
  (existing design decision in AGENTS.md).
- No single config that deploys to both providers at once; one config file
  = one provider = one deployment.
- No renaming of the repository or of existing files that would disturb
  the deployed VM's workflow.

## 2. Current architecture inventory

What the code does today, split by portability. (Function names, not line
numbers, so this survives drift — see `generator/generate.py`.)

Provider-neutral (the future **core**):

| Element | Role |
|---|---|
| `load_config`, `fail`, `example_hint` | YAML load, error helper, "copy the example" hint |
| `resolve_ssh_public_keys`, `PUBLIC_KEY_RE`, `key_file_path` | merge of the three SSH-key sources (`ssh_public_key` inline, `ssh_public_key_file`, `ssh_public_keys_file`), dedupe, syntax check |
| `parse_open_ports`, `OPEN_PORT_RE` | `open_ports` grammar: port, `low-high`, optional `tcp:`/`udp:` prefix |
| `boot_disk_gb >= 250` floor, `min_memory_gb` (default 64) | guardrails; the RAM *value* check is neutral, only the RAM *inference* is provider-specific |
| `external_ip_count` / `nic_count` integer-shape checks | bounds shape is neutral; the bounds themselves have provider rationale |
| `render`, `main` | Jinja env (`StrictUndefined`), `--config`/`--out` CLI, output messages |

GCP-specific (the future **`providers/gcp.py`** and **`templates/gcp/`**):

| Element | Why it is GCP-bound |
|---|---|
| `OS_PRESETS` | values are GCP image project/family pairs |
| `MACHINE_TYPE_RE`, `GB_PER_VCPU` | parses GCP machine-type names (`<fam>-standard\|highmem\|highcpu-<n>`) |
| `REQUIRED_KEYS` entries `gcp.project_id/region/zone`, `vm.image{project,family}` | GCP config shape |
| `apply_os_preset` | expands to GCP image refs |
| `MAX_EXTERNAL_IPS`/`MAX_NICS` = 8 | GCP forwarding-rule / vNIC-per-vCPU rationale |
| nic_count ≤ vCPUs check, `my-gcp-project` placeholder warning | GCP rules |
| `generator/templates/main.tf.j2` | every rendered resource is `google_*` |
| `generator/templates/policy-routing.sh` | probes the GCP metadata server (`Metadata-Flavor: Google`) |

`provisioning/setup0.sh` is already cloud-agnostic. `provisioning/capture.sh`
is nearly so (its first-boot anchor prefers `/etc/google_instance_id`; see
§9).

## 3. Target architecture

### 3.1 Repository tree, before and after

```
BEFORE                                  AFTER
generator/                              generator/
├── generate.py      (everything)       ├── generate.py      (core + CLI + provider registry)
└── templates/                          ├── providers/
    ├── main.tf.j2   (GCP)              │   ├── __init__.py
    └── policy-routing.sh (GCP)         │   ├── gcp.py       (presets, RAM parse, checks)
                                        │   └── aws.py
                                        └── templates/
config/examples/                            ├── gcp/
├── vm.yaml          (GCP)                  │   ├── main.tf.j2          (moved, unchanged)
└── base_authorized_keys                    │   └── policy-routing.sh   (moved, unchanged)
                                            └── aws/
build/               (deployed GCP VM)          ├── main.tf.j2
                                                ├── policy-routing.sh   (IMDSv2 variant)
                                                └── user-data.sh.j2     (keys + secondary IPs)
                                        config/examples/
                                        ├── vm.yaml          (GCP, gains commented `provider: gcp`)
                                        ├── vm-aws.yaml      (new)
                                        └── base_authorized_keys
                                        build/               (deployed GCP VM — untouched)
                                        build-aws/           (convention for AWS deploys)
```

### 3.2 The provider interface

Each provider is a plain Python module with a fixed, duck-typed surface —
no classes or ABCs; with exactly two implementations and a ~300-line tool,
a module-per-provider plus a literal registry is the proportionate design.
`generate.py` keeps all neutral logic and dispatches:

```python
# generate.py (sketch of the changed part)
from providers import gcp, aws          # generate.py's dir is on sys.path

PROVIDERS = {"gcp": gcp, "aws": aws}

def get_provider(cfg: dict):
    name = cfg.get("provider", "gcp")          # absent = gcp: back-compat
    if name not in PROVIDERS:
        fail(f"unknown provider {name!r}; valid: " + ", ".join(sorted(PROVIDERS)))
    return PROVIDERS[name]
```

The module surface every provider must export
(`providers/gcp.py` sketch — bodies are today's code, relocated):

```python
NAME = "gcp"
TEMPLATE_SUBDIR = "gcp"                 # generator/templates/gcp/

# Added to the neutral REQUIRED_KEYS (network.*, vm.name/machine_type/
# boot_disk_gb/ssh_user stay in core):
REQUIRED_KEYS = [
    ("gcp", "project_id"), ("gcp", "region"), ("gcp", "zone"),
    ("vm", "image"), ("vm", "boot_disk_type"),
]

# Same preset NAMES on every provider; provider-shaped values.
OS_PRESETS = {
    "rocky-10": ("rocky-linux-cloud", "rocky-linux-10", "rocky"),
    # ... (unchanged table)
}

MAX_EXTERNAL_IPS = 8    # NIC IP + protocol-forwarded extras
MAX_NICS = 8            # GCP: at most one vNIC per vCPU, 10 vNIC cap

def apply_os_preset(cfg) -> None: ...   # vm.os -> vm.image + default ssh_user

def machine_ram_gb(machine_type: str) -> int | None:
    """RAM inferred from the name; None = core prints the warn-only path."""
    # MACHINE_TYPE_RE / GB_PER_VCPU move here

def validate(cfg) -> None: ...
    # GCP extras: image{project,family} shape, nic_count <= vCPUs,
    # 'my-gcp-project' placeholder warning

def render_context(cfg) -> dict:
    """Provider-specific Jinja context additions."""
    # GCP: {"startup_script": <policy-routing.sh text>}
```

Core `validate()` keeps ownership of everything neutral — the disk floor,
the `min_memory_gb` comparison (calling `provider.machine_ram_gb()` for the
inference), count bounds (against `provider.MAX_*`), `ssh_source_ranges`,
`parse_open_ports` — **so no provider can skip a guardrail**. `render()`
loads `templates/{provider.TEMPLATE_SUBDIR}/main.tf.j2` and merges
`provider.render_context(cfg)` into the existing neutral context
(`**cfg`, `ssh_public_keys`, `open_ports`).

`providers/aws.py` exports the same names:
`REQUIRED_KEYS = [("aws", "region"), ("aws", "availability_zone"), ("vm", "image"), ("vm", "boot_disk_type")]`,
an AWS `OS_PRESETS` (§5), `machine_ram_gb` backed by static tables (§5),
`MAX_EXTERNAL_IPS`/`MAX_NICS` with AWS rationale, `validate` with AWS
placeholder warnings and advisories, and `render_context` returning the
cloud-init `user_data` text.

### 3.3 Config schema

One new optional top-level key selects the provider; each provider owns a
sibling section; everything else keeps its current name:

- `provider: gcp | aws` — **absent means `gcp`** (hard back-compat rule).
- `gcp:` — `project_id`, `region`, `zone` (unchanged).
- `aws:` — `region`, `availability_zone`, optional `profile` (rendered
  into the provider block only when set), optional `vpc_cidr` (default
  `10.10.0.0/16`; subnets carved as `10.10.<i>.0/24`, matching the GCP
  multi-NIC layout).

Design principle — **neutral key, provider vocabulary**: keys like
`vm.machine_type` and `vm.boot_disk_type` keep one name everywhere, but
their *values* speak the selected provider's language
(`n2-standard-16`/`pd-balanced` vs `m5.4xlarge`/`gp3`). The generator
validates values through the selected provider module.

Committed examples stay flat files in `config/examples/`:
`vm.yaml` remains the GCP example (gaining only a commented
`# provider: gcp` line), and `vm-aws.yaml` is added. The gitignore
mechanics (`config/*` ignored except `examples/`) and `example_hint()`
(matches by filename) work unchanged: the AWS flow is
`cp config/examples/vm-aws.yaml config/vm-aws.yaml`.

The full worked AWS example (this is the file P2 would commit):

```yaml
# Settings for a VM on AWS.
#
# This is the committed example. The generator reads the local copy,
# which is gitignored:
#     cp config/examples/vm-aws.yaml config/vm-aws.yaml
# Edit the copy, then run:
#     python3 generator/generate.py --config config/vm-aws.yaml --out build-aws

provider: aws

aws:
  region: us-east-1
  availability_zone: us-east-1a
  # profile: my-sso-profile      # optional; omit to use env/default chain
  # vpc_cidr: 10.10.0.0/16       # subnets are carved as 10.10.<i>.0/24

network:
  name: rocky10-net
  # CIDRs allowed to reach SSH. The security group denies everything
  # else inbound, so SSH and the open_ports list below are the only
  # ways in.
  ssh_source_ranges:
    - 0.0.0.0/0
  # Extra ports open to the public internet, same grammar as on GCP:
  # port, "low-high" range, or tcp:/udp: prefix (plain entries are TCP).
  open_ports: []

vm:
  name: rocky10-vm-aws
  # m5.4xlarge = 16 vCPU / 64 GB RAM. The generator refuses instance
  # types it can determine to have less than min_memory_gb.
  machine_type: m5.4xlarge
  min_memory_gb: 64
  boot_disk_gb: 250
  boot_disk_type: gp3               # gp3 | gp2 | io1 | io2
  # Same semantics as GCP but different mechanics (see the design doc):
  # extras are secondary private IPs with an Elastic IP each.
  external_ip_count: 1
  # Extra ENIs; limits depend on instance type (advisory, not enforced).
  nic_count: 1
  # Same preset names as GCP; values resolve to AMI lookups.
  os: rocky-10
  # For any other AMI, delete vm.os and set vm.image instead:
  # image:
  #   ami_id: ami-0123456789abcdef0
  # ssh_user: rocky
  ssh_public_keys_file: config/base_authorized_keys
  # ssh_public_key_file: ~/.ssh/id_ed25519.pub
  # ssh_public_key: "ssh-ed25519 AAAA... user@host"
```

### 3.4 Templates: per-provider whole files

Templates split into `templates/gcp/` and `templates/aws/` as **complete,
independent files** — no shared Jinja base template or macro layer. The
rationale: every rendered resource differs between clouds; the only Jinja
the current template shares is trivial loops; a macro layer would obscure
per-provider review and complicate `StrictUndefined` debugging. Logic that
genuinely is shared (port parsing, key merging, guardrails) already lives
in Python and config, which is where sharing belongs in this design.

Cross-provider invariant carried over from GCP: any script injected into
HCL via heredoc (`startup-script`, `user_data`) must never contain
`${...}` — OpenTofu would interpolate it. Use `$var`, never `${var}`.

## 4. GCP → AWS resource mapping

The centerpiece. Left column is the current, deployed behavior; right
column is the proposed AWS rendering.

| Concern | GCP (current) | AWS (proposed) |
|---|---|---|
| Provider block | `google` `~> 6.0`; project/region/zone | `aws`; region (+ optional profile) |
| Network | `google_compute_network` (auto-subnets; custom mode when `nic_count > 1`) | `aws_vpc` + `aws_internet_gateway` + `aws_route_table` (0.0.0.0/0 → IGW) + association + `aws_subnet` with `map_public_ip_on_launch` — AWS has no auto-subnet mode; the explicit trio is always rendered |
| Per-NIC subnets | `google_compute_subnetwork`, `10.10.<i>.0/24` | `aws_subnet` × `nic_count`, `10.10.<i>.0/24`, all in `aws.availability_zone` |
| Firewall | `google_compute_firewall` `allow_ssh` + conditional `allow_public`, matched by instance tag | one `aws_security_group`: ingress tcp/22 from `ssh_source_ranges`, per-proto ingress rules from `open_ports` (0.0.0.0/0), egress allow-all. SGs are stateful default-deny allow-lists — the closest analog; attached to every ENI, preserving the "covers every NIC and address" property |
| Instance | `google_compute_instance`; `image = "project/family"` | `aws_instance`; `ami` from a `data` source (§5); `instance_type`; `root_block_device { volume_size, volume_type }` |
| SSH keys | `ssh-keys` instance metadata (multi-key, **updates in place** on re-apply) | cloud-init `user_data` writing `authorized_keys` for `vm.ssh_user`. `aws_key_pair` is rejected: single key, launch-time only — it cannot express the three-source multi-key merge. Consequence: **key rotation is not in-place on AWS** (§6) |
| Startup script (multi-NIC) | `startup-script` metadata; GCP metadata server | same iproute2 logic inside cloud-init; IMDSv2 probes (below) |
| Extra external IPs | protocol forwarding: `google_compute_address` + `google_compute_target_instance` + `google_compute_forwarding_rule` (`L3_DEFAULT`) | secondary private IPs on the primary ENI + one `aws_eip` + `aws_eip_association(private_ip)` per extra. AWS has no protocol-forwarding analog. The guest must plumb secondary private IPs itself (a user-data job — GCP's guest agent did this for free) |
| Multi-NIC | `network_interface` blocks, one subnet each, ephemeral external IP each | `aws_network_interface` per NIC (`device_index`), one subnet each; secondary ENIs get **no** auto public IP — each needs an `aws_eip`. ENI limits vary by instance type (no vCPU rule) → advisory warning, not a hard check |
| Outputs | `access_config[0].nat_ip` paths | `aws_instance.public_ip` / `aws_eip[*].public_ip` — same output **names** (`public_ip`, `public_ips`, `ssh_command`), preserving the planned Ansible-inventory contract |
| RAM guardrail | parse `<fam>-standard\|highmem\|highcpu-<n>` × GB/vCPU | static tables (§5); warn-only when unparseable. Never call cloud APIs at generate time — offline generation is a property to keep |
| Auth | ADC (`gcloud auth application-default login`) | `AWS_PROFILE` / env vars / `~/.aws/credentials` default chain. Neither provider ever puts credentials in YAML or HCL |

### 4.1 The four hard spots, in prose

**SSH-key delivery.** GCP's `ssh-keys` metadata accepts many keys and the
guest agent reconciles them on every apply. AWS's native `aws_key_pair` is
one key, consumed only at first boot. The design therefore delivers the
merged key list via cloud-init `user_data` that writes
`/home/<ssh_user>/.ssh/authorized_keys` (creating the user if the AMI's
default user differs). Delivery is an **inline HCL heredoc**, consistent
with the GCP startup-script pattern, keeping the "build dir = main.tf +
state, nothing else" property; the alternative (a sidecar user-data file
next to main.tf) was rejected because it breaks that single-artifact
property and complicates `--out` handling.

**Extra external IPs.** The AWS analog of GCP protocol forwarding is:
`external_ip_count - 1` secondary private IPs on the primary ENI, each
associated with an `aws_eip`. Two provider-specific consequences: the
default EIP quota is ~5 per region (so `MAX_EXTERNAL_IPS` may need to be
lower on AWS, or the docs must point at a quota-increase request), and the
guest must add the secondary private IPs to its interface at boot — a
small per-boot user-data script (`ip addr add` from IMDSv2's
`local-ipv4s` list).

**Multi-NIC.** One `aws_network_interface` per NIC, one subnet each (same
`10.10.<i>.0/24` layout as GCP, all in one AZ). The reply-routing problem
is identical, so `policy-routing.sh` carries over with only its
metadata-probe lines changed:

```sh
# GCP (current)                          # AWS (IMDSv2)
MD=http://169.254.169.254/computeMetadata/v1/instance/network-interfaces
HDR="Metadata-Flavor: Google"            TOK=$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
                                               -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
                                         MD=http://169.254.169.254/latest/meta-data/network/interfaces/macs
curl -sf -H "$HDR" "$MD/$i/mac"          curl -sf -H "X-aws-ec2-metadata-token: $TOK" "$MD/"   # lists MACs
curl -sf -H "$HDR" "$MD/$i/ip"           ... "$MD/$mac/local-ipv4s"
curl -sf -H "$HDR" "$MD/$i/gateway"      ... "$MD/$mac/subnet-ipv4-cidr-block"   # gateway = base + 1
```

The `/sys/class/net` MAC→device lookup and the `ip route`/`ip rule`
table-per-NIC logic (tables `100+i`, priorities `10000+i`) are unchanged.
Note AWS's metadata does not expose the gateway directly; it is derived
as the subnet CIDR's first host address.

**RAM inference.** AWS instance-type names don't encode RAM. Mirroring
the GCP approach with static tables keeps generation offline and the
guardrail useful for the common families:

```python
GB_PER_VCPU = {"m": 4, "t": 4, "r": 8, "x": 8, "c": 2}   # family letter
VCPUS = {"large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16,
         "8xlarge": 32, "12xlarge": 48, "16xlarge": 64, "24xlarge": 96}
# "m5.4xlarge" -> m -> 4 GB/vCPU x 16 vCPU = 64 GB
```

Anything unmatched returns `None` → the existing warn-only path ("verify
it has at least N GB"), exactly like unparseable GCP machine types today.

### 4.2 AWS template skeleton

Illustrative sketch of `generator/templates/aws/main.tf.j2` — elided
where marked; it becomes real, `tofu validate`-checked code in Phase 3:

```terraform
# Generated by generator/generate.py — do not edit by hand.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region = "{{ aws.region }}"
  {%- if aws.profile is defined %}
  profile = "{{ aws.profile }}"
  {%- endif %}
}

resource "aws_vpc" "vpc" {
  cidr_block = "{{ aws.vpc_cidr | default('10.10.0.0/16') }}"
  tags       = { Name = "{{ network.name }}" }
}

resource "aws_internet_gateway" "igw" { vpc_id = aws_vpc.vpc.id }

resource "aws_route_table" "rt" {
  vpc_id = aws_vpc.vpc.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
}

resource "aws_subnet" "subnet" {
  count                   = {{ vm.nic_count }}
  vpc_id                  = aws_vpc.vpc.id
  cidr_block              = "10.10.${count.index}.0/24"
  availability_zone       = "{{ aws.availability_zone }}"
  map_public_ip_on_launch = true
}
# ... aws_route_table_association per subnet (elided)

# Stateful default-deny allow-list: the analog of the GCP firewall rules.
resource "aws_security_group" "vm" {
  vpc_id = aws_vpc.vpc.id
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [{% for cidr in network.ssh_source_ranges %}"{{ cidr }}"{{ ", " if not loop.last }}{% endfor %}]
  }
{% for proto in ("tcp", "udp") if open_ports[proto] %}
  # ... one ingress block per open_ports entry (ranges split into from/to; elided)
{% endfor %}
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# OS preset -> AMI (SSM parameter for Ubuntu/Debian, aws_ami filter for
# Rocky/Alma Marketplace images; exact form per preset — elided)
data "aws_ssm_parameter" "ami" { name = "{{ vm.image.ssm_parameter }}" }

resource "aws_instance" "vm" {
  ami           = data.aws_ssm_parameter.ami.value
  instance_type = "{{ vm.machine_type }}"
  root_block_device {
    volume_size = {{ vm.boot_disk_gb }}
    volume_type = "{{ vm.boot_disk_type }}"
  }
  # nic_count == 1: subnet + SG inline; > 1: aws_network_interface
  # attachments per device_index (elided)
  vpc_security_group_ids = [aws_security_group.vm.id]
  subnet_id              = aws_subnet.subnet[0].id
  user_data              = <<-EOT
{{ user_data | indent(4, true, true) }}
  EOT
  tags = { Name = "{{ vm.name }}" }
}

# external_ip_count > 1: secondary private IPs + aws_eip +
# aws_eip_association per extra (elided)

output "public_ip"   { value = aws_instance.vm.public_ip }
output "public_ips"  { value = [aws_instance.vm.public_ip] } # + EIPs when > 1
output "ssh_command" { value = "ssh {{ vm.ssh_user }}@${aws_instance.vm.public_ip}" }
```

### 4.3 Cloud-init user-data sketch

Built by `providers/aws.py:render_context()` from the merged key list —
same no-`${...}` rule as every injected script:

```sh
#!/usr/bin/env bash
# authorized_keys for the base login (all merged keys), then per-boot
# plumbing for secondary private IPs / policy routing when configured.
set -euo pipefail
user=rocky                        # vm.ssh_user
home=$(getent passwd "$user" | cut -d: -f6)
install -d -m 700 -o "$user" -g "$user" "$home/.ssh"
cat > "$home/.ssh/authorized_keys" <<'KEYS'
ssh-ed25519 AAAA... user@host
KEYS
chmod 600 "$home/.ssh/authorized_keys" && chown "$user:$user" "$home/.ssh/authorized_keys"
# ... write + enable a small systemd unit for secondary-IP/policy-routing
#     setup on every boot (elided)
```

## 5. OS presets on AWS

Preset **names** are identical on both providers; only the values differ.
On AWS a preset resolves to either a public SSM parameter (Ubuntu, Debian
— always fresh, no subscription) or a `data.aws_ami` owner+name filter
(Rocky, AlmaLinux — Marketplace):

| preset | AWS source (indicative — verify, §9) | default `ssh_user` |
|---|---|---|
| `ubuntu-24.04` | SSM `/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id` | `ubuntu` |
| `ubuntu-22.04` | SSM `/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id` | `ubuntu` |
| `debian-13` / `debian-12` | SSM `/aws/service/debian/release/<13\|12>/latest/amd64` | `admin` |
| `rocky-10` / `rocky-9` | `data.aws_ami`: official Rocky owner + name filter `Rocky-<10\|9>-EC2-Base-*x86_64` | `rocky` |
| `almalinux-10` / `almalinux-9` | `data.aws_ami`: official Alma owner + name filter | `ec2-user` (verify) |

**Marketplace wrinkle (must be documented prominently in the README when
implemented):** the first `tofu apply` with a Rocky/Alma AMI fails until
the AWS account has accepted that product's Marketplace subscription in
the console — a one-time, per-account manual step with no GCP equivalent.

The free-form escape hatch mirrors GCP: drop `vm.os` and set `vm.image`
with `ami_id: ami-...` (or `ssm_parameter: /aws/service/...`) plus
`vm.ssh_user`. `vm.os` and `vm.image` stay mutually exclusive.

## 6. Use modes per provider

| Step | GCP (today, unchanged) | AWS (proposed) |
|---|---|---|
| One-time auth | `gcloud auth login` + `gcloud auth application-default login` (ADC; consent checkboxes) | `aws configure` / SSO profile; credentials via `AWS_PROFILE`/env default chain |
| One-time account prep | enable `compute.googleapis.com` | accept Marketplace subscription for Rocky/Alma presets |
| Copy example | `cp config/examples/vm.yaml config/vm.yaml` | `cp config/examples/vm-aws.yaml config/vm-aws.yaml` |
| Generate | `.venv/bin/python generator/generate.py` | `.venv/bin/python generator/generate.py --config config/vm-aws.yaml --out build-aws` |
| Verify (no cloud access) | `tofu -chdir=build fmt -check && tofu -chdir=build init -backend=false -input=false && tofu -chdir=build validate` | same, `-chdir=build-aws` |
| Deploy | `tofu -chdir=build init && tofu -chdir=build apply` | same, `-chdir=build-aws` |
| Second VM | `cp config/vm.yaml config/vm2.yaml` + `--out build2` | `cp config/vm-aws.yaml config/vm-aws2.yaml` + `--out build-aws2` |
| Provision accounts | `scp -r provisioning <ssh_user>@IP: && ssh ... setup0.sh` | identical (setup0.sh is distro/cloud-agnostic) |
| Rotate base-login keys | edit keys, regenerate, `apply` → **updates in place** via metadata | edit keys, regenerate, `apply` → **replaces the instance** (user_data change with `user_data_replace_on_change = true`); plan must be reviewed before applying |

Behavioral differences to keep in mind:

| Behavior | GCP | AWS |
|---|---|---|
| Key rotation | in-place, seconds | instance replacement (or manual authorized_keys edit on the VM) |
| Extra external IPs | forwarding rules; guest agent installs routes automatically | EIPs on secondary private IPs; guest plumbs them via user-data unit |
| Default `build/` dir | the deployed GCP VM — never point AWS at it | AWS always uses explicit `--config`/`--out` |
| Image freshness | family reference resolves at apply | SSM parameter resolves at apply; Marketplace filter picks newest matching AMI |

## 7. Guardrails and security parity

All guardrails execute in **core** code paths, so no provider module can
skip them:

- **RAM ≥ `min_memory_gb` (default 64)** — core compares; the provider
  only supplies `machine_ram_gb()`. Unknown names warn instead of fail on
  both providers.
- **Boot disk ≥ 250 GB** — plain core check, provider-independent.
- **Own network, default-deny, SSH + `open_ports` only** — on GCP via
  dedicated VPC + two firewall rules; on AWS via dedicated VPC + one
  security group whose only ingress rules are SSH (from
  `ssh_source_ranges`) and the `open_ports` list. Same two-rule spirit;
  the AGENTS.md rule "no allow rules beyond these without asking" applies
  to both templates.
- **At least one SSH key** — core `resolve_ssh_public_keys` runs for both.

## 8. Migration roadmap

Five phases; each is one commit, docs updated in the same commit, and each
ends with the repo's native verification. The back-compat check —
**`config/vm.yaml` renders byte-identical `build/main.tf` and
`tofu -chdir=build plan` is a no-op** — runs at the end of *every* phase.

1. **P1 — Extract the provider interface (GCP-only).** Create
   `generator/providers/gcp.py`, move `templates/main.tf.j2` and
   `policy-routing.sh` to `templates/gcp/`, add the registry with `gcp`
   as the only entry. Pure refactor. Verify: byte-identical render, plan
   no-op, `tofu fmt/init/validate` on a fresh render, negative tests
   (bad machine type, missing keys) still fail identically.
2. **P2 — Config schema.** Recognize `provider:` (default `gcp`), commit
   `config/examples/vm-aws.yaml`, add the commented `provider: gcp` line
   to the GCP example. `provider: aws` fails cleanly with
   "provider 'aws' not implemented yet". Verify: as P1 + the clean error.
3. **P3 — AWS provider, validate-only.** `providers/aws.py`,
   `templates/aws/*`. Verify: render `vm-aws.yaml` → `build-aws/`,
   `tofu fmt -check`/`init -backend=false`/`validate`; render matrix over
   all AWS presets; negative tests (undersized `machine_type`,
   `boot_disk_gb < 250`, unknown preset listing valid ones); GCP renders
   still byte-identical.
4. **P4 — Real AWS deploy** in a scratch account: SSH reachability with
   the multi-key merge, `open_ports` behavior, then `external_ip_count`
   and `nic_count` exercises — the first real test of the IMDSv2
   policy-routing and secondary-IP scripts. Also close the
   `capture.sh` anchor gap (§9). Record results (quotas hit, AMI users)
   back into this doc.
5. **P5 — Documentation restructure.** README gains per-provider
   sections (§6 becomes the README's use-mode matrix); the auth section
   splits into GCP/AWS subsections; AGENTS.md design decisions and
   current-state updated. This document then flips its status header to
   "implemented".

## 9. Open questions / needs verification

Marked with the phase that must resolve them:

1. Exact SSM parameter paths for Ubuntu 24.04/22.04 and Debian 12/13,
   and whether Debian 13 is published yet (before P3).
2. Rocky/Alma Marketplace AMI owner IDs, name-filter patterns, default
   login users (`rocky` assumed; Alma `ec2-user` unconfirmed), and the
   exact subscription-acceptance failure text (P3/P4).
3. ENI and secondary-private-IP limits for the recommended instance
   types (m5.4xlarge: believed 8 ENIs / 30 IPs per ENI) → the AWS
   `MAX_NICS`/`MAX_EXTERNAL_IPS` values and advisory wording (P3).
4. The static RAM tables against real instance specs; confirm
   `m5.4xlarge` = 16 vCPU / 64 GB as the example default (P3).
5. Default EIP quota (~5/region) vs `MAX_EXTERNAL_IPS` — cap lower on
   AWS or document the quota-increase path (P3).
6. `user_data_replace_on_change` semantics for the key-rotation story —
   confirm replacement is required and whether stop/start suffices (P4).
7. `capture.sh` first-boot anchor on AWS: no `/etc/google_instance_id`;
   candidate anchor `/var/lib/cloud/instances/<instance-id>` mtime
   (cloud-init), falling back to `/etc/machine-id` as today (P4).

## 10. Documentation impact (the P5 spec)

- **README.md**: intro reframed "a VM on GCP or AWS"; Layout gains
  `generator/providers/` and `templates/{gcp,aws}/`; Usage shows both
  copy-example flows; "Choosing the OS" becomes a two-column preset
  table; new "Choosing the provider" section (schema + `provider:` key);
  the extra-IP and multi-NIC sections each get a short "on AWS" note;
  "Google Cloud authentication" becomes "Authentication" with GCP and
  AWS subsections; the use-mode matrix from §6 lands as a summary table.
- **AGENTS.md**: layout rows for the new paths; design-decision bullets
  (provider registry defaulting to gcp; per-provider whole-file
  templates; neutral-key/provider-vocabulary principle; guardrails in
  core); current-state keeps recording that the *deployed* VM is the GCP
  one in `build/` and which phases have landed.
- Both updated in the same commit as each phase, per repo convention.
