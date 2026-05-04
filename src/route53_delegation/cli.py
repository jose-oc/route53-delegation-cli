from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

from route53_delegation.aws import Route53Service
from route53_delegation.core import (
    build_child_zone_change_set,
    build_delegation_change_set,
    build_inventory_snapshot,
    build_parent_cleanup_change_set,
    build_record_lookup,
    build_ttl_change_set,
    build_ttl_plan,
)
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
        aws_response = service.apply_change_batch(zone["id"], changes)
        result["aws_change_info"] = aws_response["ChangeInfo"]

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
            aws_response = service.apply_change_batch(child_zone["id"], changes)
            target_result["aws_change_info"] = aws_response["ChangeInfo"]
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
        aws_response = service.apply_change_batch(parent_zone["id"], changes)
        result["aws_change_info"] = aws_response["ChangeInfo"]

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
        aws_response = service.apply_change_batch(parent_zone["id"], changes)
        result["aws_change_info"] = aws_response["ChangeInfo"]

    output_path = Path(args.output) if args.output else default_output_path("cleanup-parent", manifest.parent_zone.name)
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


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
