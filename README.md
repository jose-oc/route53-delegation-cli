# Route 53 Delegation CLI

`route53-delegation-cli` is a small Python tool for preparing selective Route 53 subdomain delegation from a large parent public hosted zone.

The CLI now covers the main migration phases:

1. inventory records for selected subdomains
2. generate a reviewable TTL-reduction plan
3. reduce TTLs for eligible records, with dry-run as the default
4. create child hosted zones
5. populate child hosted zones
6. add parent-zone `NS` delegations
7. clean up migrated records from the parent zone
8. export one target subtree as a BIND-style zone file
9. verify live delegation with `dig`
10. roll back TTL, delegation, and parent records if needed

## What the Tool Does

The CLI is manifest-driven. You define:

- the parent hosted zone
- the subdomains you want to prepare for delegation
- the pre-cutover TTL you want for each target

The tool then:

- reads the current Route 53 state from AWS
- writes YAML artifacts locally for review
- only changes TTLs when you explicitly use `--apply`
- uses dry-run by default for every mutating step

Command inputs are intentionally split by source of truth:

- use `manifest` when the command defines or discovers the intended migration scope
- use `inventory`, `plan`, or prior result artifacts when the command should follow already captured state even if the manifest later drifts

## Requirements

- Python `3.11` or newer
- AWS credentials with Route 53 read access for `inventory` and `plan`
- AWS credentials with Route 53 write access for `reduce-ttl --apply`, `create-child-zones --apply`, `populate-child-zones --apply`, `delegate-subdomains --apply`, `cleanup-parent --apply`, `restore-ttl --apply`, `undelegate-subdomains --apply`, and `restore-parent-records --apply`
- `uv` recommended for local development and test runs

## Build and Install

### Option 0: Install from a Release Wheel

If you have downloaded a release `.whl` file from GitHub, you can install the tool without cloning this repository.

Example:

```bash
python3 -m pip install route53_delegation_cli-0.1.0-py3-none-any.whl
```

After installation, run:

```bash
route53-delegation --help
```

If you want to upgrade an existing installation from a newer wheel:

```bash
python3 -m pip install --upgrade route53_delegation_cli-0.1.0-py3-none-any.whl
```

If you prefer installing into an isolated virtual environment:

```bash
python3 -m venv route53-delegation-venv
source route53-delegation-venv/bin/activate
python3 -m pip install route53_delegation_cli-0.1.0-py3-none-any.whl
route53-delegation --help
```

### Option 1: Install with `uv`

From this directory:

```bash
uv sync
```

Then run the installed CLI with:

```bash
uv run route53-delegation --help
```

### Option 2: Install with `pip`

From this directory:

```bash
python3 -m pip install .
```

Then run:

```bash
route53-delegation --help
```

## Development Mode

For development, use an editable install so code changes are picked up without reinstalling the package.

### Editable install with `pip`

```bash
python3 -m pip install --editable .
```

### Day-to-day development with `uv`

You can also avoid a global install and just run the package directly from the workspace:

```bash
uv run route53-delegation --help
```

If your environment restricts writes to the default `uv` cache location, use a workspace-local cache:

```bash
env UV_CACHE_DIR=.uv-cache uv run route53-delegation --help
```

## Manifest Format

Create a manifest file such as `manifest.yaml`:

```yaml
parent_zone:
  name: xyz.com
  hosted_zone_id: Z1234567890
targets:
  - name: abc.xyz.com
    pre_cutover_ttl: 300
  - name: def.xyz.com
    pre_cutover_ttl: 300
```

Fields:

- `parent_zone.name`: DNS name of the parent public hosted zone
- `parent_zone.hosted_zone_id`: optional Route 53 hosted zone ID; recommended when you want to avoid lookup ambiguity
- `targets[].name`: subdomain to prepare for delegation
- `targets[].pre_cutover_ttl`: TTL to set on eligible records before delegation

