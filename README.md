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
build/                       generated main.tf lands here (gitignored)
```

## Usage

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# edit config/vm.yaml (at minimum: gcp.project_id and the SSH key)
.venv/bin/python generator/generate.py
tofu -chdir=build init
tofu -chdir=build apply
```

The generator enforces the guardrails: it fails if the machine type has
less than `vm.min_memory_gb` (default 64) or the boot disk is under 250 GB.

Outputs after `apply`: the VM's public IP and a ready-to-paste `ssh` command.
