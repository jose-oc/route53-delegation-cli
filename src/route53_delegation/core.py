from __future__ import annotations

from copy import deepcopy
from typing import Any

from route53_delegation.manifest import Manifest


SKIP_ALIAS = "alias_record"
SKIP_NS = "ns_record"
SKIP_SOA = "soa_record"
SKIP_NO_TTL = "no_ttl"
SKIP_ALREADY_SET = "ttl_already_matches_target"


def normalize_dns_name(name: str) -> str:
    return name.rstrip(".").lower()


def fqdn(name: str) -> str:
    return normalize_dns_name(name) + "."


def record_belongs_to_target(record_name: str, target_name: str) -> bool:
    normalized_record = normalize_dns_name(record_name)
    normalized_target = normalize_dns_name(target_name)
    return normalized_record == normalized_target or normalized_record.endswith("." + normalized_target)


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(record)
    normalized["Name"] = fqdn(record["Name"])
    if "AliasTarget" in normalized and "DNSName" in normalized["AliasTarget"]:
        normalized["AliasTarget"]["DNSName"] = fqdn(normalized["AliasTarget"]["DNSName"])
    return normalized


def flag_record(record: dict[str, Any], target_name: str) -> list[str]:
    reasons: list[str] = []
    if normalize_dns_name(record["Name"]) == normalize_dns_name(target_name):
        reasons.append("apex_record")
    if record["Type"] == "NS":
        reasons.append("ns_record")
    if record["Type"] == "SOA":
        reasons.append("soa_record")
    if "AliasTarget" in record:
        reasons.append("alias_record")
    if "SetIdentifier" in record:
        reasons.append("routing_policy_record")
    if "HealthCheckId" in record:
        reasons.append("health_check_record")
    return reasons


def summarize_record(record: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "name": fqdn(record["Name"]),
        "type": record["Type"],
    }
    if "TTL" in record:
        summary["ttl"] = record["TTL"]
    if "SetIdentifier" in record:
        summary["set_identifier"] = record["SetIdentifier"]
    if "HealthCheckId" in record:
        summary["health_check_id"] = record["HealthCheckId"]
    if "AliasTarget" in record:
        summary["alias_target"] = {
            "dns_name": fqdn(record["AliasTarget"]["DNSName"]),
            "hosted_zone_id": record["AliasTarget"]["HostedZoneId"],
            "evaluate_target_health": record["AliasTarget"].get("EvaluateTargetHealth", False),
        }
    if "ResourceRecords" in record:
        summary["resource_records"] = [entry["Value"] for entry in record["ResourceRecords"]]
    return summary


