# AGENTS.md

Context for agents picking up work on this repository.

## What this project is

A Python generator that renders OpenTofu HCL to create a single VM on
GCP (Rocky Linux 10 by default, selectable via the `vm.os` preset).
Hard requirements the code enforces:

- At least 64 GB RAM (default machine type: `n2-standard-16`)
- At least 250 GB of disk (a `pd-balanced` boot disk)
- SSH reachable from the open internet; **all other inbound ports
  closed** unless explicitly listed in `network.open_ports`
- All key settings parameterized in one YAML file (`config/vm.yaml`)

The flow is: edit `config/vm.yaml` → run `generator/generate.py` (loads
YAML, validates, renders the Jinja2 template) → `build/main.tf` → deploy
with `tofu`.

## Layout

| Path | Role |
|---|---|
| `config/examples/` | Committed config templates (placeholder values, no real keys) — copy into `config/` and edit |
| `config/vm.yaml` | Single source of truth for all settings. Local copy of the example — gitignored |
| `config/base_authorized_keys` | Initial public keys for the base login (`vm.ssh_user`), one per line. Local copy — gitignored |
| `generator/generate.py` | Loads + validates YAML, renders the template |
| `generator/templates/main.tf.j2` | OpenTofu HCL template |
| `generator/templates/policy-routing.sh` | Startup script injected when `vm.nic_count` > 1 (reply routing for secondary NICs) |
| `provisioning/` | Runs on the VM after apply: `setup0.sh` creates admin accounts (sudo, SSH-key-only); `keys/` holds users' public keys |
| `build/` | Generated `main.tf` + tofu state. Gitignored — never edit or commit |
| `.venv/` | Local venv with PyYAML + Jinja2 (gitignored) |

## Design decisions (don't undo these casually)

- **YAML is the parameterization layer, not tofu variables.** The
  generator renders concrete values into the HCL. Keep new knobs in
  `vm.yaml`, not in `variables.tf`.
- **Persistent boot disk, not GCP Local SSD.** Local SSD only comes in
  fixed 375 GB slices and is wiped on instance stop, so "250 GB local
  storage" is implemented as a 250 GB boot disk. This was a deliberate
  choice discussed with the user.
- **Security model relies on GCP's VPC default-deny.** The VM sits on
  its own VPC with one ingress rule (TCP/22 from
  `network.ssh_source_ranges`) plus, only when `network.open_ports` is
  non-empty, a second rule (`<network>-allow-public`) opening those
  ports to `0.0.0.0/0` (added at user request 2026-09-25; entries are
  ports, `low-high` ranges, or `tcp:`/`udp:`-prefixed, parsed by
  `parse_open_ports` in `generate.py`). Do not attach the VM to the
  `default` network and do not add allow rules beyond these two
  without asking.
- **RAM guardrail** in `generate.py` parses predefined machine-type
  names (`<family>-standard|highmem|highcpu-<vcpus>`) and fails if the
  inferred RAM is below `vm.min_memory_gb` (default 64); it only warns
  for names it can't parse. Disk has a hard 250 GB floor.
- **The base OS is a `vm.os` preset** (rocky-10/9, almalinux-10/9,
  ubuntu-24.04/22.04, debian-13/12; table `OS_PRESETS` in
  `generate.py`, all family names verified against the live GCP API
  2026-09-25). A preset expands to `vm.image` + a default `vm.ssh_user`
  (the distro's conventional login); an explicit `ssh_user` overrides
  the default, and `vm.os` + `vm.image` together is an error. For
  unlisted images, set `vm.image.project`/`family` directly — with
  `vm.os` absent, behavior is exactly the pre-preset generator, which
  is what keeps the deployed VM's render byte-identical.
- **Image is a family reference** (e.g. `rocky-linux-cloud/rocky-linux-10`)
  so it tracks the latest release in the family automatically.
- SSH keys go in via instance metadata (`ssh-keys`), not OS Login. Keys
  for the base login are merged from three `vm.*` sources
  (`ssh_public_key` inline, `ssh_public_key_file` single-key file,
  `ssh_public_keys_file` multi-key file — no code default; the example
  config points it at `config/base_authorized_keys`), deduplicated, at
  least one required.
- **Extra external IPv4s use protocol forwarding, not extra NICs.**
  `vm.external_ip_count` (1–8, default 1) controls the total; the first
  is the NIC's ephemeral IP, the rest are reserved `google_compute_address`es
  sent to the same NIC via a `google_compute_target_instance` +
  per-IP `google_compute_forwarding_rule` (`L3_DEFAULT`, all ports).
  Chosen over multiple NICs because each extra NIC would need its own
  VPC plus guest policy routing, breaking the one-VPC security model.
  The VPC firewall filters forwarded traffic too, and the image's
  google-guest-agent auto-installs local routes for forwarded IPs.
  With the default count of 1, the rendered HCL has no forwarding
  resources at all.
- **Real extra NICs are a separate knob, `vm.nic_count`** (1–8, default
  1), for VMs that must show N interfaces in `ip a`. With count > 1 the
  VPC flips to `auto_create_subnetworks = false` with one
  `google_compute_subnetwork` (`10.10.<i>.0/24`) per NIC — GCP permits
  same-VPC multi-NIC when every NIC has a unique subnet and nic0 is on
  that VPC — so the one-VPC/one-firewall-rule security model holds.
  `policy-routing.sh` goes in as startup-script metadata so replies to
  traffic on secondary NICs use the right interface (the script must
  never contain a dollar-brace, or the HCL heredoc interpolates it).
  Changing nic_count replaces the VM and the network; with count 1 the
  render is identical to the single-NIC original. Orthogonal to
  external_ip_count (forwarded IPs always target nic0).

## Commands

```sh
# one-time setup
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# regenerate build/main.tf (always re-run after touching vm.yaml or the template)
.venv/bin/python generator/generate.py

# verify without deploying (this is the de facto test suite)
tofu -chdir=build fmt -check
tofu -chdir=build init -backend=false -input=false
tofu -chdir=build validate

# deploy
tofu -chdir=build init && tofu -chdir=build apply
```

There are no Python unit tests; verification is the generator's own
guardrails plus `tofu validate`. If you change validation logic, a quick
negative test is: point `--config` at a copy of `vm.yaml` with
`machine_type: e2-standard-8` and confirm it exits non-zero.

## Current state / known gaps (as of 2026-09-25)

- **Live config is untracked** (since 2026-09-25): everything in
  `config/` except `config/examples/` is gitignored. The deployed VM's
  actual `vm.yaml` and keys file exist only on the original author's
  machine (like the tofu state) and in git history before this change;
  the committed examples carry placeholder values. The deployed values
  that matter are recorded in the bullets below. Note: the author's
  local keys file still uses the pre-rename name
  `config/rocky_authorized_keys` (the setting is explicit in their
  `vm.yaml`, so it keeps working); the example was renamed to
  `base_authorized_keys` when OS presets landed.
- **`vm.os` presets have not been applied to a real VM.** The deployed
  VM predates them and its config sets `vm.image` directly (a no-op
  path through the preset code — verified byte-identical render).
  Likewise `setup0.sh`'s wheel/sudo group detection is untested on a
  live Debian-family VM (it has not been run anywhere yet, see below).
- **`network.open_ports` is unset on the deployed VM** — SSH remains
  its only open port, and no `-allow-public` rule has ever been
  applied (the knob passes `tofu validate` only). Adding the feature
  changed the rendered HCL for existing configs in comments only;
  `tofu plan` against the deployed state is a no-op.

- **The VM is deployed.** `gcp.project_id` is set to the real project
  (`orcd-dr`) and `tofu apply` has created the network, firewall rule,
  and instance. OpenTofu state lives only in `build/terraform.tfstate`
  on the original author's machine (local state, gitignored — there is
  no remote backend). From a fresh clone, do not `apply` without first
  recovering that state or importing the existing resources, or you
  will create duplicates.
