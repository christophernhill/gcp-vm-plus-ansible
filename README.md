# gcp-vm-plus-ansible

Python generator that renders OpenTofu configuration for a VM on GCP or
AWS (Rocky Linux 10 by default — see "Choosing the OS"): 64 GB+ RAM,
250 GB boot disk, SSH open to the internet, all other inbound ports
closed unless listed in `network.open_ports` (the VM sits on its own
VPC, which denies ingress by default on either cloud).

## Layout

```
config/examples/             committed config templates (vm.yaml for GCP,
                             vm-aws.yaml for AWS) — copy them into
                             config/ and edit the copies
config/vm.yaml               all tunable settings (project, zone, machine
                             type, disk, OS, SSH keys, external IP
                             count, allowed CIDRs, open ports); your local
                             copy of the example, gitignored
config/base_authorized_keys  initial public keys for the base login
                             (vm.ssh_user), one per line; local copy,
                             gitignored
generator/generate.py        shared core: loads + validates the YAML, runs
                             the guardrails, renders the selected
                             provider's template
generator/providers/         one module per cloud (gcp.py, aws.py):
                             presets, RAM inference, provider-only checks
generator/templates/gcp/     OpenTofu HCL template + policy-routing.sh (GCP)
generator/templates/aws/     OpenTofu HCL template + policy-routing.sh (AWS)
docs/                        design documents (multi-provider architecture)
provisioning/                material that runs on the VM after apply:
                             setup0.sh (admin accounts), capture.sh
                             (state report), keys/ (public keys)
build/                       generated main.tf lands here (gitignored);
                             AWS deploys conventionally use build-aws/
```