If you installed from a wheel and do not have the repository locally, create this manifest yourself in any working directory before running the tool.

## Usage

### 1. Build an Inventory

This reads the parent public hosted zone from Route 53, filters records that belong to the selected target subdomains, and writes an inventory snapshot as YAML.

```bash
uv run route53-delegation inventory --manifest manifest.yaml --output artifacts/inventory.yaml
```

If `--output` is omitted, the tool writes a timestamped YAML file under `artifacts/`.

### 2. Generate a Plan

This reads the inventory snapshot and writes a TTL-reduction plan.

```bash
uv run route53-delegation plan --inventory artifacts/inventory.yaml --output artifacts/plan.yaml
```

The plan includes:

- records eligible for TTL reduction
- skipped records and the reason they were skipped
- future migration phases for operator reference

### 3. Reduce TTLs

By default, this is a dry-run. It builds the exact Route 53 change batch and writes the result to a YAML artifact without making changes in AWS.

This operation uses the plan artifact as the source of truth for the target zone and TTL updates.

```bash
uv run route53-delegation reduce-ttl --plan artifacts/plan.yaml --output artifacts/reduce-ttl.yaml
```

To actually apply the TTL updates:

```bash
uv run route53-delegation reduce-ttl --plan artifacts/plan.yaml --apply --output artifacts/reduce-ttl.yaml
```

### 4. Create Child Hosted Zones

This checks whether each target child zone already exists. In dry-run mode it reports what would be created. With `--apply`, it creates missing public hosted zones in Route 53.

This operation uses the inventory artifact as the source of truth for the list of child zones to create.

```bash
uv run route53-delegation create-child-zones --inventory artifacts/inventory.yaml --output artifacts/create-child-zones.yaml
```

To actually create missing child zones:

```bash
uv run route53-delegation create-child-zones --inventory artifacts/inventory.yaml --apply --output artifacts/create-child-zones.yaml
```

### 5. Populate Child Hosted Zones

This reads the inventory snapshot and prepares the record copy into each child zone. Route 53-managed apex `NS` and `SOA` records are skipped automatically.

```bash
uv run route53-delegation populate-child-zones --inventory artifacts/inventory.yaml --output artifacts/populate-child-zones.yaml
```

To actually populate the child zones:

```bash
uv run route53-delegation populate-child-zones --inventory artifacts/inventory.yaml --apply --output artifacts/populate-child-zones.yaml
```

### 6. Add Parent-Zone Delegation Records

This resolves the live child hosted zones, reads their Route 53 name servers, and prepares parent-zone `NS` delegation records.

This operation uses the `source_zone` and `targets` stored in the inventory artifact as the source of truth.

```bash
uv run route53-delegation delegate-subdomains --inventory artifacts/inventory.yaml --output artifacts/delegate-subdomains.yaml
```

To actually add the delegation records:

```bash
uv run route53-delegation delegate-subdomains --inventory artifacts/inventory.yaml --apply --output artifacts/delegate-subdomains.yaml
```

### 7. Clean Up the Parent Zone

This prepares deletion of the migrated records from the parent zone after delegation. It preserves the new apex delegation `NS` record automatically.

This operation uses the `source_zone` and `targets` stored in the inventory artifact as the source of truth. 

```bash
uv run route53-delegation cleanup-parent --inventory artifacts/inventory.yaml --output artifacts/cleanup-parent.yaml
```

To actually delete the migrated parent-zone records:

```bash
uv run route53-delegation cleanup-parent --inventory artifacts/inventory.yaml --apply --output artifacts/cleanup-parent.yaml
```

### 8. Export a Zone File

This exports one target subtree from the inventory artifact into a BIND-style zone file that is suitable for review and often useful as a starting point for Route 53 console import workflows.

```bash
uv run route53-delegation export-zone-file --inventory artifacts/inventory.yaml --target abc.xyz.com --output artifacts/abc.xyz.com.zone
```

