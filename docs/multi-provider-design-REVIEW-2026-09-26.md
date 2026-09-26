# Review: docs/multi-provider-design.md
**Date:** 2026-09-26  |  **Reviewer:** sub-agent (style + content pass)

## Summary
The document is in good shape: it explains motivation before mechanism, keeps its promises about the deployed VM front and center, and most of the prose already reads like textbook material rather than compressed notes. The remaining style problems are localized — a handful of sentence fragments used as section openers, one invented label ("the GCP triple"), one badly packed sentence in §3.2, and a few table cells that lean on the reader's prior knowledge. On content, the code-facing claims are almost all accurate against `generator/generate.py` and the templates; the two real errors are the claim that attached Elastic IPs are free (false since AWS began charging for all public IPv4 addresses in February 2024) and the claim that the §6 GCP column is "exactly today's README" (the offline-verify row comes from AGENTS.md, not the README). A third, smaller misstatement implies a committed test suite exists when AGENTS.md explicitly says there are no Python unit tests. Structure is sound; the main gap is that the "never point an AWS run at build/" rule is enforced only by documentation, and the design never considers adding a cheap mechanical safeguard.

## Style findings

1. **Medium — overloaded em-dash interruption in the goals sentence (§1).**
   > "The goal is to let the same YAML-driven generator deploy the same kind of VM — guardrailed memory and disk sizes, its own isolated network, nothing reachable from outside except SSH and an explicit list of open ports, several SSH keys merged for the base login, optionally several external IP addresses, optionally several network interfaces — on either GCP or AWS."

   A six-item noun list is wedged between the object and its prepositional phrase; the reader holds "deploy the same kind of VM" open for forty words before learning "on either GCP or AWS". Rewrite by stating the claim first, then unpacking: *"The goal is to let the same YAML-driven generator deploy the same kind of VM on either GCP or AWS. 'The same kind of VM' means everything the GCP path provides today: guardrailed memory and disk sizes, an isolated network that admits nothing but SSH and an explicit list of open ports, SSH keys merged from several sources for the base login, and — optionally — several external IP addresses or several network interfaces."*

2. **Medium — invented label "the GCP triple" (§3.2).**
   > "`REQUIRED_KEYS` asks for `aws.region` and `aws.availability_zone` instead of the GCP triple"

   "The GCP triple" is exactly the private-shorthand pattern the document should avoid: the reader must scroll back and reconstruct which three keys are meant. Rewrite: *"…instead of GCP's three keys (`gcp.project_id`, `gcp.region`, `gcp.zone`)"*.

3. **Medium — one sentence carrying six compressed claims, plus an undefined term (§3.2).**
   > "`REQUIRED_KEYS` asks for … instead of the GCP triple, `OS_PRESETS` maps the same preset names to AMI lookups (§5), `machine_ram_gb` uses static tables (§4.1), `MAX_EXTERNAL_IPS` and `MAX_NICS` carry AWS-specific limits and comments, `validate` adds AWS advisories, and `render_context` returns the cloud-init `user_data` text."

   Six exports, six comma-spliced clauses, each in shorthand ("carry AWS-specific limits and comments", "adds AWS advisories"). "Advisories" is never defined — the reader meets it again in the example config ("advisory, not enforced") and has to infer it means a warning that does not stop generation. Convert to a bulleted list, one export per bullet, and define the term at first use: *"`validate` adds AWS-specific advisories — warnings printed to the user that do not stop generation, such as the ENI-count caution described in §4."*

4. **Medium — pattern: sentence fragments as section and list openers.** The brief calls these out specifically; four instances remain:
   - §8: "Five phases, each one commit, each updating the documentation in the same commit (a repo convention)." → *"The migration proceeds in five phases. Each phase is one commit, and each commit updates the documentation alongside the code, as this repository's convention requires."*
   - §4.3: "Built by `providers/aws.py:render_context()` from the merged key list." → *"This script is built by `providers/aws.py:render_context()` from the merged key list."*
   - §7: "**Boot disk of at least 250 GB.** A plain shared check." → *"…This check involves nothing provider-specific, so it runs unchanged in the shared core."*
   - §5: "…which the template can read with a `data \"aws_ssm_parameter\"` block: always current, no account setup needed." → *"…block; the parameter always holds the current release, and reading it requires no account setup."*

5. **Low — compressed parentheticals in the routing summary (§4.1).**
   > "finding the device by MAC under `/sys/class/net`, one routing table per interface (`100+i`), one source rule per address (priority `10000+i`)"

   "(100+i)" is a bare formula with no noun telling the reader what it numbers. Rewrite: *"…creating one routing table per interface (numbered 100 + i) and one source-address rule per interface (at priority 10000 + i)"*.