def build_inventory_snapshot(manifest: Manifest, zone: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    targets_output: list[dict[str, Any]] = []
    for target in manifest.targets:
        matched = [normalize_record(record) for record in records if record_belongs_to_target(record["Name"], target.name)]
        flagged: list[dict[str, Any]] = []
        for record in matched:
            reasons = flag_record(record, target.name)
            if reasons:
                flagged.append({"record": summarize_record(record), "reasons": reasons})
        targets_output.append(
            {
                "name": fqdn(target.name),
                "pre_cutover_ttl": target.pre_cutover_ttl,
                "matched_record_count": len(matched),
                "source_records": matched,
                "records": [summarize_record(record) for record in matched],
                "flagged_records": flagged,
            }
        )

    return {
        "schema_version": 1,
        "source_zone": {
            "name": fqdn(zone["name"]),
            "hosted_zone_id": zone["id"],
            "private_zone": zone["private_zone"],
        },
        "targets": targets_output,
    }


def ttl_skip_reason(record: dict[str, Any], target_ttl: int) -> str | None:
    if "AliasTarget" in record:
        return SKIP_ALIAS
    if record["Type"] == "NS":
        return SKIP_NS
    if record["Type"] == "SOA":
        return SKIP_SOA
    if "TTL" not in record:
        return SKIP_NO_TTL
    if record["TTL"] == target_ttl:
        return SKIP_ALREADY_SET
    return None


def make_record_key(record: dict[str, Any]) -> str:
    key = f"{fqdn(record['Name'])}|{record['Type']}"
    if "SetIdentifier" in record:
        key += f"|{record['SetIdentifier']}"
    return key


def build_ttl_plan(inventory_snapshot: dict[str, Any]) -> dict[str, Any]:
    plan_targets: list[dict[str, Any]] = []
    total_eligible = 0
    total_skipped = 0

    for target_snapshot in inventory_snapshot["targets"]:
        target_name = target_snapshot["name"]
        desired_ttl = target_snapshot["pre_cutover_ttl"]
        eligible_records: list[dict[str, Any]] = []
        skipped_records: list[dict[str, Any]] = []

        for record_summary in target_snapshot["records"]:
            reason = ttl_skip_reason_from_summary(record_summary, desired_ttl)
            if reason is None:
                eligible_records.append(
                    {
                        "record": record_summary,
                        "current_ttl": record_summary["ttl"],
                        "target_ttl": desired_ttl,
                        "record_key": make_record_key_from_summary(record_summary),
                    }
                )
            else:
                skipped_records.append({"record": record_summary, "reason": reason})

        total_eligible += len(eligible_records)
        total_skipped += len(skipped_records)
        plan_targets.append(
            {
                "name": target_name,
                "pre_cutover_ttl": desired_ttl,
                "eligible_ttl_updates": eligible_records,
                "skipped_ttl_updates": skipped_records,
                "future_phases": [
                    "create_child_hosted_zone",
                    "populate_child_zone",
                    "add_parent_ns_delegation",
                    "delete_parent_zone_records",
                ],
            }
        )

    return {
        "schema_version": 1,
        "source_zone": inventory_snapshot["source_zone"],
        "summary": {
            "target_count": len(plan_targets),
            "eligible_ttl_update_count": total_eligible,
            "skipped_ttl_update_count": total_skipped,
        },
        "targets": plan_targets,
    }


def ttl_skip_reason_from_summary(record_summary: dict[str, Any], target_ttl: int) -> str | None:
    if "alias_target" in record_summary:
        return SKIP_ALIAS
    if record_summary["type"] == "NS":
        return SKIP_NS
    if record_summary["type"] == "SOA":
        return SKIP_SOA
    if "ttl" not in record_summary:
        return SKIP_NO_TTL
    if record_summary["ttl"] == target_ttl:
        return SKIP_ALREADY_SET
    return None


def make_record_key_from_summary(record_summary: dict[str, Any]) -> str:
    key = f"{record_summary['name']}|{record_summary['type']}"
    if "set_identifier" in record_summary:
        key += f"|{record_summary['set_identifier']}"
    return key


def build_record_lookup(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {make_record_key(record): normalize_record(record) for record in records}


def build_ttl_change(record: dict[str, Any], target_ttl: int) -> dict[str, Any]:
    new_record = deepcopy(record)
    new_record["TTL"] = target_ttl
    return {"Action": "UPSERT", "ResourceRecordSet": new_record}


def build_ttl_change_set(plan_snapshot: dict[str, Any], record_lookup: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for target in plan_snapshot["targets"]:
        for planned_record in target["eligible_ttl_updates"]:
            key = planned_record["record_key"]
            source_record = record_lookup.get(key)
            if source_record is None:
                skipped.append({"record_key": key, "reason": "record_missing_from_live_zone"})
                continue
            changes.append(build_ttl_change(source_record, planned_record["target_ttl"]))
    return changes, skipped


def find_target_snapshot(snapshot: dict[str, Any], target_name: str) -> dict[str, Any]:
    normalized_target = fqdn(target_name)
    for target in snapshot["targets"]:
        if target["name"] == normalized_target:
            return target
    raise ValueError(f"Target {target_name} not found in artifact.")


def child_zone_record_skip_reason(record: dict[str, Any], target_name: str) -> str | None:
    normalized_name = normalize_dns_name(record["Name"])
    normalized_target = normalize_dns_name(target_name)
    if normalized_name == normalized_target and record["Type"] in {"NS", "SOA"}:
        return "child_zone_apex_managed_by_route53"
    if normalized_name == normalized_target and record["Type"] == "CNAME":
        return "apex_cname_not_permitted_in_child_zone"
    return None


def build_child_zone_change_set(inventory_snapshot: dict[str, Any], target_name: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target_snapshot = find_target_snapshot(inventory_snapshot, target_name)
    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for record in target_snapshot.get("source_records", []):
        reason = child_zone_record_skip_reason(record, target_name)
        if reason is not None:
            skipped.append({"record_key": make_record_key(record), "reason": reason, "record": summarize_record(record)})
            continue
        changes.append({"Action": "UPSERT", "ResourceRecordSet": deepcopy(record)})
    return changes, skipped


def build_delegation_record(target_name: str, ttl: int, name_servers: list[str]) -> dict[str, Any]:
    return {
        "Name": fqdn(target_name),
        "Type": "NS",
        "TTL": ttl,
        "ResourceRecords": [{"Value": fqdn(name_server)} for name_server in name_servers],
    }


def build_delegation_change_set(manifest: Manifest, child_zone_details: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for target in manifest.targets:
        details = child_zone_details[target.name]
        changes.append(
            {
                "Action": "UPSERT",
                "ResourceRecordSet": build_delegation_record(
                    target_name=target.name,
                    ttl=target.pre_cutover_ttl,
                    name_servers=details["name_servers"],
                ),
            }
        )
    return changes


def cleanup_parent_skip_reason(record: dict[str, Any], target_name: str) -> str | None:
    normalized_name = normalize_dns_name(record["Name"])
    normalized_target = normalize_dns_name(target_name)
    if normalized_name == normalized_target and record["Type"] == "NS":
        return "delegation_record_preserved"
    if normalized_name == normalized_target and record["Type"] == "SOA":
        return "soa_record_not_expected_in_parent_cleanup"
    return None


def build_parent_cleanup_change_set(
    inventory_snapshot: dict[str, Any],
    live_parent_lookup: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for target_snapshot in inventory_snapshot["targets"]:
        target_name = target_snapshot["name"]
        for record in target_snapshot.get("source_records", []):
            reason = cleanup_parent_skip_reason(record, target_name)
            if reason is not None:
                skipped.append({"record_key": make_record_key(record), "reason": reason, "record": summarize_record(record)})
                continue
            key = make_record_key(record)
            live_record = live_parent_lookup.get(key)
            if live_record is None:
                skipped.append({"record_key": key, "reason": "record_missing_from_live_parent_zone"})
                continue
            changes.append({"Action": "DELETE", "ResourceRecordSet": deepcopy(live_record)})
    return changes, skipped


def build_restore_ttl_change_set(
    reduce_ttl_result: dict[str, Any],
    live_record_lookup: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for change in reduce_ttl_result.get("changes", []):
        record = change["record"]
        key = make_record_key(record)
        live_record = live_record_lookup.get(key)
        if live_record is None:
            skipped.append({"record_key": key, "reason": "record_missing_from_live_zone"})
            continue
        restored_record = deepcopy(live_record)
        restored_record["TTL"] = change["original_ttl"]
        changes.append({"Action": "UPSERT", "ResourceRecordSet": restored_record})

    return changes, skipped


def build_undelegation_change_set(
    target_names: list[str],
    live_parent_lookup: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for target_name in target_names:
        key = f"{fqdn(target_name)}|NS"
        live_record = live_parent_lookup.get(key)
        if live_record is None:
            skipped.append({"record_key": key, "reason": "delegation_record_missing_from_live_parent_zone"})
            continue
        changes.append({"Action": "DELETE", "ResourceRecordSet": deepcopy(live_record)})

    return changes, skipped


def build_restore_parent_change_set(inventory_snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for target_snapshot in inventory_snapshot["targets"]:
        target_name = target_snapshot["name"]
        for record in target_snapshot.get("source_records", []):
            reason = cleanup_parent_skip_reason(record, target_name)
            if reason is not None:
                skipped.append({"record_key": make_record_key(record), "reason": reason, "record": summarize_record(record)})
                continue
            changes.append({"Action": "UPSERT", "ResourceRecordSet": deepcopy(record)})

    return changes, skipped


def zone_file_skip_reason(record: dict[str, Any], target_name: str) -> str | None:
    normalized_name = normalize_dns_name(record["Name"])
    normalized_target = normalize_dns_name(target_name)
    if "AliasTarget" in record:
        return "alias_record_not_supported_in_bind_export"
    if "SetIdentifier" in record:
        return "routing_policy_record_not_supported_in_bind_export"
    if "HealthCheckId" in record:
        return "health_check_record_not_supported_in_bind_export"
    if "ResourceRecords" not in record:
        return "record_without_resource_records_not_supported_in_bind_export"
    if normalized_name == normalized_target and record["Type"] in {"NS", "SOA"}:
        return "child_zone_apex_managed_by_route53"
    return None


def format_zone_file_record_line(record: dict[str, Any], value: str) -> str:
    ttl = record.get("TTL", 300)
    return f"{fqdn(record['Name'])} {ttl} IN {record['Type']} {value}"


def build_zone_file_export(inventory_snapshot: dict[str, Any], target_name: str) -> tuple[list[str], list[dict[str, Any]]]:
    target_snapshot = find_target_snapshot(inventory_snapshot, target_name)
    lines: list[str] = []
    skipped: list[dict[str, Any]] = []
    for record in target_snapshot.get("source_records", []):
        reason = zone_file_skip_reason(record, target_name)
        if reason is not None:
            skipped.append({"record_key": make_record_key(record), "reason": reason, "record": summarize_record(record)})
            continue
        for resource_record in record["ResourceRecords"]:
            lines.append(format_zone_file_record_line(record, resource_record["Value"]))
    return lines, skipped


def pick_verification_record(inventory_snapshot: dict[str, Any], target_name: str) -> dict[str, Any] | None:
    target_snapshot = find_target_snapshot(inventory_snapshot, target_name)
    for record in target_snapshot.get("source_records", []):
        if record["Type"] in {"NS", "SOA"}:
            continue
        if "AliasTarget" in record:
            continue
        if "ResourceRecords" not in record:
            continue
        return deepcopy(record)
    return None


def chunk_changes(changes: list[dict[str, Any]], chunk_size: int = 900) -> list[list[dict[str, Any]]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [changes[index : index + chunk_size] for index in range(0, len(changes), chunk_size)]