## Usage

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config/examples/vm.yaml config/vm.yaml
cp config/examples/base_authorized_keys config/base_authorized_keys
# edit config/vm.yaml: set gcp.project_id and at least one SSH key source
# (add keys to config/base_authorized_keys and/or set vm.ssh_public_key*)
.venv/bin/python generator/generate.py
tofu -chdir=build init
tofu -chdir=build apply
```

For AWS, start from the AWS example and name the output directory
explicitly (see "Choosing the provider" for the current AWS status
before running `apply`):

```sh
cp config/examples/vm-aws.yaml config/vm-aws.yaml
# edit config/vm-aws.yaml: aws.region / availability_zone, a verified
# vm.os preset, and at least one SSH key source
.venv/bin/python generator/generate.py --config config/vm-aws.yaml --out build-aws
tofu -chdir=build-aws init
tofu -chdir=build-aws apply
```

Everything in `config/` except `examples/` is gitignored, so your real
project ID, keys, and any extra configs (e.g. `vm2.yaml`) stay local —
only the templates in `config/examples/` are committed.

The generator enforces the guardrails: it fails if the machine type has
less than `vm.min_memory_gb` (default 64) or the boot disk is under 250 GB.

Outputs after `apply`: the VM's public IP (`public_ip`), the list of all
external IPv4 addresses (`public_ips`), and a ready-to-paste `ssh` command.

## Choosing the provider

The optional top-level `provider:` key selects the cloud: `gcp` (the
default when the key is absent — every config written before the key
existed is a GCP config) or `aws`. Each provider owns one section of
the config file — `gcp:` with `project_id`/`region`/`zone`, or `aws:`
with `region`/`availability_zone` plus optional `profile` and
`vpc_cidr`. Every other key keeps one name everywhere, but its value is
written in the selected provider's vocabulary: `vm.machine_type` holds
`n2-standard-16` on GCP and `m5.4xlarge` on AWS; `vm.boot_disk_type`
holds `pd-balanced` or `gp3`. One config file describes one deployment
on one provider.

Each generated `main.tf` carries its provider in a header marker, and
the generator refuses to render one provider's output into a directory
whose existing `main.tf` names another (`--force` overrides) — an AWS
run can never clobber the GCP `build/` directory by a forgotten
`--out` flag.

**AWS status: deployed and verified** (2026-09-26, us-east-1): the
example config was applied end-to-end (SSH with the merged key list,
`open_ports` behavior), a second config with `external_ip_count: 3` +
`nic_count: 2` answered SSH on every address, key rotation was
confirmed to replace the instance, and everything was torn down
afterwards. One cosmetic note: with the SSM-backed presets (Ubuntu,
Debian, Amazon Linux), `tofu plan` shows `ami = (sensitive value)` —
the AWS provider marks SSM parameter values sensitive; the AMI ID is
not actually secret.

## Choosing the OS

Set `vm.os` to one of the presets; it picks the boot image and the
distro's conventional login name for `vm.ssh_user` (override by setting
`ssh_user` yourself). The preset names are the same on both providers;
what they resolve to differs:

| `vm.os` | GCP image (project/family) | AWS image lookup | default `ssh_user` (GCP / AWS) |
|---|---|---|---|
| `rocky-10` (example default) | rocky-linux-cloud/rocky-linux-10 | name filter `Rocky-10-EC2-Base-*x86_64*` ¹ | `rocky` |
| `rocky-9` | rocky-linux-cloud/rocky-linux-9 | name filter `Rocky-9-EC2-Base-*x86_64*` ¹ | `rocky` |
| `almalinux-10` | almalinux-cloud/almalinux-10 | name filter `AlmaLinux OS 10.* x86_64` (spaces) | `almalinux` / `ec2-user` ² |
| `almalinux-9` | almalinux-cloud/almalinux-9 | name filter `AlmaLinux OS 9.* x86_64` (spaces) | `almalinux` / `ec2-user` ² |
| `centos-stream-10` | centos-cloud/centos-stream-10 | name filter `CentOS Stream 10 x86_64*` (spaces) | `centos` / `ec2-user` ² |
| `centos-stream-9` | centos-cloud/centos-stream-9 | name filter `CentOS Stream 9 x86_64*` (spaces) | `centos` / `ec2-user` ² |
| `fedora-44` | fedora-cloud/fedora-cloud-44-x86-64 | name filter `Fedora-Cloud-Base-AmazonEC2.x86_64-44-*` | `fedora` |
| `fedora-43` | fedora-cloud/fedora-cloud-43-x86-64 | name filter `Fedora-Cloud-Base-AmazonEC2.x86_64-43-*` | `fedora` |
| `ubuntu-24.04` | ubuntu-os-cloud/ubuntu-2404-lts-amd64 | SSM `/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id` | `ubuntu` |
| `ubuntu-22.04` | ubuntu-os-cloud/ubuntu-2204-lts | SSM `.../server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id` | `ubuntu` |
| `debian-13` | debian-cloud/debian-13 | SSM `/aws/service/debian/release/13/latest/amd64` | `debian` / `admin` |
| `debian-12` | debian-cloud/debian-12 | SSM `/aws/service/debian/release/12/latest/amd64` | `debian` / `admin` |
| `amazon-linux-2023` | — (AWS only; no GCP images exist) | SSM `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64` | `ec2-user` |

¹ The Rocky AMIs are AWS Marketplace products: the first `apply` fails
with `OptInRequired` until someone accepts the product's subscription
in the AWS console (one time per account, free; the error message
includes the product-page URL). The Rocky product also rejects
burstable (t2/t3) instance types — a non-issue here, since the 64 GB
RAM guardrail already rules them out. AlmaLinux, CentOS Stream, and
Fedora are plain community AMIs needing no subscription.

² Where the clouds differ, the value is GCP / AWS. All AWS login users
were verified by booting the AMIs (2026-09-26) — note CentOS Stream's
is `ec2-user`, not `centos`. All AWS owner IDs and name filters were
verified against the live API the same day.

For any other image, drop `vm.os` and set `vm.image` directly (the two
are mutually exclusive). On GCP: `vm.image.project` +
`vm.image.family` (find families with `gcloud compute images list`).
On AWS, exactly one of three shapes: `{ami_id: ami-...}`,
`{ssm_parameter: /aws/service/...}`, or `{ami_owner: ...,
ami_name_filter: ...}` — plus `vm.ssh_user`. The RAM/disk guardrails
apply either way, and everything else (firewall, extra IPs, NICs,
provisioning) is distro-independent.

One GCP caveat: the Fedora images are published by the Fedora project
rather than by Google, and it has not been verified that they ship the
google-guest-agent — SSH keys will land at first boot either way (via
cloud-init), but in-place key updates and the automatic routes for
extra forwarded IPs depend on the agent.

## Opening extra ports

By default the firewall admits only SSH. To open more ports to the
public internet, list them in `network.open_ports`:

```yaml
network:
  open_ports: [80, 443, "8000-8100", "udp:51820"]