Notes:

- standard records are emitted one value per line
- unsupported Route 53-specific records are written as comments with skip reasons
- apex child-zone `NS` and `SOA` records are skipped because Route 53 manages them automatically

### 9. Verify Delegation

This checks the live child hosted zones, compares the expected child-zone name servers with recursive `NS` resolution, and runs authoritative `dig` queries against the child-zone name servers for one sample record per target.

```bash
uv run route53-delegation verify-delegation --inventory artifacts/inventory.yaml --output artifacts/verify-delegation.yaml
```

The verification artifact includes:

- expected child-zone name servers from Route 53
- recursive `NS` answers for the target
- whether the delegation matches
- direct authoritative answers from the child-zone name servers
- a trace excerpt for the sample record

`verify-delegation` is report-only. It does not automatically fail the migration, it does not trigger rollback, and it does not currently exit non-zero just because the DNS answers do not match the expected child hosted zone.

How to tell the DNS change looks correct:

- `delegation_matches` is `true`
- `recursive_name_servers` and `expected_name_servers` contain the same Route 53 child-zone name servers
- every entry in `authoritative_checks` has `authoritative: true`
- every `authoritative_checks[].answer` contains the expected record value from the child zone
- `trace_excerpt` shows the target subdomain being referred to the new child-zone name servers

How to tell something is wrong:

- `delegation_matches` is `false`
- `recursive_name_servers` still show old or unexpected name servers
- one or more `authoritative_checks` entries have `authoritative: false`
- one child-zone name server returns an empty answer, `SERVFAIL`, or an unexpected value
- `trace_excerpt` does not show the expected child-zone delegation path

Important operational note:

- DNS changes often take time to converge because of resolver caches and TTL
- direct authoritative checks against the child-zone name servers can be correct before recursive resolution has fully caught up
- if authoritative answers are correct but recursive answers are still old, wait for TTL expiry before deciding the change is bad

### 10. Roll Back a Migration

Rollback is split into separate operations so you can reverse only the part that needs to be undone.

Recommended rollback order:

1. restore parent records
2. remove parent-zone delegation
3. restore original TTLs if needed

If you are rolling back immediately after a bad delegation, restoring parent records first reduces the chance of a gap when the delegation record is removed.

#### Restore Parent Records

This recreates the original migrated records in the parent zone from the inventory snapshot. It preserves the delegation `NS` record during the restore step.

```bash
uv run route53-delegation restore-parent-records --inventory artifacts/inventory.yaml --output artifacts/restore-parent-records.yaml
```

To actually restore the records:

```bash
uv run route53-delegation restore-parent-records --inventory artifacts/inventory.yaml --apply --output artifacts/restore-parent-records.yaml
```

#### Remove Delegation Records

This deletes the parent-zone `NS` delegation records for the selected targets.

```bash
uv run route53-delegation undelegate-subdomains --inventory artifacts/inventory.yaml --output artifacts/undelegate-subdomains.yaml
```

To actually remove the delegation:

```bash
uv run route53-delegation undelegate-subdomains --inventory artifacts/inventory.yaml --apply --output artifacts/undelegate-subdomains.yaml
```

#### Restore Original TTLs

This uses the artifact generated by `reduce-ttl` to put the original TTL values back.

```bash
uv run route53-delegation restore-ttl --result artifacts/reduce-ttl.yaml --output artifacts/restore-ttl.yaml
```

To actually restore the TTLs:

```bash
uv run route53-delegation restore-ttl --result artifacts/reduce-ttl.yaml --apply --output artifacts/restore-ttl.yaml
```

## Example Workflow

