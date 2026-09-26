# Multi-provider design: AWS as an alternate provider

**Status: design only.** This document describes a future architecture;
none of it is implemented. The generator today produces GCP
infrastructure only, and the GCP VM already deployed from this
repository must keep working, untouched, while any part of this design
is built.

Two promises make up the compatibility contract, and everything else in
this document is constrained by them:

- A configuration file that does not name a provider is a GCP
  configuration. After every implementation phase, the deployed VM's
  local `config/vm.yaml` must still produce exactly the same
  `build/main.tf`, byte for byte, and `tofu plan` against the live
  state must report no changes.
- Every command, file location, and workflow documented in the README
  today remains valid without modification. AWS support is purely
  additive.

## 1. Goals and non-goals

The goal is to let the same YAML-driven generator deploy the same kind
of VM on either GCP or AWS. "The same kind of VM" means everything the
GCP path provides today: guardrailed memory and disk sizes, an isolated
network that admits nothing but SSH and an explicit list of open ports,
SSH keys merged from several sources for the base login, and —
optionally — several external IP addresses or several network
interfaces. Code that is common to both clouds should live in
one core location; code, templates, and configuration that are specific
to one cloud should be cleanly separated per provider. The
documentation should explain how using each provider differs.

Just as important is what this design does not attempt:

- OpenTofu state stays local, one directory per deployment. No remote
  state backend.
- YAML remains the layer where everything is parameterized. The
  generator renders concrete values into the HCL; we do not switch to
  Terraform/OpenTofu variables. (This repeats an existing design
  decision recorded in AGENTS.md.)
- One configuration file describes one deployment on one provider.
  There is no single config that deploys to both clouds at once.
- No renaming of the repository or of existing files in ways that would
  disturb the deployed VM's workflow.

## 2. What the current code does, and which parts already transfer

Before deciding where to cut, it is worth classifying what the code
actually does. About half of `generator/generate.py` has nothing to do
with GCP and can move to a shared core unchanged; the other half, plus
both template files, is GCP through and through. The tables name
functions rather than line numbers so they stay accurate as the file
evolves.

Provider-neutral — this becomes the shared core:

| Element | What it does |
|---|---|
| `load_config`, `fail`, `example_hint` | reads the YAML, reports errors, and suggests copying the committed example when a config file is missing |
| `resolve_ssh_public_keys`, `PUBLIC_KEY_RE`, `key_file_path` | merges SSH keys from the three configured sources (`ssh_public_key` inline, `ssh_public_key_file`, `ssh_public_keys_file`), removes duplicates, checks each looks like an OpenSSH public key |
| `parse_open_ports`, `OPEN_PORT_RE` | parses the `open_ports` list: a port, a `"low-high"` range, or either with a `tcp:`/`udp:` prefix |
| the disk and memory guardrails | `boot_disk_gb` must be at least 250, inferred RAM at least `min_memory_gb` (default 64). The comparisons are ordinary arithmetic and stay shared; what varies by cloud is how to work out how much memory a machine type has — see §4.1 |
| the `external_ip_count` / `nic_count` checks | both must be integers within provider limits; checking the type and range is shared, the limits themselves belong to the provider |
| `render`, `main` | the Jinja environment (`StrictUndefined`, so a template can only use values the generator actually supplied), the `--config`/`--out` command line, and the output messages |

GCP-specific — this moves into `providers/gcp.py` and
`templates/gcp/`:

