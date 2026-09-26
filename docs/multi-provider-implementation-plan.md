# Implementation plan: multi-provider support (AWS alongside GCP)

**Date:** 2026-09-26 | **Source design:** `docs/multi-provider-design.md`

This plan turns the design document into an ordered sequence of changes.
It is written for an implementing agent to follow top to bottom. The
design document remains the authority on *why*; this plan is the *what,
where, and in which order*. The §11 side tool (image query) is out of
scope here.

## Ground rules (read first, apply to every phase)

1. **Never run `tofu apply` or `tofu destroy` against `build/`.** That
   directory holds the deployed GCP VM's state. `tofu -chdir=build plan`
   (read-only) is allowed and required.
2. **Never edit `config/vm.yaml`, `config/vm2.yaml`, or
   `config/rocky_authorized_keys`.** They are the user's live, gitignored
   working config. Committed config changes go only in `config/examples/`.
3. **Never `git add -A`.** The working tree contains private untracked
   material (`provisioning/mmm.txt`, `vm-dir/`,
   `vm-dir-ansible-analysis.md`). Stage files explicitly.
4. **The back-compat check runs at the end of every phase** and must
   pass before committing:

   ```sh
   .venv/bin/python generator/generate.py --out /tmp/bc-check
   diff /tmp/bc-check/main.tf build/main.tf        # must be empty
   tofu -chdir=build plan -input=false             # must report no changes
   ```