- **The state file has already been lost and rebuilt once**
  (2026-09-23): `build/` was deleted, a later `apply` hit 409
  "already exists" errors, and the fix was `tofu import` of the
  network, firewall, instance, and target instance into a fresh state,
  followed by a clean apply. If you see 409s on apply, suspect missing
  state and import — never delete the GCP resources to "unblock".
- `vm.external_ip_count` is 4 and applied: the VM answers on its
  ephemeral NIC IP plus three reserved static IPs
  (`rocky10-vm-ip-2/3/4` via `rocky10-vm-fwd-2/3/4`). After apply, the
  extra IPs can take a minute or two to open for SSH while the guest
  agent installs routes.
- `vm.nic_count` is 1 on the deployed VM and must stay 1 there
  (changing it forces replacement). Multi-NIC has passed
  `tofu validate` but has **never been applied to a real VM** — the
  first 4-NIC VM (via a second config + `--out build2`) will be the
  real test, especially of `policy-routing.sh`.
- **Unmanaged stragglers exist in the project**: a hand-made
  `rocky10-vm-second-ip` (34.57.137.79) + `rocky10-vm-fwd-rule` predate
  the generated forwarding rules and give the VM a fifth external IP
  outside tofu's control. The target instance they share
  (`rocky10-vm-target`) IS imported/managed. Ask the user before
  deleting the pair — releasing the address is irreversible.
- Auth on the dev machine is working: gcloud CLI installed, ADC
  configured via `gcloud auth application-default login`. The provider
  block has no `credentials` field on purpose — it discovers ADC. See
  the README's "Google Cloud authentication" section, including the
  consent-checkbox gotcha. GCP "API keys" do not work for Compute
  Engine.
- `vm.ssh_public_key_file` points at `~/.ssh/gce_cf_key.pub`, which is
  specific to the original author's machine.
  `config/rocky_authorized_keys` holds two more keys (lincolnb,
  thekla); all three are deployed in the instance's ssh-keys metadata.
- Initial provisioning is a shell script, `provisioning/setup0.sh`
  (admin accounts lincolnb + tloizou, sudo via the detected sudo group
  — wheel on RHEL-family, sudo on Debian-family — + NOPASSWD
  drop-in, locked passwords so SSH keys are the only way in). It has
  **not been run yet** — it needs the users' public keys dropped into
  `provisioning/keys/<username>.pub` first, and it refuses to run
  without them.
- The repo is named `gcp-vm-plus-ansible`: **Ansible provisioning is
  the intended next phase but does not exist yet.** The tofu outputs
  (`public_ip`, `ssh_command`) were designed as the inventory inputs
  for it; setup0.sh is expected to be absorbed into Ansible eventually.

## Conventions

- **Keep the documentation up to date.** Any change to code,
  configuration, workflow, or deployment state must be reflected in
  `README.md` and this file in the same commit. Treat stale docs as a
  bug; refresh the "as of" date on the current-state section above
  whenever you revise it.
- Commit messages: meaningful summary + body, and the body must include
  "Assisted by AI." (user requirement). Do **not** add `Co-Authored-By`
  or other AI attribution trailers — the user has explicitly declined
  them (2026-09-25), and the full history was rewritten to remove them.
- Never commit `build*/`, `*.tfstate`, `.terraform/`, credential JSON
  files, or anything in `config/` outside `config/examples/` —
  `.gitignore` already covers these. Config changes meant for everyone
  belong in the `config/examples/` templates.
- License is MIT. Remote: https://github.com/christophernhill/gcp-vm-plus-ansible (public).