```

Each entry is a single port, a `"low-high"` range, or either with a
`tcp:`/`udp:` prefix — plain entries are TCP. On GCP the generator
renders one extra firewall rule (`<network>-allow-public`) with source
`0.0.0.0/0`; on AWS each entry becomes one security-group ingress rule
(with the range split into `from_port`/`to_port`), same source. These
ports are always world-reachable, unlike SSH, whose sources are
governed separately by `network.ssh_source_ranges`. The rule targets
the VM (GCP tag / the ENIs' security group), so it covers every
external IP (`vm.external_ip_count`) and every NIC (`vm.nic_count`).
An empty or absent list renders no extra rule, leaving SSH as the only
open port.

Set `vm.external_ip_count` (1–8, default 1) to give the VM more than one
external IPv4 address. The first address is the NIC's ephemeral IP, as
before; each additional one is a reserved static IP routed to the same
NIC with GCP protocol forwarding (`google_compute_address` +
`google_compute_target_instance` + `google_compute_forwarding_rule`,
protocol `L3_DEFAULT`). The google-guest-agent shipped in official GCP
images adds local routes for the forwarded addresses automatically, and
the VPC firewall applies to them like any other traffic — the same
rules (SSH plus any `network.open_ports`) govern every address.

Note these forwarded addresses do **not** appear as interfaces in
`ip a` on the VM — they land on the one NIC. If you want actual extra
NICs, use `vm.nic_count` instead (next section); the two knobs are
independent and can be combined.

On AWS the same knob (1–5 there) works differently: each extra address
is a secondary private IP on the primary network interface with an
Elastic IP associated to it, and a small systemd unit installed via
`user_data` configures the secondary addresses inside the guest at each
boot (stock AMIs configure only the primary address). Two practical
differences: fresh AWS accounts are limited to 5 Elastic IPs per region
(a quota increase lifts it), and since February 2024 AWS bills *every*
public IPv4 address at about $0.005/hour (~$3.60/month), attached or
not. Also note that whenever the AWS template uses explicit network
interfaces (`nic_count` > 1 or `external_ip_count` > 1), even the
primary interface's base address is an Elastic IP, because attaching
pre-created ENIs disables the subnet's auto-assign-public-IP.

## Multiple NICs

Set `vm.nic_count` (1–8, default 1) to give the VM that many network
interfaces, each visible in `ip a` with its own internal subnet
(`10.10.<i>.0/24`) and its own ephemeral external IP. With a count
above 1 the VPC switches from auto-created subnets to one explicit
subnet per NIC — GCP allows NICs to share a VPC as long as each has a
unique subnet — so the single-VPC/default-deny security model is
unchanged, and the same firewall rules cover every NIC. A
startup-script (`generator/templates/gcp/policy-routing.sh`) is injected
into the instance metadata to set up source-based policy routing at
each boot, so inbound connections to the secondary NICs' external IPs
get their replies out of the right interface.

Two caveats: the NIC count is fixed at instance creation, so changing
it **replaces the VM** (and the network, because of the subnet-mode
switch) — use it for new VMs, not the deployed one; and GCP allows at
most one vNIC per vCPU (the generator checks this when it can parse
the machine type).

On AWS the same knob creates one `aws_network_interface` per NIC, each
in its own `10.10.<i>.0/24` subnet within the single configured
availability zone, and each secondary ENI gets its own Elastic IP
(secondary ENIs receive no automatic public IP). The same
policy-routing script runs, with its metadata queries translated to
IMDSv2 (`generator/templates/aws/policy-routing.sh`). AWS ENI limits
depend on the instance type with no simple rule, so the generator
prints an advisory instead of enforcing a bound.

## Running a second VM

The generator takes `--config` and `--out`, and each output directory
carries its own OpenTofu state, so a second VM is a second config:

```sh
cp config/vm.yaml config/vm2.yaml
# edit vm2.yaml: change vm.name and network.name (must not collide),
# e.g. set nic_count: 4 for a 4-NIC machine
.venv/bin/python generator/generate.py --config config/vm2.yaml --out build2
tofu -chdir=build2 init && tofu -chdir=build2 apply
```

## How using each provider differs

The workflows are deliberately parallel (the architecture and its
rationale live in
[`docs/multi-provider-design.md`](docs/multi-provider-design.md)):

| Step | GCP | AWS |
|---|---|---|
| One-time auth | `gcloud auth login` + `gcloud auth application-default login` | `aws configure` or an SSO profile; the standard credential chain |
| One-time account prep | enable `compute.googleapis.com` | accept the Marketplace subscription if using a Rocky preset |
| Copy the example | `cp config/examples/vm.yaml config/vm.yaml` | `cp config/examples/vm-aws.yaml config/vm-aws.yaml` |
| Generate | `.venv/bin/python generator/generate.py` | `.venv/bin/python generator/generate.py --config config/vm-aws.yaml --out build-aws` |
| Verify (offline) | `tofu -chdir=build fmt -check && tofu -chdir=build init -backend=false -input=false && tofu -chdir=build validate` | the same three commands with `-chdir=build-aws` |
| Deploy | `tofu -chdir=build init && tofu -chdir=build apply` | the same with `-chdir=build-aws` |
| Second VM | `cp config/vm.yaml config/vm2.yaml`, generate with `--out build2` | `cp config/vm-aws.yaml config/vm-aws2.yaml`, generate with `--out build-aws2` |
| Provision accounts | `scp -r provisioning <ssh_user>@IP:` then run `setup0.sh` | identical — `setup0.sh` is distro- and cloud-agnostic |
| Rotate base-login keys | edit keys, regenerate, `apply`; the running VM is updated in place | edit keys, regenerate, `apply`; the instance is **replaced** (a `user_data` change with `user_data_replace_on_change = true`) — review the plan before applying |

Behavioral differences worth remembering:

- **Key rotation**: in place on GCP; replaces the instance on AWS (or
  edit `authorized_keys` on the VM by hand).
- **Extra external IPs**: GCP forwarding rules, with routes installed
  by the guest agent; AWS Elastic IPs on secondary private addresses,
  configured in the guest by a `user_data`-installed unit.
- **The default `build/` directory** holds the deployed GCP VM's
  configuration and state — AWS runs always name their own `--config`
  and `--out` (the provider marker in each generated `main.tf` makes a
  mix-up a clean error rather than a clobber).
- **Image freshness**: a GCP image family resolves to the newest
  release at apply time; on AWS the SSM parameter resolves at apply
  time and the Marketplace name filter picks the newest matching AMI.
- **Public IPv4 cost**: GCP bills in-use external addresses at a small
  hourly rate; AWS bills every public IPv4 address (~$0.005/hour)
  whether or not it is attached.

Teardown is symmetric: `tofu -chdir=<dir> destroy` removes everything
the corresponding apply created, on either provider.

## SSH keys for the base login

The keys that can log in as the base user (`vm.ssh_user`) come from
three merged sources in `config/vm.yaml` (duplicates removed; at least
one key is required):

- `vm.ssh_public_keys_file` — a file of initial keys, one per line in
  `authorized_keys` format (blank lines and `#` comments ignored); the
  example config points it at `config/base_authorized_keys`