| Element | Why it belongs to GCP |
|---|---|
| `OS_PRESETS` | each preset maps to a GCP image project, an image family, and a default login user |
| `MACHINE_TYPE_RE`, `GB_PER_VCPU` | GCP machine-type names such as `n2-standard-16` encode their own specifications: family (`standard` = 4 GB per vCPU, `highmem` = 8, `highcpu` = 1) and vCPU count. The memory guardrail relies on parsing that naming scheme |
| the `gcp.project_id/region/zone` and `vm.image{project,family}` required keys | this is the shape of a GCP configuration |
| `apply_os_preset` | expands `vm.os` into GCP image references |
| `MAX_EXTERNAL_IPS = 8`, `MAX_NICS = 8` | the limits come from GCP: forwarding rules for the extra addresses, and GCP's rule of at most one virtual NIC per vCPU |
| the nic_count ≤ vCPUs check and the `my-gcp-project` placeholder warning | GCP-only rules |
| `generator/templates/main.tf.j2` | every resource it renders is a `google_*` resource |
| `generator/templates/policy-routing.sh` | reads the GCP metadata server (the `Metadata-Flavor: Google` header) to discover per-NIC addresses |

The scripts that run on the VM after deployment are already portable:
`provisioning/setup0.sh` contains nothing cloud-specific, and
`provisioning/capture.sh` is portable except for one detail — it uses
`/etc/google_instance_id` to date the instance's first boot, and AWS
images have no such file (§9 lists the fix).

## 3. Target architecture

### 3.1 Repository tree, before and after

```
BEFORE                                  AFTER
generator/                              generator/
├── generate.py      (everything)       ├── generate.py      (core + CLI + provider registry)
└── templates/                          ├── providers/
    ├── main.tf.j2   (GCP)              │   ├── __init__.py
    └── policy-routing.sh (GCP)         │   ├── gcp.py       (presets, RAM inference, checks)
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
                                        ├── vm.yaml          (GCP, gains a commented `provider: gcp`)
                                        ├── vm-aws.yaml      (new)
                                        └── base_authorized_keys
                                        build/               (deployed GCP VM — untouched)
                                        build-aws/           (naming convention for AWS deploys)
```

### 3.2 The provider interface

Each provider is an ordinary Python module that exports an agreed set
of names. There are no classes and no abstract base classes: with
exactly two implementations and a tool of roughly three hundred lines,
a module per provider plus a small dictionary is easier to read, test,
and debug than an inheritance hierarchy would be. `generate.py` keeps
all the shared logic and picks the provider module once, from the
config:

```python
# generate.py (sketch of the changed part)
from providers import gcp, aws          # generate.py's dir is on sys.path

PROVIDERS = {"gcp": gcp, "aws": aws}

def get_provider(cfg: dict):
    name = cfg.get("provider", "gcp")   # no provider key means GCP
    if name not in PROVIDERS:
        fail(f"unknown provider {name!r}; valid: " + ", ".join(sorted(PROVIDERS)))
    return PROVIDERS[name]
```

Every provider module must export the same surface. Here it is for
`providers/gcp.py`; the bodies are today's code, relocated:

```python
NAME = "gcp"
TEMPLATE_SUBDIR = "gcp"                 # generator/templates/gcp/

# Added to the shared required keys. The core list keeps everything
# both clouds need (network.*, vm.name/machine_type/boot_disk_gb/
# boot_disk_type/image/ssh_user); only the SHAPE of vm.image and
# vm.boot_disk_type is provider-specific, checked in validate() below.
REQUIRED_KEYS = [
    ("gcp", "project_id"), ("gcp", "region"), ("gcp", "zone"),
]

# The preset NAMES are the same on every provider; the values are
# provider-specific.
OS_PRESETS = {
    "rocky-10": ("rocky-linux-cloud", "rocky-linux-10", "rocky"),
    # ... (unchanged table)
}

MAX_EXTERNAL_IPS = 8    # NIC IP + protocol-forwarded extras
MAX_NICS = 8            # GCP: at most one vNIC per vCPU, 10 vNIC cap

def apply_os_preset(cfg) -> None: ...   # vm.os -> vm.image + default ssh_user

def machine_ram_gb(machine_type: str) -> int | None:
    """RAM inferred from the name; None means the core falls back to a
    warning instead of a hard failure."""
    # MACHINE_TYPE_RE / GB_PER_VCPU move here

def validate(cfg) -> None: ...
    # GCP-only checks: image{project,family} shape, nic_count <= vCPUs,
    # the 'my-gcp-project' placeholder warning

def render_context(cfg) -> dict:
    """Provider-specific additions to the Jinja context."""
    # GCP: {"startup_script": <policy-routing.sh text>}
```