5. **One commit per phase**, documentation updated in the same commit
   (README.md and AGENTS.md, refreshing AGENTS.md's current-state "as
   of" date). Commit bodies must include the line `Assisted by AI.` and
   must NOT carry `Co-Authored-By` or any other attribution trailer —
   this repo explicitly forbids them.
6. **Test configs** are built by copying `config/examples/vm.yaml` (or
   `vm-aws.yaml`) to `/tmp` and pointing `ssh_public_keys_file` at a
   dummy key file:

   ```sh
   printf 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICWvM2hHmwSDC+jJpDoI8sYw7Q6f90eBv4hbNAJbgEkK dummy@test\n' > /tmp/test-keys
   sed 's|ssh_public_keys_file: config/base_authorized_keys|ssh_public_keys_file: /tmp/test-keys|' config/examples/vm.yaml > /tmp/test-vm.yaml
   ```

7. **Offline tofu verification** per output directory, with a plugin
   cache so the AWS provider downloads once:

   ```sh
   export TF_PLUGIN_CACHE_DIR=/tmp/tf-plugin-cache && mkdir -p "$TF_PLUGIN_CACHE_DIR"
   tofu -chdir=<dir> fmt -check
   tofu -chdir=<dir> init -backend=false -input=false
   tofu -chdir=<dir> validate
   ```

8. **AWS credentials are not required for phases 1–3.** `tofu validate`
   does not contact AWS. Phase 4 requires real credentials and is gated
   on them; if none are available, stop after phase 3 and record that in
   AGENTS.md.

---

## Phase 1 — Extract the provider interface (GCP only, byte-identical)

Goal: pure refactor. Nothing about behavior or output changes.

### Steps

1. `git mv generator/templates/main.tf.j2 generator/templates/gcp/main.tf.j2`
   and `git mv generator/templates/policy-routing.sh generator/templates/gcp/policy-routing.sh`.
   Do not change a byte of either file — in particular do not touch the
   GCP template's header comment (byte-identical output depends on it).
2. Create `generator/providers/__init__.py`, empty.
3. Create `generator/providers/gcp.py`. Move these from `generate.py`,
   unchanged in behavior: `OS_PRESETS` (all 12 presets), `MACHINE_TYPE_RE`,
   `GB_PER_VCPU`, `MAX_EXTERNAL_IPS`, `MAX_NICS`, `apply_os_preset`, the
   image-shape check, the nic_count ≤ vCPUs check, and the
   `my-gcp-project` placeholder warning. The module surface:

   ```python
   NAME = "gcp"
   TEMPLATE_SUBDIR = "gcp"
   REQUIRED_KEYS = [("gcp", "project_id"), ("gcp", "region"), ("gcp", "zone")]
   OS_PRESETS = { ... }          # moved verbatim
   MAX_EXTERNAL_IPS = 8
   MAX_NICS = 8

   def apply_os_preset(cfg) -> None: ...
   def machine_ram_gb(machine_type) -> int | None: ...   # None = "cannot tell"
   def validate(cfg) -> None: ...                        # GCP-only checks + warnings
   def render_context(cfg) -> dict: ...                  # {"startup_script": <policy-routing.sh text>}
   ```

   Error-reporting convention (avoids a circular import of `fail`):
   provider functions raise `ValueError(message)` for fatal problems and
   `print(..., file=sys.stderr)` for warnings. `render_context` computes
   its own template path:
   `Path(__file__).resolve().parent.parent / "templates" / TEMPLATE_SUBDIR`.
4. Rework `generate.py`:
   - Keep, unchanged: `fail`, `example_hint`, `load_config`,
     `resolve_ssh_public_keys`, `PUBLIC_KEY_RE`, `parse_open_ports`,
     `OPEN_PORT_RE`, `key_file_path`.
   - Add the registry and lookup:

     ```python
     from providers import gcp
     PROVIDERS = {"gcp": gcp}

     def get_provider(cfg):
         name = cfg.get("provider", "gcp")
         if name not in PROVIDERS:
             fail(f"unknown provider {name!r}; valid: " + ", ".join(sorted(PROVIDERS)))
         return PROVIDERS[name]
     ```

     (Phase 1 does not advertise the `provider:` key anywhere; the
     lookup simply defaults to gcp.)
   - Core `REQUIRED_KEYS` shrinks to what both clouds need:
     `("network","name")`, `("network","ssh_source_ranges")`,
     `("vm","name")`, `("vm","machine_type")`, `("vm","boot_disk_gb")`,
     `("vm","boot_disk_type")`, `("vm","image")`, `("vm","ssh_user")`.
   - `validate(cfg, provider)` runs, in order: provider.apply_os_preset
     (wrapped in `try/except ValueError as e: fail(str(e))` — same
     wrapper for every provider hook); the required-keys loop over core
     list + `provider.REQUIRED_KEYS`; the RAM guardrail via
     `provider.machine_ram_gb()` (keep the exact current failure and
     warning message texts); the 250 GB disk floor; the
     `external_ip_count`/`nic_count` bounds against `provider.MAX_*`
     (keep message texts); `provider.validate(cfg)`; the
     `ssh_source_ranges` check; `parse_open_ports`.
   - `render(cfg, out_dir, provider)` loads
     `f"{provider.TEMPLATE_SUBDIR}/main.tf.j2"` from the existing
     `FileSystemLoader(TEMPLATE_DIR)` and renders with
     `**cfg, ssh_public_keys=..., open_ports=..., **provider.render_context(cfg)`.
     The GCP `render_context` supplies `startup_script`, so the final
     Jinja context is identical to today's.
   - `main()` calls `get_provider` after `load_config` and threads it
     through.
5. Run the verification below; commit.

### Phase 1 verification

- The back-compat check (ground rule 4). This is the whole point of the
  phase: the moved template plus relocated code must reproduce
  `build/main.tf` exactly.
- Render `/tmp/test-vm.yaml` and run the offline tofu checks.
- Negative tests, each must exit non-zero with the same clean one-line
  error as before: `machine_type: e2-standard-8` (RAM guardrail);
  `os: fedora-42` (unknown preset, lists all 12); `os:` plus an `image:`
  block (mutual exclusion); `image:` missing `family` (shape error).

Commit summary: `Extract provider interface; GCP becomes providers/gcp.py`.

---

## Phase 2 — The `provider:` key, the AWS example, and the safeguard

Goal: configs can say which provider they mean; AWS is named but not yet
implemented; the cross-provider output-directory mistake becomes
impossible.

### Steps

1. In `get_provider`, give AWS a deliberate stub:

   ```python
   if name == "aws":
       fail("provider 'aws' is not implemented yet (see docs/multi-provider-design.md)")
   ```

2. Add the commented line `# provider: gcp` near the top of
   `config/examples/vm.yaml` (comment only — an active key would be fine
   too, but a comment keeps the file's diff minimal and the default
   documented).
3. Create `config/examples/vm-aws.yaml` with exactly the worked example
   from design §3.3, including its header comment about copying to
   `config/vm-aws.yaml` and generating with
   `--config config/vm-aws.yaml --out build-aws`.
4. Implement the output-directory safeguard in `generate.py`:
   - Each provider template's first line may carry a marker of the form
     `provider: <name>` inside the header comment. The existing GCP
     template has **no** marker and must not gain one (byte-identical
     rule), so the check treats a `main.tf` without a marker as GCP.
   - Before writing, if `<out>/main.tf` exists: read its first line;
     determine its provider (marker value, else `gcp`); if that differs
     from the selected provider, `fail` with a message naming both
     providers and the directory, and mentioning the override flag.
   - Add `--force` to argparse to skip the check.
5. Documentation: update README's "Multiple providers (design)" section
   (the `provider:` key now parses; AWS still fails cleanly; example
   file exists) and AGENTS.md's current-state bullet.
6. Verify; commit.

### Phase 2 verification

- Back-compat check (ground rule 4).
- A test config with an explicit `provider: gcp` line renders
  byte-identically to one without it.
- `provider: aws` produces the exact "not implemented yet" error;
  `provider: azure` produces the unknown-provider error listing `gcp`.
- Safeguard: render a GCP config into `/tmp/sg-test`; overwrite
  `/tmp/sg-test/main.tf`'s first line with
  `# Generated by generator/generate.py — provider: aws — do not edit by hand.`;
  rendering GCP into that directory must fail; with `--force` it must
  succeed. Rendering GCP over a legacy (marker-less) `main.tf` must
  succeed without `--force` — this is what keeps `build/` usable.
- `yaml.safe_load` parses `config/examples/vm-aws.yaml`.

Commit summary: `Recognize provider: key; add AWS example and output-dir safeguard`.

---

## Phase 3 — The AWS provider, validated but never deployed

Goal: `provider: aws` configs render real HCL that passes
`tofu validate` for every preset. No AWS account is contacted.

### Steps

1. Create `generator/providers/aws.py` with the same surface as gcp.py:
   - `NAME = "aws"`, `TEMPLATE_SUBDIR = "aws"`.
   - `REQUIRED_KEYS = [("aws", "region"), ("aws", "availability_zone")]`.
   - `vm.image` on AWS accepts exactly one of three shapes, and
     `validate` enforces that: `{ami_id: ...}`, `{ssm_parameter: ...}`,
     or `{ami_owner: ..., ami_name_filter: ...}`.
   - `OS_PRESETS` maps preset name → (image dict, default ssh_user),
     using the design §5 table: `ubuntu-24.04`, `ubuntu-22.04`,
     `debian-13`, `debian-12`, `amazon-linux-2023` via `ssm_parameter`;
     `rocky-10/9`, `almalinux-10/9`, `centos-stream-10/9`,
     `fedora-44/43` via `ami_owner` + `ami_name_filter`. The SSM paths
     and owner IDs come from design §5/§9 and are **unverified**: put
     the doc's indicative values in, mark each unverified entry with a
     trailing comment `# unverified - see design doc §9`, and leave
     final confirmation to phase 4 step 1. Do not silently invent owner
     IDs — where the design has none, use a placeholder that
     `validate` rejects with "preset X needs verification (design §9)"
     so nobody can deploy it accidentally.
   - `machine_ram_gb`: parse `^([a-z])[a-z0-9]*\.([a-z0-9]+)$`; family
     letter → GB-per-vCPU table `{"m": 4, "t": 4, "r": 8, "c": 2}`;
     size word → vCPU table (`large`=2 doubling up to `24xlarge`=96);
     anything unmatched returns `None` (core warns).
   - `MAX_EXTERNAL_IPS = 5` (the default EIP quota — revisit in phase
     4), `MAX_NICS = 8`.
   - `validate` additionally prints a stderr advisory when
     `nic_count > 1` (ENI limits depend on the instance type) and
     rejects SSH keys containing `${` (they travel inside an HCL
     heredoc).
   - `render_context(cfg)` returns `{"user_data": <text>}`: a bash
     cloud-init script that writes the merged `authorized_keys` for
     `vm.ssh_user` (creating the user if absent), and — only when
     `nic_count > 1` or `external_ip_count > 1` — installs and enables
     a small systemd unit built from
     `templates/aws/policy-routing.sh` (secondary-IP plumbing and
     source-based routing via IMDSv2, per the design §4.1 snippet).
     `$var` only, never `${var}`.
2. Create `generator/templates/aws/policy-routing.sh`: the GCP script
   with the metadata queries replaced by the IMDSv2 sequence in design
   §4.1 (token PUT, MAC listing, `local-ipv4s`,
   `subnet-ipv4-cidr-block` with gateway = first host address); the
   `/sys/class/net` MAC lookup and the `ip route`/`ip rule` logic are
   copied unchanged, including the header comment about `${...}`.
3. Create `generator/templates/aws/main.tf.j2`. First line:
   `# Generated by generator/generate.py — provider: aws — do not edit by hand.`
   Required content (design §4.2 is the sketch; this is the contract):
   - `terraform` block pinning `hashicorp/aws ~> 6.0`; `provider "aws"`
     with region and optional profile
     (`{% if aws.profile is defined %}`).
   - `aws_vpc` (`{{ aws.vpc_cidr | default('10.10.0.0/16') }}`),
     `aws_internet_gateway`, `aws_route_table` with the default route,
     one `aws_route_table_association` per subnet, `aws_subnet` ×
     `vm.nic_count` using `cidrsubnet(aws_vpc.vpc.cidr_block, 8, count.index)`
     and `map_public_ip_on_launch = true`.
   - One `aws_security_group`: SSH ingress from
     `network.ssh_source_ranges`; ingress blocks derived from
     `open_ports` — note GCP-format port strings ("80", "8000-8100")
     must become `from_port`/`to_port` integers, so have
     `render_context` also supply a structured
     `open_port_rules: [{proto, from, to}, ...]` list rather than
     splitting strings in Jinja; allow-all egress.
   - AMI resolution matching the three image shapes:
     `data "aws_ssm_parameter"` when `vm.image.ssm_parameter` is
     defined, `data "aws_ami"` (most_recent, owners, name filter) when
     `vm.image.ami_owner` is defined, else the literal
     `vm.image.ami_id`.
   - `aws_instance` with `instance_type`, `root_block_device
     {volume_size, volume_type}`, `user_data` heredoc (indent filter,
     as the GCP template does for startup-script),
     `user_data_replace_on_change = true`, and `tags = {Name = vm.name}`.
   - NIC wiring: when `nic_count == 1` and `external_ip_count == 1`,
     inline `subnet_id` + `vpc_security_group_ids`. When either count
     exceeds 1, declare explicit `aws_network_interface` resources (the
     primary carries `private_ip_list`/secondary IPs for
     `external_ip_count - 1` extras), attach via `network_interface`
     blocks with `device_index`, and create one `aws_eip` +
     `aws_eip_association` per extra address and per secondary ENI.
   - Outputs `public_ip`, `public_ips`, `ssh_command` with the same
     names and meanings as the GCP template.
   - Remember `StrictUndefined`: every variable the template touches
     must come from the config or `render_context`.
4. Register it: `PROVIDERS = {"gcp": gcp, "aws": aws}`; delete the
   phase-2 stub branch.
5. Documentation: README's multi-provider section now says AWS renders
   and validates but has never been deployed; AGENTS.md current-state
   likewise; note which presets remain unverified.
6. Verify; commit.

### Phase 3 verification

- Back-compat check (ground rule 4) — GCP output must be untouched by
  all of this.
- Render matrix: for every AWS preset that has real (non-placeholder)
  image values, copy `vm-aws.yaml` to `/tmp`, set `os:` to the preset,
  point the keys file at the dummy key, render to its own out dir, and
  run the offline tofu checks. Every preset must pass `fmt -check`,
  `init -backend=false`, `validate`.
- Feature matrix: one render each with `open_ports: [80, "udp:53"]`,
  `external_ip_count: 3`, `nic_count: 2`, and all three combined — all
  must validate.
- Negative tests (non-zero exit, clean message, no traceback):
  `machine_type: c5.xlarge` with `min_memory_gb: 64` (8 GB, fails);
  `machine_type: weird.metal` (warns, proceeds); `boot_disk_gb: 100`;
  unknown preset (error lists the AWS preset names, including
  `amazon-linux-2023`); `os:` + `image:` together; `image:` with only
  `ami_owner` (shape error); an unverified-placeholder preset (refused
  with the §9 pointer).
- Safeguard cross-check: rendering an AWS config into a directory
  holding a GCP `main.tf` fails without `--force`, and vice versa.

Commit summary: `Add AWS provider and template (validate-only)`.

---

## Phase 4 — First real AWS deployment (requires credentials)

**Gate:** working AWS credentials (`aws sts get-caller-identity`
succeeds) and the user's go-ahead, since this creates billable
resources. Use a scratch account/region; everything here happens in
`build-aws*`, never `build/`.

### Steps

1. Resolve the design's §9 open items with the real APIs, and update
   `providers/aws.py` and design-doc §5/§9 with the answers:
   - SSM paths: `aws ssm get-parameter --name <path> --region us-east-1`
     for each Ubuntu/Debian/Amazon Linux path.
   - Marketplace/community AMIs:
     `aws ec2 describe-images --owners <candidate> --filters "Name=name,Values=<pattern>" --query 'Images[0].{id:ImageId,name:Name}'`
     for Rocky, Alma, CentOS Stream, Fedora; record owner IDs, name
     patterns, and whether a Marketplace subscription is needed.
   - Default login users: boot one instance per unverified distro (or
     consult the AMI's documentation) and record the user.
   - ENI/secondary-IP limits:
     `aws ec2 describe-instance-types --instance-types m5.4xlarge --query 'InstanceTypes[0].NetworkInfo'`;
     set `MAX_NICS`/`MAX_EXTERNAL_IPS` accordingly.
2. Deploy the example config: copy `vm-aws.yaml` to `config/` (local,
   gitignored), generate to `build-aws`, `tofu init && tofu apply`.
   Confirm: SSH as the preset's default user with a key from the merged
   list; a second key from the list also works; `open_ports` behaves
   (one TCP port reachable, others closed).
3. Exercise the hard features on a second config (`--out build-aws2`):
   `external_ip_count: 3` (all three addresses answer SSH after boot)
   and `nic_count: 2` (SSH reachable on both NICs — first live test of
   the IMDSv2 policy-routing script).
4. Key-rotation check: add a key, regenerate, `tofu plan` — confirm the
   plan replaces the instance (per `user_data_replace_on_change`), and
   document that behavior where README describes rotation.
5. `capture.sh`: add the AWS first-boot anchor — prefer
   `/etc/google_instance_id`, then the mtime of
   `/var/lib/cloud/instances/<instance-id>` (cloud-init), then
   `/etc/machine-id`. Run it once on the AWS VM; fix what looks wrong.
6. Tear everything down (`tofu -chdir=build-aws destroy`, etc.) and
   confirm with `aws ec2 describe-instances` that nothing is left.
7. Documentation: fold every phase-4 answer back into design §5/§9,
   README caveats, AGENTS.md current-state ("AWS deployed and torn down
   on <date>; presets verified").
8. Commit.

Commit summary: `Verify AWS provider against a live account; fix presets`.

---

## Phase 5 — Documentation restructure

Design §10 is the specification. In one commit:

1. README.md: intro says "a VM on GCP or AWS"; Layout gains
   `generator/providers/` and `templates/{gcp,aws}/`; Usage shows both
   copy-the-example flows; "Choosing the OS" becomes a two-column
   preset table (GCP and AWS values side by side, Amazon Linux marked
   AWS-only); new "Choosing the provider" section; "on AWS" paragraphs
   in the extra-IP and multi-NIC sections; "Google Cloud
   authentication" becomes "Authentication" with one subsection per
   provider; the use-mode table from design §6 lands as a summary.
2. AGENTS.md: layout rows; design-decision bullets (registry defaulting
   to gcp, one template per provider, "same key, provider vocabulary",
   guardrails in core, the output-dir safeguard); current-state
   updated.
3. `docs/multi-provider-design.md`: flip the status header from "design
   only" to implemented, and update §9 to reflect what phase 4
   resolved.
4. Run the back-compat check one last time; commit.

Commit summary: `Restructure docs for two providers`.

---

## Definition of done

- [ ] `generator/providers/{__init__,gcp,aws}.py` exist; `generate.py`
      contains no provider-specific constants or checks.
- [ ] `generator/templates/gcp/` holds the moved files, byte-identical;
      `generator/templates/aws/` holds the new template and routing
      script.
- [ ] A config without `provider:` renders exactly as before the work
      started: `diff` against `build/main.tf` empty, `tofu plan` no-op.
- [ ] `config/examples/vm-aws.yaml` committed; `config/vm*.yaml` and
      the user's local files untouched.
- [ ] Every AWS preset renders and passes
      `fmt -check`/`init -backend=false`/`validate`; feature and
      negative matrices pass.
- [ ] The output-directory safeguard refuses cross-provider renders,
      honors `--force`, and treats marker-less `main.tf` files as GCP.
- [ ] Phase 4 either completed with §9 answers folded back into the
      design doc, or explicitly recorded in AGENTS.md as blocked on
      credentials.
- [ ] README.md and AGENTS.md describe both providers per design §10;
      the design doc's status header reflects reality.
- [ ] Five commits (or four plus a recorded phase-4 block), each with
      docs in the same commit, `Assisted by AI.` in the body, and no
      attribution trailers.