- `vm.ssh_public_key_file` — a single-key file
- `vm.ssh_public_key` — a key pasted inline

On GCP they are injected via the instance's `ssh-keys` metadata, so
re-running the generator and `tofu apply` after editing keys updates
the VM in place. On AWS they ride in the cloud-init `user_data` script
(which writes `authorized_keys` for `vm.ssh_user`, creating the user if
the AMI has no such account), so changing keys **replaces the
instance**.

Note: OpenTofu state is local (`build/terraform.tfstate`, gitignored) —
there is no remote backend, so the machine that ran `apply` owns the
deployment. Re-run the generator after any change to `config/vm.yaml`
or the template, then `tofu -chdir=build apply` to reconcile.

## Provisioning the VM

`provisioning/setup0.sh` creates the admin accounts (`lincolnb`,
`tloizou`) with sudo privileges and SSH-key-only access: passwords are
locked, and each account's `provisioning/keys/<username>.pub` becomes
its `authorized_keys`. Drop the public keys in `provisioning/keys/`
first (see the README there), then:

```sh
IP=$(tofu -chdir=build output -raw public_ip)
scp -r provisioning rocky@"$IP":    # rocky = vm.ssh_user; adjust for your OS
ssh rocky@"$IP" 'sudo bash provisioning/setup0.sh'
```