The shared `validate()` in `generate.py` keeps performing every check
that does not depend on the cloud: the disk floor, the memory
comparison (it calls `provider.machine_ram_gb()` to get the number, but
compares it itself), the count bounds (against the provider's
`MAX_*` constants), the `ssh_source_ranges` and `open_ports` checks.
Because these checks run in shared code, a provider module cannot
weaken them, accidentally or otherwise. Similarly, `render()` stays in
the core: it loads `templates/<provider>/main.tf.j2` and merges the
provider's extra context (for GCP, the startup script text) into the
shared context it already builds — the config itself, the merged SSH
keys, and the parsed ports.

`providers/aws.py` exports the same names, filled with AWS content:

- `REQUIRED_KEYS` asks for `aws.region` and `aws.availability_zone`
  instead of GCP's three keys (`gcp.project_id`, `gcp.region`,
  `gcp.zone`).
- `OS_PRESETS` maps the same preset names to AMI lookups (§5).
- `machine_ram_gb` estimates memory from static tables instead of
  parsing the type name (§4.1).
- `MAX_EXTERNAL_IPS` and `MAX_NICS` hold AWS's limits, with comments
  explaining where each number comes from.
- `validate` adds AWS-specific advisories — warnings printed for the
  user that do not stop generation, such as the caution that the
  chosen instance type may not support the requested number of
  network interfaces (§4).
- `render_context` returns the cloud-init `user_data` text (§4.3).

### 3.3 Configuration schema

One new optional top-level key selects the provider, each provider owns
one section of the file, and every other key keeps its current name:

- `provider: gcp | aws` — when the key is absent, the provider is GCP.
  This is what keeps every existing config valid.
- `gcp:` — `project_id`, `region`, `zone`, exactly as today.
- `aws:` — `region`, `availability_zone`, an optional `profile`
  (written into the provider block only when present), and an optional
  `vpc_cidr` (default `10.10.0.0/16`, from which subnets are carved as
  `10.10.<i>.0/24` — the same address layout the GCP multi-NIC setup
  uses).

The principle for all the shared keys: **the key keeps one name
everywhere, but its value is written in the selected provider's
vocabulary.** `vm.machine_type` is the same key in both files, but it
holds `n2-standard-16` in a GCP config and `m5.4xlarge` in an AWS
config; `vm.boot_disk_type` holds `pd-balanced` or `gp3`. The generator
does not translate between vocabularies — it hands the value to the
selected provider module, which knows how to check it. This avoids
inventing an abstract machine-type language that would satisfy nobody
and go stale immediately.

The committed examples stay side by side in `config/examples/` rather
than moving into per-provider subdirectories:
`vm.yaml` remains the GCP example (it gains only a commented
`# provider: gcp` line for discoverability), and `vm-aws.yaml` is
added. The gitignore rules (everything in `config/` is ignored except
`examples/`) and the copy-the-example workflow carry over unchanged;
`example_hint()` already suggests `cp` commands by filename, so the AWS
flow is simply `cp config/examples/vm-aws.yaml config/vm-aws.yaml`.

Here is the complete AWS example — this is the file Phase 2 would
commit:

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
  # Same meaning as on GCP but a different mechanism (see the design
  # doc): each extra address is a secondary private IP with an Elastic
  # IP attached.
  external_ip_count: 1
  # Extra ENIs; the limit depends on the instance type (advisory, not
  # enforced).
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

### 3.4 Templates: one complete file per provider

The templates split into `templates/gcp/` and `templates/aws/`, and
each provider gets a complete, self-contained template — there is no
shared Jinja base template and no macro library. The reasoning: when
you compare the two clouds resource by resource, essentially nothing
survives the translation. Every resource type, attribute path, and
argument name differs; the only Jinja the current template could share
is a couple of trivial loops. A shared template layer would save a few
lines while making each provider's output harder to review and
`StrictUndefined` errors harder to trace. The things that genuinely are
shared — port parsing, key merging, the guardrails — already live in
Python and in the config schema, which is where sharing belongs in this
design.

