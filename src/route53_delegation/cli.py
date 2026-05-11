from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

from route53_delegation.aws import Route53Service
from route53_delegation.core import (
    build_child_zone_change_set,
    chunk_changes,
    build_delegation_change_set,
    build_inventory_snapshot,
    build_parent_cleanup_change_set,
    build_record_lookup,
    build_restore_parent_change_set,
    build_restore_ttl_change_set,
    build_ttl_change_set,
    build_ttl_plan,
    build_undelegation_change_set,
    build_zone_file_export,
    pick_verification_record,
)
from route53_delegation.dns import dig_full, dig_short, parse_authoritative_answer
from route53_delegation.io import default_output_path, dump_yaml_file, load_yaml_file
from route53_delegation.manifest import load_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="route53-delegation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory", help="Snapshot selected records from the parent hosted zone")
    inventory_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    inventory_parser.add_argument("--output", help="Path to write the inventory YAML file")
    inventory_parser.set_defaults(func=run_inventory)

    plan_parser = subparsers.add_parser("plan", help="Build a TTL reduction plan from inventory")
    plan_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    plan_parser.add_argument("--inventory", required=True, help="Path to an inventory YAML file")
    plan_parser.add_argument("--output", help="Path to write the plan YAML file")
    plan_parser.set_defaults(func=run_plan)

    ttl_parser = subparsers.add_parser("reduce-ttl", help="Reduce TTLs for eligible records from a plan")
    ttl_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    ttl_parser.add_argument("--plan", required=True, help="Path to a plan YAML file")
    ttl_parser.add_argument("--output", help="Path to write the execution result YAML file")
    ttl_parser.add_argument("--apply", action="store_true", help="Apply Route 53 changes. Dry-run is the default.")
    ttl_parser.set_defaults(func=run_reduce_ttl)

    create_child_parser = subparsers.add_parser("create-child-zones", help="Create child hosted zones for each target")
    create_child_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    create_child_parser.add_argument("--output", help="Path to write the execution result YAML file")
    create_child_parser.add_argument("--apply", action="store_true", help="Apply Route 53 changes. Dry-run is the default.")
    create_child_parser.set_defaults(func=run_create_child_zones)

    populate_parser = subparsers.add_parser("populate-child-zones", help="Populate child hosted zones from inventory")
    populate_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    populate_parser.add_argument("--inventory", required=True, help="Path to an inventory YAML file")
    populate_parser.add_argument("--output", help="Path to write the execution result YAML file")
    populate_parser.add_argument("--apply", action="store_true", help="Apply Route 53 changes. Dry-run is the default.")
    populate_parser.set_defaults(func=run_populate_child_zones)

    delegate_parser = subparsers.add_parser("delegate-subdomains", help="Create parent-zone NS delegations for each target")
    delegate_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    delegate_parser.add_argument("--output", help="Path to write the execution result YAML file")
    delegate_parser.add_argument("--apply", action="store_true", help="Apply Route 53 changes. Dry-run is the default.")
    delegate_parser.set_defaults(func=run_delegate_subdomains)

    cleanup_parser = subparsers.add_parser("cleanup-parent", help="Delete migrated target records from the parent zone")
    cleanup_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    cleanup_parser.add_argument("--inventory", required=True, help="Path to an inventory YAML file")
    cleanup_parser.add_argument("--output", help="Path to write the execution result YAML file")
    cleanup_parser.add_argument("--apply", action="store_true", help="Apply Route 53 changes. Dry-run is the default.")
    cleanup_parser.set_defaults(func=run_cleanup_parent)

    export_parser = subparsers.add_parser("export-zone-file", help="Export one target subtree as a BIND-style zone file")
    export_parser.add_argument("--inventory", required=True, help="Path to an inventory YAML file")
    export_parser.add_argument("--target", required=True, help="Target subdomain to export")
    export_parser.add_argument("--output", required=True, help="Path to write the zone file")
    export_parser.set_defaults(func=run_export_zone_file)

    verify_parser = subparsers.add_parser("verify-delegation", help="Verify live DNS delegation using dig")
    verify_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    verify_parser.add_argument("--inventory", required=True, help="Path to an inventory YAML file")
    verify_parser.add_argument("--output", help="Path to write the verification YAML file")
    verify_parser.set_defaults(func=run_verify_delegation)

    restore_ttl_parser = subparsers.add_parser("restore-ttl", help="Restore original TTLs from a previous reduce-ttl result artifact")
    restore_ttl_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    restore_ttl_parser.add_argument("--result", required=True, help="Path to a previous reduce-ttl result YAML file")
    restore_ttl_parser.add_argument("--output", help="Path to write the execution result YAML file")
    restore_ttl_parser.add_argument("--apply", action="store_true", help="Apply Route 53 changes. Dry-run is the default.")
    restore_ttl_parser.set_defaults(func=run_restore_ttl)

    undelegate_parser = subparsers.add_parser("undelegate-subdomains", help="Remove parent-zone NS delegations for each target")
    undelegate_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    undelegate_parser.add_argument("--output", help="Path to write the execution result YAML file")
    undelegate_parser.add_argument("--apply", action="store_true", help="Apply Route 53 changes. Dry-run is the default.")
    undelegate_parser.set_defaults(func=run_undelegate_subdomains)

    restore_parent_parser = subparsers.add_parser("restore-parent-records", help="Restore migrated records back into the parent zone from inventory")
    restore_parent_parser.add_argument("--manifest", required=True, help="Path to the manifest YAML file")
    restore_parent_parser.add_argument("--inventory", required=True, help="Path to an inventory YAML file")
    restore_parent_parser.add_argument("--output", help="Path to write the execution result YAML file")
    restore_parent_parser.add_argument("--apply", action="store_true", help="Apply Route 53 changes. Dry-run is the default.")
    restore_parent_parser.set_defaults(func=run_restore_parent_records)

    return parser