The script is idempotent — re-run it after adding or rotating keys. It
detects the distro's sudo group at runtime (`wheel` on RHEL-family,
`sudo` on Debian-family), so it works on every `vm.os` preset.

## Capturing the VM's state

`provisioning/capture.sh` is the read-only counterpart to `setup0.sh`:
it prints a report with enough detail to reconfigure a new VM the same
way — accounts (with SSH key fingerprints and login sessions), sudoers
drop-ins, packages installed since the anchor plus replay lists
(`dnf history userinstalled` / `apt-mark showmanual`), enabled
repositories and the full transaction history, enabled/running
services and units whose state differs from the vendor preset,
locally added systemd units and drop-ins (with contents), containers
(systemd-nspawn configs, machinectl, docker/podman/lxc), timers and
cron, listening sockets, network/policy-routing state, firewall zones,
storage/fstab, and the files changed under `/etc` — including their
contents, comment-stripped. Files whose names suggest secrets (keys,
password stores) are listed but their contents withheld, and
password-like lines inside other files are redacted. It modifies
nothing:

```sh
ssh rocky@"$IP" 'sudo bash provisioning/capture.sh' > vm-state.txt
```

The "since" anchor defaults to the instance's first boot (the mtime of
`/etc/google_instance_id` on GCP, then `/var/lib/cloud/instance` —
cloud-init's per-instance directory — on AWS and other clouds, falling
back to `/etc/machine-id`, whose mtime can be the image build); pass
any `date -d`-parsable timestamp to override:

```sh
ssh rocky@"$IP" 'sudo bash provisioning/capture.sh "2026-09-23 12:00"'
```

Like `setup0.sh`, it is distro-aware (rpm/dnf vs dpkg) and works on
every `vm.os` preset. Reports can contain sensitive operational detail
(IPs, usernames, sockets) even with secrets withheld — treat saved
reports as private and keep them out of the repo.

## Authentication

Credentials never appear in the YAML or the rendered HCL, on either
provider.

### GCP

OpenTofu's `google` provider authenticates with **Application Default
Credentials (ADC)**. Two things that don't work: GCP "API keys" (they
don't cover Compute Engine), and `gcloud auth login` on its own (that
only authenticates the gcloud CLI — the provider reads a separate
credential file that only the `application-default` login creates).

One-time setup:

```sh
brew install --cask google-cloud-sdk    # if gcloud is not installed

gcloud auth login                       # authenticates the gcloud CLI
gcloud config set project YOUR_PROJECT_ID
gcloud services enable compute.googleapis.com

gcloud auth application-default login   # creates the ADC file tofu uses
```

The last command opens a browser. On Google's consent screen, **check
all the permission checkboxes** — in particular "See, edit, configure,
and delete your Google Cloud data" (the `cloud-platform` scope) —
before clicking Continue. Leaving them unchecked fails with:

```
ERROR: ... cloud-platform scope is required but not consented.
```

On success, credentials are saved to
`~/.config/gcloud/application_default_credentials.json`, which the
provider discovers automatically — no credentials go in `vm.yaml` or
the provider block.

For CI or automation, use a service account key instead: point the
`GOOGLE_APPLICATION_CREDENTIALS` environment variable at the key's JSON
file, and never commit that file.

### AWS

The `aws` provider uses the standard AWS credential chain: the
`AWS_PROFILE` environment variable, `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`, or `~/.aws/credentials` (written by
`aws configure`, or `aws login` for SSO). Set the optional
`aws.profile` key in the config to pin a named profile into the
provider block. One-time account prep: if you use a Rocky preset,
accept the product's AWS Marketplace subscription in the console
first; the initial apply fails without it (see "Choosing the OS").