One rule carries over from the GCP template as an invariant for every
provider: a shell script embedded in the HCL through a heredoc (GCP's
`startup-script`, AWS's `user_data`) must never contain `${...}`,
because OpenTofu would try to interpolate it inside the heredoc. Scripts
use `$var`, never `${var}`.

One further safeguard is worth building in from the start. The mistake
this design most wants to prevent is pointing an AWS run at the default
output directory, `build/`, which holds the deployed GCP VM's
configuration and state. Convention alone — "always pass `--out` for
AWS" — is thin protection against a forgotten flag. The fix is
mechanical and cheap: each provider's template writes its provider name
into the generated file's header comment, and the generator refuses to
render one provider's output into a directory whose existing `main.tf`
names a different provider. An explicit override flag covers the rare
legitimate case of repurposing a directory.

## 4. Mapping each GCP construct to AWS

This table is the heart of the design. The left column is the current,
deployed behavior; the right column is what the AWS template renders
instead.

| Concern | GCP (current) | AWS (proposed) |
|---|---|---|
| Provider block | `google` `~> 6.0`; project, region, zone | `aws`; region, plus a profile when configured |
| Network | `google_compute_network`. GCP can create subnets automatically; the template only switches to explicit subnets when `nic_count > 1` | `aws_vpc` + `aws_internet_gateway` + `aws_route_table` (default route to the gateway) + `aws_subnet` with public IPs on launch. AWS has no automatic mode, so the template always creates all of these explicitly |
| Per-NIC subnets | `google_compute_subnetwork`, `10.10.<i>.0/24` | `aws_subnet` × `nic_count`, same `10.10.<i>.0/24` layout, all in `aws.availability_zone` |
| Firewall | two `google_compute_firewall` rules — SSH, and (only when `open_ports` is set) the public ports — matched to the instance by tag | one `aws_security_group` containing the same policy: an ingress rule for tcp/22 from `ssh_source_ranges`, one ingress rule per protocol for `open_ports` from anywhere, and an allow-all egress rule. Security groups are stateful allow-lists that deny by default, so the security model translates directly; the group attaches to every network interface, which preserves the property that one policy covers every NIC and every address |
| Instance | `google_compute_instance`; the image is the string `"project/family"` | `aws_instance`: the AMI comes from a data source (§5), the machine type goes into `instance_type`, and the disk size and type go into a `root_block_device` block |
| SSH keys | the `ssh-keys` instance metadata entry. It accepts many keys, and re-running `apply` after editing keys updates the running VM in place | a cloud-init `user_data` script that writes `authorized_keys` for `vm.ssh_user`. AWS's native `aws_key_pair` cannot express this — it holds a single key and is only read at first boot. The consequence for day-to-day use: on AWS, changing keys means replacing the instance (§6) |
| Startup script (multi-NIC) | injected as `startup-script` metadata; reads the GCP metadata server | the same routing logic inside cloud-init; the metadata queries change to IMDSv2 (§4.1) |
| Extra external IPs | GCP protocol forwarding: one shared `google_compute_target_instance`, plus a reserved `google_compute_address` and a `google_compute_forwarding_rule` per extra address, all delivering to the same NIC | AWS has no equivalent of protocol forwarding. The closest translation: give the primary network interface one secondary private IP per extra address, and attach an `aws_eip` to each. The guest must also be told to use those secondary addresses (§4.1) |
| Multi-NIC | `network_interface` blocks, one subnet each, each with an ephemeral external IP | one `aws_network_interface` per NIC, one subnet each. Secondary interfaces do not get a public IP automatically — each needs its own `aws_eip`. How many ENIs an instance can have depends on the instance type, with no simple rule, so the generator warns rather than enforces |
| Outputs | `public_ip`, `public_ips`, `ssh_command`, read from `access_config[0].nat_ip` | the same three output names, read from `aws_instance.public_ip` and the EIPs. Keeping the names identical matters because the outputs are the planned interface to Ansible inventory |
| Memory guardrail | parse the machine-type name, which encodes family and vCPU count | static lookup tables (§4.1). In neither case does the generator call a cloud API — being able to generate and validate offline is a property worth keeping |
| Authentication | Application Default Credentials (`gcloud auth application-default login`) | the standard AWS credential chain: `AWS_PROFILE`, environment variables, or `~/.aws/credentials`. On both providers, credentials never appear in the YAML or the rendered HCL |

### 4.1 The four places where AWS genuinely differs

**Delivering SSH keys.** GCP's `ssh-keys` metadata is a managed
mechanism: it takes any number of keys, and the in-image guest agent
reconciles the VM's accounts with it on every change. AWS offers
nothing comparable. Its `aws_key_pair` resource holds exactly one
public key and is consulted once, at first boot — it cannot represent
this repo's three-source, many-key merge. So on AWS the merged key list
travels in cloud-init `user_data`, as a script that writes
`/home/<ssh_user>/.ssh/authorized_keys` (creating the user first if the
AMI's default user has a different name). The script is embedded
directly in the HCL as a heredoc, the same way the GCP template embeds
its startup script. The alternative — writing a separate user-data file
next to `main.tf` and referencing it with `file()` — was rejected
because it would break a useful property: today a build directory is
fully described by `main.tf` plus its state file, nothing else.

**Extra external IP addresses.** On GCP, extra addresses are reserved
IPs that protocol forwarding steers to the VM's one NIC, and the guest
agent quietly installs the local routes they need. The AWS translation
has two moving parts. First, the template gives the primary interface
`external_ip_count - 1` secondary private IPs and attaches an Elastic
IP to each. Second — and this is easy to miss — nothing on a stock AMI
configures those secondary private addresses inside the guest, so the
user-data script must add them to the interface at boot (reading them
from the instance metadata). Two practical caveats. First, a fresh AWS
account is limited to five Elastic IPs per region, so the AWS value of
`MAX_EXTERNAL_IPS` may need to be lower than GCP's 8, or the
documentation must point at the quota-increase process. Second, since
February 2024 AWS charges for every public IPv4 address, attached or
not — about $0.005 per hour, roughly $3.60 per month, per address — so
each extra external IP carries a small ongoing cost.

**Multiple NICs.** The AWS side is one `aws_network_interface` per NIC,
each in its own `10.10.<i>.0/24` subnet within one availability zone.
The operating-system problem is identical on both clouds: reply packets
to a secondary interface would otherwise leave through the primary
interface's default route and be dropped. The fix is also identical —
per-interface routing tables — so `policy-routing.sh` carries over with
only its metadata queries changed. GCP's metadata server indexes
interfaces by number and hands out the gateway directly; AWS's IMDSv2
wants a session token first, indexes interfaces by MAC address, and
does not expose the gateway at all (it is, by convention, the first
host address of the subnet's CIDR block):

```sh
# GCP (current)                          # AWS (IMDSv2)
MD=http://169.254.169.254/computeMetadata/v1/instance/network-interfaces
HDR="Metadata-Flavor: Google"            TOK=$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
                                               -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
                                         MD=http://169.254.169.254/latest/meta-data/network/interfaces/macs
curl -sf -H "$HDR" "$MD/$i/mac"          curl -sf -H "X-aws-ec2-metadata-token: $TOK" "$MD/"   # lists MACs
curl -sf -H "$HDR" "$MD/$i/ip"           ... "$MD/$mac/local-ipv4s"
curl -sf -H "$HDR" "$MD/$i/gateway"      ... "$MD/$mac/subnet-ipv4-cidr-block"   # gateway = first host addr
```

Everything after the queries is unchanged: the script finds each device
by its MAC address under `/sys/class/net`, creates one routing table
per interface (numbered 100 + i), and adds one source-address rule per
interface (at priority 10000 + i).

**Inferring memory from the machine type.** GCP machine-type names
encode their own specification, which is what makes the current
guardrail possible: `n2-standard-16` is 16 vCPUs at 4 GB each. EC2
names encode the family and a size word, but not the memory, so the AWS
module ships two small tables instead:

```python
GB_PER_VCPU = {"m": 4, "t": 4, "r": 8, "c": 2}   # family letter
VCPUS = {"large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16,
         "8xlarge": 32, "12xlarge": 48, "16xlarge": 64, "24xlarge": 96}
# "m5.4xlarge" -> m -> 4 GB/vCPU x 16 vCPU = 64 GB
```

A name the tables cannot resolve — including families deliberately left
out, such as the memory-optimized `x` family, whose ratio varies too
much for one number to be honest — produces the same warning the GCP
path produces today for unparseable machine types ("verify it has at
least N GB") rather than a hard failure. Querying the EC2 API for exact
figures was considered and rejected: it would make `generate.py` need
credentials and a network connection, and offline generation is a
property this tool should keep.

### 4.2 AWS template skeleton

An illustrative sketch of `generator/templates/aws/main.tf.j2` —
shortened where marked. It becomes real, `tofu validate`-checked code
in Phase 3.

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
  cidr_block              = cidrsubnet(aws_vpc.vpc.cidr_block, 8, count.index)
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

This script is built by `providers/aws.py:render_context()` from the
merged key list. The same rule applies as to every script embedded in
HCL: `$var`, never `${var}`.

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

The preset names — `rocky-10`, `ubuntu-24.04`, and so on — are the same
on both providers; only what they resolve to differs. On AWS a preset
resolves in one of two ways. Ubuntu and Debian publish the ID of their
latest AMI in public SSM parameters, which the template can read with a
`data "aws_ssm_parameter"` block; the parameter always holds the
current release, and reading it requires no account setup. Rocky and
AlmaLinux publish through the AWS Marketplace
instead, so those presets use a `data "aws_ami"` block that filters by
the vendor's owner ID and a name pattern and picks the newest match.

| preset | AWS source (indicative — verify, §9) | default `ssh_user` |
|---|---|---|
| `ubuntu-24.04` | SSM `/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id` | `ubuntu` |
| `ubuntu-22.04` | SSM `/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id` | `ubuntu` |
| `debian-13` / `debian-12` | SSM `/aws/service/debian/release/<13\|12>/latest/amd64` | `admin` |
| `rocky-10` / `rocky-9` | `data.aws_ami`: official Rocky owner + name filter `Rocky-<10\|9>-EC2-Base-*x86_64` | `rocky` |
| `almalinux-10` / `almalinux-9` | `data.aws_ami`: official Alma owner + name filter | `ec2-user` (unconfirmed — §9 item 2) |

The Marketplace route has a wrinkle with no GCP equivalent, and the
README must say so prominently once this is implemented: the first
`tofu apply` that uses a Rocky or Alma AMI fails until someone has
accepted that product's Marketplace subscription in the AWS console — a
one-time, per-account, manual step.

The escape hatch mirrors GCP: drop `vm.os` and set `vm.image` directly,
with `ami_id: ami-...` (or `ssm_parameter: /aws/service/...`) plus
`vm.ssh_user`. As on GCP, `vm.os` and `vm.image` are mutually
exclusive.

## 6. How using each provider differs

The workflows are deliberately parallel; the table shows them side by
side. The GCP column summarizes today's README, plus the offline
verification sequence that AGENTS.md documents.

| Step | GCP (today, unchanged) | AWS (proposed) |
|---|---|---|
| One-time auth | `gcloud auth login` + `gcloud auth application-default login` | `aws configure` or an SSO profile; the standard credential chain |
| One-time account prep | enable `compute.googleapis.com` | accept the Marketplace subscription if using a Rocky/Alma preset |
| Copy the example | `cp config/examples/vm.yaml config/vm.yaml` | `cp config/examples/vm-aws.yaml config/vm-aws.yaml` |
| Generate | `.venv/bin/python generator/generate.py` | `.venv/bin/python generator/generate.py --config config/vm-aws.yaml --out build-aws` |
| Verify (offline) | `tofu -chdir=build fmt -check && tofu -chdir=build init -backend=false -input=false && tofu -chdir=build validate` | the same three commands with `-chdir=build-aws` |
| Deploy | `tofu -chdir=build init && tofu -chdir=build apply` | the same with `-chdir=build-aws` |
| Second VM | `cp config/vm.yaml config/vm2.yaml`, generate with `--out build2` | `cp config/vm-aws.yaml config/vm-aws2.yaml`, generate with `--out build-aws2` |
| Provision accounts | `scp -r provisioning <ssh_user>@IP:` then run `setup0.sh` | identical — `setup0.sh` is distro- and cloud-agnostic |
| Rotate base-login keys | edit keys, regenerate, `apply`; the running VM is updated in place | edit keys, regenerate, `apply`; the instance is **replaced** (a `user_data` change with `user_data_replace_on_change = true`) — review the plan before applying |

Behavioral differences worth remembering:

| Behavior | GCP | AWS |
|---|---|---|
| Key rotation | in place, takes seconds | replaces the instance (or edit `authorized_keys` on the VM by hand) |
| Extra external IPs | forwarding rules; the guest agent installs routes automatically | Elastic IPs on secondary private addresses; the guest configures them via a user-data unit |
| The default `build/` directory | holds the deployed GCP VM's configuration and state — never point an AWS run at it | AWS runs always name their own `--config` and `--out` |
| Image freshness | the image family resolves to the newest release at apply time | the SSM parameter resolves at apply time; the Marketplace filter picks the newest matching AMI |
| Cost of public IPv4 addresses | GCP bills in-use external IPv4 addresses at a small hourly rate | AWS bills every public IPv4 address about $0.005 per hour (since February 2024) |

Teardown is symmetric: `tofu -chdir=<dir> destroy` removes everything
the corresponding apply created, on either provider.

## 7. Guardrails and security parity

As §3.2 explained, the guardrails run in shared code, so adding a
provider cannot weaken them. This section states, guarantee by
guarantee, how each one maps onto AWS:

- **Memory of at least `min_memory_gb` (default 64).** The core does
  the comparison; the provider only supplies its estimate of the
  machine type's memory. On both providers an unrecognized name
  produces a warning rather than a failure.
- **Boot disk of at least 250 GB.** This check involves nothing
  provider-specific, so it runs unchanged in the shared core.
- **An isolated network that denies everything inbound except SSH and
  the configured `open_ports`.** On GCP this is a dedicated VPC with
  at most two firewall rules — SSH, plus one for `open_ports` when any
  are configured; on AWS it is a dedicated VPC with one
  security group whose only ingress rules are SSH (from
  `ssh_source_ranges`) and the `open_ports` list. The AGENTS.md rule —
  no additional allow rules without asking the user — applies to both
  templates equally.
- **At least one SSH key.** The shared key-merging code enforces this
  for every provider.

## 8. Migration roadmap

The migration proceeds in five phases. Each phase is one commit, and
each commit updates the documentation alongside the code, as this
repository's convention requires. The compatibility check — the deployed
VM's `config/vm.yaml` renders a byte-identical `build/main.tf`, and
`tofu -chdir=build plan` reports no changes — is repeated at the end of
**every** phase.

1. **Extract the provider interface, GCP only.** Create
   `generator/providers/gcp.py`, move the two template files to
   `templates/gcp/`, add the registry with GCP as its only entry. This
   phase changes no behavior; it only moves code. Verify with the
   byte-identical check, plus the usual `tofu fmt`/`init`/`validate` on
   a fresh render, plus the manual negative checks documented in
   AGENTS.md (an undersized machine type, missing keys) still failing
   exactly as before.
2. **Recognize the `provider:` key.** Default it to `gcp`, commit
   `config/examples/vm-aws.yaml`, add the commented `provider: gcp`
   line to the GCP example. A config that says `provider: aws` fails
   cleanly with "provider 'aws' not implemented yet". Verify as in
   phase 1, plus that clean error.
3. **The AWS provider, validated but not deployed.** Write
   `providers/aws.py` and `templates/aws/*`. Verify by rendering
   `vm-aws.yaml` into `build-aws/` and running
   `tofu fmt -check`, `init -backend=false`, and `validate`; render
   every AWS preset; run negative tests (an instance type below the
   memory floor, `boot_disk_gb` below 250, an unknown preset listing
   the valid ones); and confirm GCP renders are still byte-identical.
4. **A real AWS deployment** in a scratch account: SSH reachability
   with the multi-key merge, `open_ports` behavior, then the extra-IP
   and multi-NIC features — the first live test of the IMDSv2 routing
   and secondary-IP scripts. This phase also closes the `capture.sh`
   anchor gap (§9). Whatever this phase learns (quotas, AMI login
   users) gets folded back into this document.
5. **Restructure the documentation.** The README gains per-provider
   sections (§6 becomes its use-mode table), the authentication section
   splits into GCP and AWS halves, and AGENTS.md's design decisions and
   current-state sections are updated. This document's status header
   then changes to "implemented".

## 9. Open questions, to be resolved before the phase noted

1. The exact SSM parameter paths for Ubuntu 24.04/22.04 and Debian
   12/13, and whether Debian 13 is published there yet (phase 3).
2. Rocky and Alma Marketplace AMI owner IDs, name-filter patterns, and
   default login users — `rocky` is assumed for Rocky, `ec2-user` for
   Alma is unconfirmed — plus the exact text of the
   subscription-not-accepted failure (phases 3–4).
3. ENI and secondary-IP limits for the recommended instance types
   (m5.4xlarge is believed to allow 8 ENIs and 30 addresses per ENI),
   which set the AWS values of `MAX_NICS` and `MAX_EXTERNAL_IPS`
   (phase 3).
4. The static memory tables against real instance specifications, and
   the choice of `m5.4xlarge` (16 vCPU / 64 GB) as the example default
   (phase 3).
5. The default Elastic IP quota (five per region) versus
   `MAX_EXTERNAL_IPS`: cap lower on AWS, or document the quota-increase
   path (phase 3).
6. The exact semantics of `user_data_replace_on_change` for the
   key-rotation story — is replacement required, or does a stop/start
   suffice (phase 4).
7. A first-boot anchor for `capture.sh` on AWS, where
   `/etc/google_instance_id` does not exist. The likely candidate is
   the mtime of cloud-init's `/var/lib/cloud/instances/<instance-id>`
   directory, falling back to `/etc/machine-id` as today (phase 4).
8. The AWS provider marks `data.aws_ssm_parameter` values as
   sensitive, so plans would show `ami = (sensitive value)`. Decide
   whether to accept that or to resolve the AMI another way (phase 3).

## 10. What the documentation looks like afterwards

This section doubles as the specification for phase 5.

- **README.md**
  - the introduction becomes "a VM on GCP or AWS";
  - the Layout section gains `generator/providers/` and
    `templates/{gcp,aws}/`;
  - Usage shows both copy-the-example flows;
  - "Choosing the OS" becomes a two-column preset table;
  - a new "Choosing the provider" section explains the `provider:`
    key and the two config sections;
  - the extra-IP and multi-NIC sections each get a short "on AWS"
    paragraph;
  - "Google Cloud authentication" becomes "Authentication", with a
    subsection per provider;
  - the use-mode table from §6 lands as a summary.
- **AGENTS.md** — layout rows for the new paths; design-decision
  bullets for the provider registry (defaulting to GCP), the
  one-template-per-provider decision, the "same key, provider
  vocabulary" principle, and the guardrails-stay-in-core rule; the
  current-state section keeps recording that the *deployed* VM is the
  GCP one in `build/` and which phases have landed.
- Both files change in the same commit as each phase, per the repo's
  documentation convention.