6. **Low — pattern: table cells that assume context the prose never supplies.**
   - §4, Instance row: "`aws_instance`; the AMI comes from a data source (§5); `instance_type`; `root_block_device { volume_size, volume_type }`" — the last two items are bare identifiers with no verb. → *"`aws_instance`: the AMI comes from a data source (§5), the machine type goes into `instance_type`, and the disk settings into a `root_block_device` block."*
   - §6 behavioral table: "The default `build/` directory | is the deployed GCP VM — never point an AWS run at it" — the row label and cell form a split sentence, and the directory *is* not the VM. → *"holds the deployed GCP VM's configuration and state — never point an AWS run at it."*
   - §5 table: "`ec2-user` (verify)" — the parenthetical is cryptic until §9; append a footnote-style pointer, e.g. "`ec2-user` (unconfirmed — §9 item 2)".

7. **Low — ambiguous "flat files" (§3.3).**
   > "The committed examples stay flat files in `config/examples/`"

   "Flat files" usually means "not a database"; here it means "not sorted into per-provider subdirectories". Rewrite: *"The committed examples stay side by side in `config/examples/` rather than moving into per-provider subdirectories."*

8. **Low — the README bullet in §10 is one sentence with eight semicolon-joined clauses.** It is a specification list wearing a sentence's clothing; convert the clauses to sub-bullets so each documentation change can be checked off during phase 5.

## Content findings

1. **High — the Elastic IP pricing claim is wrong and is not covered by §9 (§4.1).**
   > "each Elastic IP that is attached to a running instance is free, but the quota still binds"

   This was AWS's pricing model until February 1, 2024. Since then AWS charges for **all** public IPv4 addresses — attached Elastic IPs and auto-assigned public IPs alike (about $0.005/hour each, ~$3.60/month). The old attached-EIP-is-free rule no longer exists. Correct to: *"since February 2024 AWS charges for every public IPv4 address, attached or not (about $0.005 per hour each), so extra external IPs carry a per-address cost on AWS that GCP static IPs share in different form — and the five-per-region quota binds independently of cost."* Consider a one-line cost note in the §6 behavioral table too.

2. **Medium — "The GCP column is exactly today's README" is not true for the Verify row (§6).**
   > "Verify (offline) | `tofu -chdir=build fmt -check && tofu -chdir=build init -backend=false -input=false && tofu -chdir=build validate`"

   Those commands do not appear anywhere in `README.md`; they come from `AGENTS.md` (lines 115–118, labeled "the de facto test suite"). Every other row does match the README (auth, `compute.googleapis.com`, copy-the-example, generate, deploy, second VM, provisioning, in-place key rotation). Fix by softening the claim — *"The GCP column summarizes today's README, plus the offline verification sequence from AGENTS.md"* — or by moving the verify commands' attribution into the row.

3. **Medium — §8 phase 1 implies a committed test suite exists.**
   > "plus the existing negative tests (an undersized machine type, missing keys) failing exactly as before"

   AGENTS.md states plainly: "There are no Python unit tests; verification is the generator's own guardrails plus `tofu validate`," followed by a *manual* negative-test recipe. Rewrite: *"plus the manual negative checks documented in AGENTS.md (an undersized machine type, missing keys) still failing exactly as before."*

4. **Low/medium — "exactly two firewall rules" overstates (§7).**
   > "On GCP this is a dedicated VPC with exactly two firewall rules"

   `generator/templates/main.tf.j2` renders `allow_public` only when `open_ports` is non-empty (the `{% if open_ports.tcp or open_ports.udp %}` guard), so the deployed count is one or two. The document itself says this correctly in §4 ("only when `open_ports` is set"). Rewrite: *"a dedicated VPC with at most two firewall rules — SSH, plus one for `open_ports` when any are configured."*

5. **Low — the target instance is not per-extra-address (§4).**
   > "a reserved `google_compute_address` plus a `google_compute_target_instance` and a `google_compute_forwarding_rule` per extra address"

   In the template, `google_compute_target_instance.vm` is a single resource with no `count`; only the address and forwarding rule are per-extra. Rewrite: *"one shared `google_compute_target_instance`, plus a reserved `google_compute_address` and a `google_compute_forwarding_rule` per extra address."*

6. **Low — `vm.boot_disk_type` (and `vm.image`) classified as GCP-only required keys (§3.2).** The GCP provider sketch moves `("vm", "image")` and `("vm", "boot_disk_type")` out of the core list, but the AWS example config requires `boot_disk_type: gp3` and the AWS template renders `volume_type` unconditionally — AWS needs both keys too. Either keep the *presence* checks in the core list (with only the *shape* checks per provider), or state the full AWS `REQUIRED_KEYS` so the reader isn't left inferring it.

