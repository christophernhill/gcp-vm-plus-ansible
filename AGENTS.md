# AGENTS.md

Context for agents picking up work on this repository.

## What this project is

A Python generator that renders OpenTofu HCL to create a single Rocky
Linux 10 VM on GCP. Hard requirements the code enforces:

- At least 64 GB RAM (default machine type: `n2-standard-16`)
- At least 250 GB of disk (a `pd-balanced` boot disk)
- SSH reachable from the open internet; **all other inbound ports closed**
- All key settings parameterized in one YAML file (`config/vm.yaml`)

The flow is: edit `config/vm.yaml` → run `generator/generate.py` (loads
YAML, validates, renders the Jinja2 template) → `build/main.tf` → deploy
with `tofu`.

## Layout

| Path | Role |
|---|---|
| `config/vm.yaml` | Single source of truth for all settings |
| `generator/generate.py` | Loads + validates YAML, renders the template |
| `generator/templates/main.tf.j2` | OpenTofu HCL template |
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
  its own VPC with exactly one ingress rule (TCP/22 from
  `network.ssh_source_ranges`). Do not attach it to the `default`
  network and do not add allow rules without asking.
- **RAM guardrail** in `generate.py` parses predefined machine-type
  names (`<family>-standard|highmem|highcpu-<vcpus>`) and fails if the
  inferred RAM is below `vm.min_memory_gb` (default 64); it only warns
  for names it can't parse. Disk has a hard 250 GB floor.
- **Image is a family reference** (`rocky-linux-cloud/rocky-linux-10`)
  so it tracks the latest Rocky 10 release automatically.
- SSH key goes in via instance metadata (`ssh-keys`), not OS Login.

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

## Current state / known gaps (as of 2026-09-22)

- **The VM is deployed.** `gcp.project_id` is set to the real project
  (`orcd-dr`) and `tofu apply` has created the network, firewall rule,
  and instance. OpenTofu state lives only in `build/terraform.tfstate`
  on the original author's machine (local state, gitignored — there is
  no remote backend). From a fresh clone, do not `apply` without first
  recovering that state or importing the existing resources, or you
  will create duplicates.
- Auth on the dev machine is working: gcloud CLI installed, ADC
  configured via `gcloud auth application-default login`. The provider
  block has no `credentials` field on purpose — it discovers ADC. See
  the README's "Google Cloud authentication" section, including the
  consent-checkbox gotcha. GCP "API keys" do not work for Compute
  Engine.
- `vm.ssh_public_key_file` points at `~/.ssh/gce_cf_key.pub`, which is
  specific to the original author's machine.
- The repo is named `gcp-vm-plus-ansible`: **Ansible provisioning is
  the intended next phase but does not exist yet.** The tofu outputs
  (`public_ip`, `ssh_command`) were designed as the inventory inputs
  for it.

## Conventions

- **Keep the documentation up to date.** Any change to code,
  configuration, workflow, or deployment state must be reflected in
  `README.md` and this file in the same commit. Treat stale docs as a
  bug; refresh the "as of" date on the current-state section above
  whenever you revise it.
- Commit messages: meaningful summary + body, and the body must include
  "Assisted by AI." (user requirement). AI-authored commits also carry a
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer.
- Never commit `build/`, `*.tfstate`, `.terraform/`, or credential JSON
  files — `.gitignore` already covers these.
- License is MIT. Remote: https://github.com/christophernhill/gcp-vm-plus-ansible (public).