def create_route53_service() -> Route53Service:
    return Route53Service(boto3.client("route53"))


def run_inventory(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    service = create_route53_service()
    zone = service.resolve_public_hosted_zone(
        zone_name=manifest.parent_zone.name,
        hosted_zone_id=manifest.parent_zone.hosted_zone_id,
    )
    records = service.list_all_record_sets(zone["id"])
    snapshot = build_inventory_snapshot(manifest, zone, records)
    output_path = Path(args.output) if args.output else default_output_path("inventory", manifest.parent_zone.name)
    dump_yaml_file(output_path, snapshot)
    print(output_path)
    return 0


def run_plan(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    inventory_snapshot = load_yaml_file(Path(args.inventory))
    plan_snapshot = build_ttl_plan(manifest, inventory_snapshot)
    output_path = Path(args.output) if args.output else default_output_path("plan", manifest.parent_zone.name)
    dump_yaml_file(output_path, plan_snapshot)
    print(output_path)
    return 0


def run_reduce_ttl(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    plan_snapshot = load_yaml_file(Path(args.plan))
    service = create_route53_service()
    zone = service.resolve_public_hosted_zone(
        zone_name=manifest.parent_zone.name,
        hosted_zone_id=manifest.parent_zone.hosted_zone_id,
    )
    live_records = service.list_all_record_sets(zone["id"])
    record_lookup = build_record_lookup(live_records)
    changes, skipped = build_ttl_change_set(plan_snapshot, record_lookup)

    result: dict[str, Any] = {
        "schema_version": 1,
        "source_zone": plan_snapshot["source_zone"],
        "mode": "apply" if args.apply else "dry-run",
        "attempted_change_count": len(changes),
        "skipped_changes": skipped,
        "changes": [
            {
                "action": change["Action"],
                "record": change["ResourceRecordSet"],
                "original_ttl": record_lookup[_record_key(change["ResourceRecordSet"])]["TTL"],
                "target_ttl": change["ResourceRecordSet"]["TTL"],
            }
            for change in changes
        ],
    }

    if args.apply and changes:
        result["aws_change_info"] = _apply_changes_in_batches(service, zone["id"], changes)

    output_path = Path(args.output) if args.output else default_output_path("reduce-ttl", manifest.parent_zone.name)
    dump_yaml_file(output_path, result)
    print(output_path)
    return 0


def run_create_child_zones(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    service = create_route53_service()
    targets_output: list[dict[str, Any]] = []

    for target in manifest.targets:
        existing_zone = service.find_public_hosted_zone(target.name)
        if existing_zone is not None:
            details = service.get_hosted_zone_details(existing_zone["id"])
            targets_output.append(
                {
                    "name": target.name + ".",
                    "action": "exists",
                    "hosted_zone_id": details["id"],
                    "name_servers": details.get("name_servers", []),
                }
            )
            continue

        planned = {
            "name": target.name + ".",
            "action": "create",
        }
        if args.apply:
            details = service.create_public_hosted_zone(
                zone_name=target.name,
                caller_reference=_caller_reference(target.name),
                comment=f"Child zone for {target.name}",
            )
            planned["hosted_zone_id"] = details["id"]
            planned["name_servers"] = details.get("name_servers", [])
        targets_output.append(planned)

    result = {
        "schema_version": 1,
        "mode": "apply" if args.apply else "dry-run",
        "targets": targets_output,
    }
    output_path = Path(args.output) if args.output else default_output_path("create-child-zones", manifest.parent_zone.name)
    dump_yaml_file(output_path, result)
    print(output_path)
    return 0


def run_populate_child_zones(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    inventory_snapshot = load_yaml_file(Path(args.inventory))
    service = create_route53_service()
    targets_output: list[dict[str, Any]] = []

    for target in manifest.targets:
        child_zone = service.resolve_public_hosted_zone(zone_name=target.name)
        changes, skipped = build_child_zone_change_set(inventory_snapshot, target.name)
        target_result: dict[str, Any] = {
            "name": target.name + ".",
            "child_hosted_zone_id": child_zone["id"],
            "attempted_change_count": len(changes),
            "skipped_changes": skipped,
            "changes": [{"action": change["Action"], "record": change["ResourceRecordSet"]} for change in changes],
        }
        if args.apply and changes:
            target_result["aws_change_info"] = _apply_changes_in_batches(service, child_zone["id"], changes)
        targets_output.append(target_result)

    result = {
        "schema_version": 1,
        "mode": "apply" if args.apply else "dry-run",
        "targets": targets_output,
    }
    output_path = Path(args.output) if args.output else default_output_path("populate-child-zones", manifest.parent_zone.name)
    dump_yaml_file(output_path, result)
    print(output_path)
    return 0


def run_delegate_subdomains(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    service = create_route53_service()
    parent_zone = service.resolve_public_hosted_zone(
        zone_name=manifest.parent_zone.name,
        hosted_zone_id=manifest.parent_zone.hosted_zone_id,
    )
    child_zone_details: dict[str, dict[str, Any]] = {}
    for target in manifest.targets:
        child_zone = service.resolve_public_hosted_zone(zone_name=target.name)
        child_zone_details[target.name] = service.get_hosted_zone_details(child_zone["id"])

    changes = build_delegation_change_set(manifest, child_zone_details)
    result: dict[str, Any] = {
        "schema_version": 1,
        "source_zone": {
            "name": parent_zone["name"],
            "hosted_zone_id": parent_zone["id"],
            "private_zone": parent_zone["private_zone"],
        },
        "mode": "apply" if args.apply else "dry-run",
        "attempted_change_count": len(changes),
        "changes": [{"action": change["Action"], "record": change["ResourceRecordSet"]} for change in changes],
    }
    if args.apply and changes:
        result["aws_change_info"] = _apply_changes_in_batches(service, parent_zone["id"], changes)

    output_path = Path(args.output) if args.output else default_output_path("delegate-subdomains", manifest.parent_zone.name)
    dump_yaml_file(output_path, result)
    print(output_path)
    return 0


def run_cleanup_parent(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    inventory_snapshot = load_yaml_file(Path(args.inventory))
    service = create_route53_service()
    parent_zone = service.resolve_public_hosted_zone(
        zone_name=manifest.parent_zone.name,
        hosted_zone_id=manifest.parent_zone.hosted_zone_id,
    )
    live_records = service.list_all_record_sets(parent_zone["id"])
    record_lookup = build_record_lookup(live_records)
    changes, skipped = build_parent_cleanup_change_set(inventory_snapshot, record_lookup)

    result: dict[str, Any] = {
        "schema_version": 1,
        "source_zone": {
            "name": parent_zone["name"],
            "hosted_zone_id": parent_zone["id"],
            "private_zone": parent_zone["private_zone"],
        },
        "mode": "apply" if args.apply else "dry-run",
        "attempted_change_count": len(changes),
        "skipped_changes": skipped,
        "changes": [{"action": change["Action"], "record": change["ResourceRecordSet"]} for change in changes],
    }
    if args.apply and changes:
        result["aws_change_info"] = _apply_changes_in_batches(service, parent_zone["id"], changes)

    output_path = Path(args.output) if args.output else default_output_path("cleanup-parent", manifest.parent_zone.name)
    dump_yaml_file(output_path, result)
    print(output_path)
    return 0


def run_export_zone_file(args: argparse.Namespace) -> int:
    inventory_snapshot = load_yaml_file(Path(args.inventory))
    lines, skipped = build_zone_file_export(inventory_snapshot, args.target)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        handle.write(f"; Zone file export for {args.target.rstrip('.')}\n")
        handle.write("; Generated by route53-delegation export-zone-file\n")
        for skipped_record in skipped:
            handle.write(
                f"; skipped {skipped_record['record']['name']} {skipped_record['record']['type']}: {skipped_record['reason']}\n"
            )
        if skipped:
            handle.write("\n")
        for line in lines:
            handle.write(f"{line}\n")
    print(output_path)
    return 0


def run_verify_delegation(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    inventory_snapshot = load_yaml_file(Path(args.inventory))
    service = create_route53_service()
    results: list[dict[str, Any]] = []

    for target in manifest.targets:
        child_zone = service.resolve_public_hosted_zone(zone_name=target.name)
        child_zone_details = service.get_hosted_zone_details(child_zone["id"])
        expected_name_servers = sorted([_fqdn_if_needed(name_server) for name_server in child_zone_details.get("name_servers", [])])
        recursive_name_servers = sorted(dig_short(["NS", target.name]))
        sample_record = pick_verification_record(inventory_snapshot, target.name)
        authoritative_checks: list[dict[str, Any]] = []
        trace_output = ""

        if sample_record is not None:
            record_name = sample_record["Name"]
            record_type = sample_record["Type"]
            trace_output = dig_full(["+trace", record_name])
            for name_server in expected_name_servers:
                response = dig_full([f"@{name_server.rstrip('.')}", record_name, record_type])
                authoritative_checks.append(
                    {
                        "name_server": name_server,
                        "record_name": record_name,
                        "record_type": record_type,
                        "authoritative": parse_authoritative_answer(response),
                        "answer": _extract_answer_lines(response),
                    }
                )

        results.append(
            {
                "target": target.name + ".",
                "child_hosted_zone_id": child_zone_details["id"],
                "expected_name_servers": expected_name_servers,
                "recursive_name_servers": recursive_name_servers,
                "delegation_matches": expected_name_servers == recursive_name_servers,
                "sample_record": _summarize_sample_record(sample_record),
                "authoritative_checks": authoritative_checks,
                "trace_excerpt": _extract_trace_excerpt(trace_output, target.name) if trace_output else [],
            }
        )

    result = {"schema_version": 1, "targets": results}
    output_path = Path(args.output) if args.output else default_output_path("verify-delegation", manifest.parent_zone.name)
    dump_yaml_file(output_path, result)
    print(output_path)
    return 0


def run_restore_ttl(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    reduce_ttl_result = load_yaml_file(Path(args.result))
    service = create_route53_service()
    zone = service.resolve_public_hosted_zone(
        zone_name=manifest.parent_zone.name,
        hosted_zone_id=manifest.parent_zone.hosted_zone_id,
    )
    live_records = service.list_all_record_sets(zone["id"])
    record_lookup = build_record_lookup(live_records)
    changes, skipped = build_restore_ttl_change_set(reduce_ttl_result, record_lookup)

    result: dict[str, Any] = {
        "schema_version": 1,
        "source_zone": reduce_ttl_result.get("source_zone", {"name": zone["name"], "hosted_zone_id": zone["id"], "private_zone": zone["private_zone"]}),
        "mode": "apply" if args.apply else "dry-run",
        "attempted_change_count": len(changes),
        "skipped_changes": skipped,
        "changes": [{"action": change["Action"], "record": change["ResourceRecordSet"]} for change in changes],
    }
    if args.apply and changes:
        result["aws_change_info"] = _apply_changes_in_batches(service, zone["id"], changes)

    output_path = Path(args.output) if args.output else default_output_path("restore-ttl", manifest.parent_zone.name)
    dump_yaml_file(output_path, result)
    print(output_path)
    return 0


def run_undelegate_subdomains(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    service = create_route53_service()
    parent_zone = service.resolve_public_hosted_zone(
        zone_name=manifest.parent_zone.name,
        hosted_zone_id=manifest.parent_zone.hosted_zone_id,
    )
    live_records = service.list_all_record_sets(parent_zone["id"])
    record_lookup = build_record_lookup(live_records)
    changes, skipped = build_undelegation_change_set(manifest, record_lookup)

    result: dict[str, Any] = {
        "schema_version": 1,
        "source_zone": {
            "name": parent_zone["name"],
            "hosted_zone_id": parent_zone["id"],
            "private_zone": parent_zone["private_zone"],
        },
        "mode": "apply" if args.apply else "dry-run",
        "attempted_change_count": len(changes),
        "skipped_changes": skipped,
        "changes": [{"action": change["Action"], "record": change["ResourceRecordSet"]} for change in changes],
    }
    if args.apply and changes:
        result["aws_change_info"] = _apply_changes_in_batches(service, parent_zone["id"], changes)

    output_path = Path(args.output) if args.output else default_output_path("undelegate-subdomains", manifest.parent_zone.name)
    dump_yaml_file(output_path, result)
    print(output_path)
    return 0


def run_restore_parent_records(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    inventory_snapshot = load_yaml_file(Path(args.inventory))
    service = create_route53_service()
    parent_zone = service.resolve_public_hosted_zone(
        zone_name=manifest.parent_zone.name,
        hosted_zone_id=manifest.parent_zone.hosted_zone_id,
    )
    changes, skipped = build_restore_parent_change_set(inventory_snapshot)

    result: dict[str, Any] = {
        "schema_version": 1,
        "source_zone": {
            "name": parent_zone["name"],
            "hosted_zone_id": parent_zone["id"],
            "private_zone": parent_zone["private_zone"],
        },
        "mode": "apply" if args.apply else "dry-run",
        "attempted_change_count": len(changes),
        "skipped_changes": skipped,
        "changes": [{"action": change["Action"], "record": change["ResourceRecordSet"]} for change in changes],
    }
    if args.apply and changes:
        result["aws_change_info"] = _apply_changes_in_batches(service, parent_zone["id"], changes)

    output_path = Path(args.output) if args.output else default_output_path("restore-parent-records", manifest.parent_zone.name)
    dump_yaml_file(output_path, result)
    print(output_path)
    return 0


def _record_key(record: dict[str, Any]) -> str:
    key = f"{record['Name']}|{record['Type']}"
    if "SetIdentifier" in record:
        key += f"|{record['SetIdentifier']}"
    return key


def _caller_reference(name: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{name}-{timestamp}"


def _extract_answer_lines(output: str) -> list[str]:
    capture = False
    lines: list[str] = []
    for line in output.splitlines():
        if line.startswith(";; ANSWER SECTION:"):
            capture = True
            continue
        if capture and line.startswith(";;"):
            break
        if capture and line.strip():
            lines.append(line.strip())
    return lines


def _extract_trace_excerpt(output: str, target_name: str) -> list[str]:
    if not output:
        return []
    matches: list[str] = []
    normalized_target = target_name.rstrip(".")
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if normalized_target in stripped:
            matches.append(stripped)
    return matches[-10:]


def _fqdn_if_needed(name: str) -> str:
    return name if name.endswith(".") else f"{name}."


def _summarize_sample_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "name": record["Name"],
        "type": record["Type"],
        "values": [item["Value"] for item in record.get("ResourceRecords", [])],
    }


def _apply_changes_in_batches(service: Route53Service, hosted_zone_id: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    batches = chunk_changes(changes)
    batch_results: list[dict[str, Any]] = []
    for index, batch in enumerate(batches, start=1):
        response = service.apply_change_batch(hosted_zone_id, batch)
        batch_results.append(
            {
                "batch_number": index,
                "change_count": len(batch),
                "change_info": response["ChangeInfo"],
            }
        )
    return {
        "batch_count": len(batch_results),
        "batches": batch_results,
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