```bash
uv run route53-delegation inventory --manifest manifest.yaml --output artifacts/inventory.yaml
uv run route53-delegation plan --inventory artifacts/inventory.yaml --output artifacts/plan.yaml
uv run route53-delegation reduce-ttl --plan artifacts/plan.yaml
uv run route53-delegation create-child-zones --inventory artifacts/inventory.yaml
uv run route53-delegation populate-child-zones --inventory artifacts/inventory.yaml
uv run route53-delegation delegate-subdomains --inventory artifacts/inventory.yaml
uv run route53-delegation cleanup-parent --inventory artifacts/inventory.yaml
uv run route53-delegation export-zone-file --inventory artifacts/inventory.yaml --target abc.xyz.com --output artifacts/abc.xyz.com.zone
uv run route53-delegation verify-delegation --inventory artifacts/inventory.yaml
```

Then, once you have reviewed each dry-run artifact:

```bash
uv run route53-delegation reduce-ttl --plan artifacts/plan.yaml --apply
uv run route53-delegation create-child-zones --inventory artifacts/inventory.yaml --apply
uv run route53-delegation populate-child-zones --inventory artifacts/inventory.yaml --apply
uv run route53-delegation delegate-subdomains --inventory artifacts/inventory.yaml --apply
uv run route53-delegation cleanup-parent --inventory artifacts/inventory.yaml --apply
```

Example rollback workflow:

```bash
uv run route53-delegation restore-parent-records --inventory artifacts/inventory.yaml
uv run route53-delegation undelegate-subdomains --inventory artifacts/inventory.yaml
uv run route53-delegation restore-ttl --result artifacts/reduce-ttl.yaml
```

Then, once you have reviewed the rollback dry-run artifacts:

```bash
uv run route53-delegation restore-parent-records --inventory artifacts/inventory.yaml --apply
uv run route53-delegation undelegate-subdomains --inventory artifacts/inventory.yaml --apply
uv run route53-delegation restore-ttl --result artifacts/reduce-ttl.yaml --apply
```

## Running Tests

Run the full test suite with:

```bash
uv run pytest
```

If needed, use a workspace-local cache:

```bash
env UV_CACHE_DIR=.uv-cache uv run pytest
```

## Project Layout

- [src/route53_delegation/cli.py](/Users/jose/Documents/Codex/2026-05-03/i-use-aws-route53-where-i/src/route53_delegation/cli.py): CLI entrypoint
- [src/route53_delegation/core.py](/Users/jose/Documents/Codex/2026-05-03/i-use-aws-route53-where-i/src/route53_delegation/core.py): record filtering, planning, and TTL change logic
- [src/route53_delegation/aws.py](/Users/jose/Documents/Codex/2026-05-03/i-use-aws-route53-where-i/src/route53_delegation/aws.py): Route 53 API wrapper
- [src/route53_delegation/dns.py](/Users/jose/Documents/Codex/2026-05-03/i-use-aws-route53-where-i/src/route53_delegation/dns.py): `dig` helpers for delegation verification
- [src/route53_delegation/manifest.py](/Users/jose/Documents/Codex/2026-05-03/i-use-aws-route53-where-i/src/route53_delegation/manifest.py): manifest parsing and validation
- [tests/test_cli.py](/Users/jose/Documents/Codex/2026-05-03/i-use-aws-route53-where-i/tests/test_cli.py) and [tests/test_core.py](/Users/jose/Documents/Codex/2026-05-03/i-use-aws-route53-where-i/tests/test_core.py): test coverage

## Notes

- v1 supports public hosted zones only.
- The inventory artifact preserves full Route 53 record payloads so later phases can recreate records exactly.
- TTL reduction skips records that should not be changed in this first version, such as alias records and `NS`/`SOA` records.
- Child-zone population skips the child zone's apex `NS` and `SOA` records because Route 53 manages them automatically.
- Parent cleanup preserves the new delegation `NS` record at the target apex.
- `verify-delegation` relies on the local `dig` command being available in the operator environment.
- `verify-delegation` is report-only and is designed to help operators decide whether to wait for DNS convergence or begin rollback.
