# gcp-vm-plus-ansible

Python generator that renders OpenTofu configuration for a Rocky Linux 10
VM on GCP: 64 GB+ RAM, 250 GB boot disk, SSH open to the internet, all
other inbound ports closed (the VM sits on its own VPC, and GCP VPCs deny
ingress by default).

## Layout

```
config/vm.yaml               all tunable settings (project, zone, machine
                             type, disk, image, SSH key, allowed CIDRs)
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

Outputs after `apply`: the VM's public IP and a ready-to-paste `ssh` command.

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