7. **Low — internal inconsistency in the §4.2 sketch: `vpc_cidr` is configurable but subnets are hardcoded.** The VPC uses `{{ aws.vpc_cidr | default('10.10.0.0/16') }}`, yet the subnets render literal `10.10.${count.index}.0/24`; a user who overrides `vpc_cidr` gets subnets outside their VPC. Since §3.3 promises subnets "carved" from `vpc_cidr`, the sketch should use `cidrsubnet()` (or the design should drop the `vpc_cidr` knob). Related small nit: `data.aws_ssm_parameter.value` is marked sensitive by the AWS provider, so plans will show `ami = (sensitive value)`; worth a line in §9.

8. **Low — the illustrative memory table's `"x": 8` is materially wrong (§4.1).** AWS x-family instances run at roughly 16–30 GB per vCPU (e.g., x1e.xlarge is 4 vCPU / 122 GB), so the table would *reject* machines that comfortably clear the floor — a false failure, not a safe underestimate. §9 item 4 does flag the tables for verification, so this is acceptable as an open item, but since the doc presents the table as a concrete sketch, correcting or removing `"x"` now would be cheap.

9. **Low — `OS_PRESETS` described as mapping to "a GCP image project and family" (§2).** Each preset in `generate.py` is a triple that also carries the default `ssh_user` — and the `ssh_user` element is exactly the part that recurs on AWS (§5's table has a default-user column), so it is worth naming: *"each preset maps to a GCP image project, an image family, and a default login user."* The §3.2 sketch already shows the triple, so this is a one-word-class fix.

**Verified fine (no action):** the provider-neutral/GCP-specific split matches the code (`load_config`, `fail`, `example_hint`, `resolve_ssh_public_keys` with its three sources in the stated order, `parse_open_ports` grammar, the 250 GB and 64 GB floors, the `MAX_*` values, the `nic_count <= vCPUs` check, the `my-gcp-project` warning, `StrictUndefined`, `example_hint`'s filename-based `cp` suggestion, the gitignore rules); `setup0.sh` contains nothing cloud-specific and `capture.sh`'s only GCP dependency is `/etc/google_instance_id` with the `/etc/machine-id` fallback, exactly as §2 and §9.7 say; the GCP metadata snippet matches `policy-routing.sh` line for line. On the AWS side: IMDSv2 token mechanics and header names, the by-MAC interface index, the gateway-not-exposed / first-host-address convention, the single-key `aws_key_pair` limitation, the default quota of five EIPs per region, security-group statefulness, the Canonical/Debian SSM parameter approach (gp2 for 22.04, gp3 for 24.04 — plausibly right, and flagged "indicative"), the Debian `admin` user, the Marketplace opt-in failure on first apply, m5.4xlarge at 16 vCPU / 64 GB with 8 ENIs / 30 addresses, and `user_data_replace_on_change` are all correct or appropriately parked in §9.

## Structure findings

1. **Order is right.** Compatibility contract first, then goals, then a classification of what exists, then architecture, then the mapping table and the four deep differences, then workflow, roadmap, open questions. Motivation consistently precedes mechanism.
2. **Redundancy:** the "guardrails run in shared code, so a provider module cannot weaken them" point appears nearly verbatim in §3.2 and §7. Keep §7, but open it with "As §3.2 explained, …" and trim, or recast §7 as an explicit summary checklist.
3. **Missing safeguard:** the danger the doc most emphasizes — an AWS run pointed at the default `--out build`, which holds the live GCP VM's state — is mitigated only by convention ("never point an AWS run at it"). The generator could refuse to render provider X into a directory whose existing `main.tf` was generated for provider Y (the header comment makes this a one-line check). This deserves a paragraph in §3 or a §9 item.
4. **Missing minor topics:** AWS public-IPv4 cost (falls out of content finding 1) and a one-line statement that teardown (`tofu destroy`) is symmetric on both providers.

## What works well
The two-promise compatibility contract at the top does real work — every later section can be checked against it. Naming functions instead of line numbers in the §2 tables is a genuinely good durability decision, and the classification itself is accurate. The §4 mapping table plus the "four places where AWS genuinely differs" section is the strongest teaching material in the document: each difference states the GCP mechanism, the AWS translation, and the easy-to-miss consequence (the in-guest secondary-IP configuration especially). The argument for modules-over-classes and one-template-per-provider gives reasons, not just verdicts. §9 is honest about what is unverified and ties each open item to a phase. Preserve all of this while fixing the findings above.
