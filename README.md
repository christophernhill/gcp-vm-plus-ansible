# gcp-vm-plus-ansible

Python generator that renders OpenTofu configuration for a Rocky Linux 10
VM on GCP: 64 GB+ RAM, 250 GB boot disk, SSH open to the internet, all
other inbound ports closed (the VM sits on its own VPC, and GCP VPCs deny
ingress by default).

## Layout

```
config/vm.yaml               all tunable settings (project, zone, machine
                             type, disk, image, SSH keys, external IP
                             count, allowed CIDRs)
config/rocky_authorized_keys initial public keys for the base login
                             ("rocky"), one per line
generator/generate.py        loads + validates the YAML, renders the template
generator/templates/main.tf.j2   OpenTofu HCL template
provisioning/                material that runs on the VM after apply:
                             setup0.sh (admin accounts), keys/ (public keys)
build/                       generated main.tf lands here (gitignored)
```

## Usage

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# edit config/vm.yaml — gcp.project_id and the SSH key are set for the
# author's environment; change them for yours
.venv/bin/python generator/generate.py
tofu -chdir=build init
tofu -chdir=build apply
```

The generator enforces the guardrails: it fails if the machine type has
less than `vm.min_memory_gb` (default 64) or the boot disk is under 250 GB.

Outputs after `apply`: the VM's public IP (`public_ip`), the list of all
external IPv4 addresses (`public_ips`), and a ready-to-paste `ssh` command.

## Multiple external IPv4 addresses

Set `vm.external_ip_count` (1–8, default 1) to give the VM more than one
external IPv4 address. The first address is the NIC's ephemeral IP, as
before; each additional one is a reserved static IP routed to the same
NIC with GCP protocol forwarding (`google_compute_address` +
`google_compute_target_instance` + `google_compute_forwarding_rule`,
protocol `L3_DEFAULT`). The google-guest-agent shipped in the Rocky
image adds local routes for the forwarded addresses automatically, and
the VPC firewall applies to them like any other traffic — SSH stays the
only open port on every address.

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
unchanged, and the one SSH firewall rule covers every NIC. A
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

## SSH keys for the base login

The keys that can log in as `rocky` come from three merged sources in
`config/vm.yaml` (duplicates removed; at least one key is required):

- `vm.ssh_public_keys_file` — a file of initial keys, one per line in
  `authorized_keys` format (blank lines and `#` comments ignored);
  defaults to `config/rocky_authorized_keys`
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
scp -r provisioning rocky@"$IP":
ssh rocky@"$IP" 'sudo bash provisioning/setup0.sh'
```

The script is idempotent — re-run it after adding or rotating keys.

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
