# gcp-vm-plus-ansible

Python generator that renders OpenTofu configuration for a VM on GCP
(Rocky Linux 10 by default — see "Choosing the OS"): 64 GB+ RAM, 250 GB
boot disk, SSH open to the internet, all other inbound ports closed
unless listed in `network.open_ports` (the VM sits on its own VPC, and
GCP VPCs deny ingress by default).

## Layout

```
config/examples/             committed config templates — copy them into
                             config/ and edit the copies
config/vm.yaml               all tunable settings (project, zone, machine
                             type, disk, OS, SSH keys, external IP
                             count, allowed CIDRs, open ports); your local
                             copy of the example, gitignored
config/base_authorized_keys  initial public keys for the base login
                             (vm.ssh_user), one per line; local copy,
                             gitignored
generator/generate.py        loads + validates the YAML, renders the template
generator/templates/main.tf.j2   OpenTofu HCL template
docs/                        design documents (multi-provider architecture)
provisioning/                material that runs on the VM after apply:
                             setup0.sh (admin accounts), capture.sh
                             (state report), keys/ (public keys)
build/                       generated main.tf lands here (gitignored)
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

Everything in `config/` except `examples/` is gitignored, so your real
project ID, keys, and any extra configs (e.g. `vm2.yaml`) stay local —
only the templates in `config/examples/` are committed.

The generator enforces the guardrails: it fails if the machine type has
less than `vm.min_memory_gb` (default 64) or the boot disk is under 250 GB.

Outputs after `apply`: the VM's public IP (`public_ip`), the list of all
external IPv4 addresses (`public_ips`), and a ready-to-paste `ssh` command.

## Choosing the OS

Set `vm.os` to one of the presets; it picks the boot image and the
distro's conventional login name for `vm.ssh_user` (override by setting
`ssh_user` yourself):

| `vm.os` | image | default `ssh_user` |
|---|---|---|
| `rocky-10` (example default) | rocky-linux-cloud/rocky-linux-10 | `rocky` |
| `rocky-9` | rocky-linux-cloud/rocky-linux-9 | `rocky` |
| `almalinux-10` | almalinux-cloud/almalinux-10 | `almalinux` |
| `almalinux-9` | almalinux-cloud/almalinux-9 | `almalinux` |
| `ubuntu-24.04` | ubuntu-os-cloud/ubuntu-2404-lts-amd64 | `ubuntu` |
| `ubuntu-22.04` | ubuntu-os-cloud/ubuntu-2204-lts | `ubuntu` |
| `debian-13` | debian-cloud/debian-13 | `debian` |
| `debian-12` | debian-cloud/debian-12 | `debian` |

For any other GCP public image, drop `vm.os` and set `vm.image.project`
+ `vm.image.family` (plus `vm.ssh_user`) directly — `vm.os` and
`vm.image` are mutually exclusive. Find families with
`gcloud compute images list`. The RAM/disk guardrails apply either way,
and everything else (firewall, extra IPs, NICs, provisioning) is
distro-independent.

## Opening extra ports

By default the firewall admits only SSH. To open more ports to the
public internet, list them in `network.open_ports`:

```yaml
network:
  open_ports: [80, 443, "8000-8100", "udp:51820"]
```

Each entry is a single port, a `"low-high"` range, or either with a
`tcp:`/`udp:` prefix — plain entries are TCP. The generator renders one
extra firewall rule (`<network>-allow-public`) with source
`0.0.0.0/0`; these ports are always world-reachable, unlike SSH, whose
sources are governed separately by `network.ssh_source_ranges`. The
rule targets the VM's tag, so it covers every external IP
(`vm.external_ip_count`) and every NIC (`vm.nic_count`). An empty or
absent list renders no extra rule, leaving SSH as the only open port.

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

## Multiple NICs

Set `vm.nic_count` (1–8, default 1) to give the VM that many network
interfaces, each visible in `ip a` with its own internal subnet
(`10.10.<i>.0/24`) and its own ephemeral external IP. With a count
above 1 the VPC switches from auto-created subnets to one explicit
subnet per NIC — GCP allows NICs to share a VPC as long as each has a
unique subnet — so the single-VPC/default-deny security model is
unchanged, and the same firewall rules cover every NIC. A
startup-script (`generator/templates/policy-routing.sh`) is injected
into the instance metadata to set up source-based policy routing at
each boot, so inbound connections to the secondary NICs' external IPs
get their replies out of the right interface.

Two caveats: the NIC count is fixed at instance creation, so changing
it **replaces the VM** (and the network, because of the subnet-mode
switch) — use it for new VMs, not the deployed one; and GCP allows at
most one vNIC per vCPU (the generator checks this when it can parse
the machine type).

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

## Multiple providers (design)

The generator currently targets GCP only. An architecture for adding AWS
as an alternate provider is fully specified in
[`docs/multi-provider-design.md`](docs/multi-provider-design.md): an
optional `provider:` config key defaulting to `gcp`, provider modules
under `generator/providers/`, per-provider templates and example configs,
and a five-phase migration roadmap. **None of it is implemented yet** —
every workflow in this README is unchanged, and existing configs need no
edits (a config without a `provider:` key is a GCP config by definition).

## SSH keys for the base login

The keys that can log in as the base user (`vm.ssh_user`) come from
three merged sources in `config/vm.yaml` (duplicates removed; at least
one key is required):

- `vm.ssh_public_keys_file` — a file of initial keys, one per line in
  `authorized_keys` format (blank lines and `#` comments ignored); the
  example config points it at `config/base_authorized_keys`
- `vm.ssh_public_key_file` — a single-key file
- `vm.ssh_public_key` — a key pasted inline

They are injected via the instance's `ssh-keys` metadata, so re-running
the generator and `tofu apply` after editing keys updates the VM in
place.

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
`/etc/google_instance_id`, falling back to `/etc/machine-id`, whose
mtime can be the image build); pass any `date -d`-parsable timestamp
to override:

```sh
ssh rocky@"$IP" 'sudo bash provisioning/capture.sh "2026-09-23 12:00"'
```

Like `setup0.sh`, it is distro-aware (rpm/dnf vs dpkg) and works on
every `vm.os` preset. Reports can contain sensitive operational detail
(IPs, usernames, sockets) even with secrets withheld — treat saved
reports as private and keep them out of the repo.

## Google Cloud authentication

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
