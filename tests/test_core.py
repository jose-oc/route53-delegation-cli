from __future__ import annotations

from route53_delegation.core import (
    SKIP_ALIAS,
    SKIP_ALREADY_SET,
    SKIP_NS,
    build_child_zone_change_set,
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
    normalize_dns_name,
    pick_verification_record,
    record_belongs_to_target,
    ttl_skip_reason,
)
from route53_delegation.manifest import Manifest, ParentZone, Target


def manifest() -> Manifest:
    return Manifest(
        parent_zone=ParentZone(name="xyz.com", hosted_zone_id="Z123"),
        targets=[
            Target(name="abc.xyz.com", pre_cutover_ttl=300),
            Target(name="def.xyz.com", pre_cutover_ttl=120),
        ],
    )


def test_normalize_dns_name() -> None:
    assert normalize_dns_name("AbC.XyZ.Com.") == "abc.xyz.com"


def test_record_belongs_to_target_matches_apex_and_descendants() -> None:
    assert record_belongs_to_target("abc.xyz.com.", "abc.xyz.com")
    assert record_belongs_to_target("a.abc.xyz.com.", "abc.xyz.com")
    assert not record_belongs_to_target("abc2.xyz.com.", "abc.xyz.com")


def test_inventory_selects_only_requested_targets() -> None:
    snapshot = build_inventory_snapshot(
        manifest(),
        {"id": "Z123", "name": "xyz.com.", "private_zone": False},
        [
            {"Name": "abc.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.10"}]},
            {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.11"}]},
            {"Name": "abc2.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.12"}]},
        ],
    )
    abc_target = snapshot["targets"][0]
    assert abc_target["matched_record_count"] == 2
    assert len(abc_target["source_records"]) == 2
    assert abc_target["flagged_records"][0]["reasons"] == ["apex_record"]


def test_ttl_skip_reason_marks_alias_and_ns_records() -> None:
    alias_record = {
        "Name": "a.abc.xyz.com.",
        "Type": "A",
        "AliasTarget": {
            "DNSName": "dualstack.lb.example.net.",
            "HostedZoneId": "ZLB",
            "EvaluateTargetHealth": False,
        },
    }
    ns_record = {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1"}]}
    assert ttl_skip_reason(alias_record, 60) == SKIP_ALIAS
    assert ttl_skip_reason(ns_record, 60) == SKIP_NS
    assert ttl_skip_reason({"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300}, 300) == SKIP_ALREADY_SET


def test_plan_generation_preserves_eligible_and_skipped_records() -> None:
    inventory_snapshot = {
        "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
        "targets": [
            {
                "name": "abc.xyz.com.",
                "pre_cutover_ttl": 300,
                "records": [
                    {"name": "a.abc.xyz.com.", "type": "A", "ttl": 3600, "resource_records": ["192.0.2.11"]},
                    {"name": "b.abc.xyz.com.", "type": "NS", "ttl": 172800, "resource_records": ["ns-1"]},
                    {"name": "c.abc.xyz.com.", "type": "A", "alias_target": {"dns_name": "lb.example.net.", "hosted_zone_id": "ZLB", "evaluate_target_health": False}},
                ],
            },
            {"name": "def.xyz.com.", "pre_cutover_ttl": 120, "records": []},
        ],
    }
    plan_snapshot = build_ttl_plan(manifest(), inventory_snapshot)
    abc_target = plan_snapshot["targets"][0]
    assert len(abc_target["eligible_ttl_updates"]) == 1
    assert len(abc_target["skipped_ttl_updates"]) == 2
    assert plan_snapshot["summary"]["eligible_ttl_update_count"] == 1


def test_ttl_change_set_builds_changes_and_reports_missing_records() -> None:
    plan_snapshot = {
        "targets": [
            {
                "eligible_ttl_updates": [
                    {
                        "record_key": "a.abc.xyz.com.|A",
                        "target_ttl": 300,
                    },
                    {
                        "record_key": "missing.abc.xyz.com.|A",
                        "target_ttl": 300,
                    },
                ]
            }
        ]
    }
    record_lookup = build_record_lookup(
        [{"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.11"}]}]
    )
    changes, skipped = build_ttl_change_set(plan_snapshot, record_lookup)
    assert len(changes) == 1
    assert changes[0]["ResourceRecordSet"]["TTL"] == 300
    assert skipped == [{"record_key": "missing.abc.xyz.com.|A", "reason": "record_missing_from_live_zone"}]


def test_child_zone_change_set_skips_route53_managed_apex_ns() -> None:
    inventory_snapshot = {
        "targets": [
            {
                "name": "abc.xyz.com.",
                "source_records": [
                    {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1"}]},
                    {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                ],
            }
        ]
    }
    changes, skipped = build_child_zone_change_set(inventory_snapshot, "abc.xyz.com")
    assert len(changes) == 1
    assert changes[0]["ResourceRecordSet"]["Name"] == "a.abc.xyz.com."
    assert skipped[0]["reason"] == "child_zone_apex_managed_by_route53"


def test_child_zone_change_set_skips_apex_cname() -> None:
    inventory_snapshot = {
        "targets": [
            {
                "name": "illuminate.nepgroup.io.",
                "source_records": [
                    {
                        "Name": "illuminate.nepgroup.io.",
                        "Type": "CNAME",
                        "TTL": 300,
                        "ResourceRecords": [{"Value": "some-target.example.net."}],
                    }
                ],
            }
        ]
    }
    changes, skipped = build_child_zone_change_set(inventory_snapshot, "illuminate.nepgroup.io")
    assert changes == []
    assert skipped[0]["reason"] == "apex_cname_not_permitted_in_child_zone"


def test_build_delegation_change_set_uses_child_zone_name_servers() -> None:
    changes = build_delegation_change_set(
        manifest(),
        {
            "abc.xyz.com": {"name_servers": ["ns-1.awsdns.com", "ns-2.awsdns.net"]},
            "def.xyz.com": {"name_servers": ["ns-3.awsdns.org", "ns-4.awsdns.co.uk"]},
        },
    )
    assert changes[0]["ResourceRecordSet"]["Type"] == "NS"
    assert changes[0]["ResourceRecordSet"]["Name"] == "abc.xyz.com."
    assert changes[0]["ResourceRecordSet"]["ResourceRecords"][0]["Value"] == "ns-1.awsdns.com."


def test_parent_cleanup_change_set_skips_delegation_and_deletes_live_records() -> None:
    inventory_snapshot = {
        "targets": [
            {
                "name": "abc.xyz.com.",
                "source_records": [
                    {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1"}]},
                    {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                ],
            }
        ]
    }
    live_lookup = build_record_lookup(
        [
            {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1"}]},
            {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 120, "ResourceRecords": [{"Value": "192.0.2.11"}]},
        ]
    )
    changes, skipped = build_parent_cleanup_change_set(inventory_snapshot, live_lookup)
    assert len(changes) == 1
    assert changes[0]["Action"] == "DELETE"
    assert changes[0]["ResourceRecordSet"]["TTL"] == 120
    assert skipped[0]["reason"] == "delegation_record_preserved"


def test_zone_file_export_skips_alias_and_formats_standard_records() -> None:
    inventory_snapshot = {
        "targets": [
            {
                "name": "abc.xyz.com.",
                "source_records": [
                    {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1"}]},
                    {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                    {
                        "Name": "b.abc.xyz.com.",
                        "Type": "A",
                        "AliasTarget": {"DNSName": "lb.example.net.", "HostedZoneId": "ZLB", "EvaluateTargetHealth": False},
                    },
                ],
            }
        ]
    }
    lines, skipped = build_zone_file_export(inventory_snapshot, "abc.xyz.com")
    assert lines == ["a.abc.xyz.com. 300 IN A 192.0.2.11"]
    assert skipped[0]["reason"] == "child_zone_apex_managed_by_route53"
    assert skipped[1]["reason"] == "alias_record_not_supported_in_bind_export"


def test_pick_verification_record_uses_standard_non_ns_record() -> None:
    inventory_snapshot = {
        "targets": [
            {
                "name": "abc.xyz.com.",
                "source_records": [
                    {"Name": "abc.xyz.com.", "Type": "SOA", "TTL": 900, "ResourceRecords": [{"Value": "soa"}]},
                    {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1"}]},
                    {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                ],
            }
        ]
    }
    record = pick_verification_record(inventory_snapshot, "abc.xyz.com")
    assert record is not None
    assert record["Name"] == "a.abc.xyz.com."


def test_chunk_changes_splits_large_change_sets() -> None:
    from route53_delegation.core import chunk_changes

    changes = [{"Action": "UPSERT", "ResourceRecordSet": {"Name": f"r{index}.example.com.", "Type": "A"}} for index in range(1801)]
    batches = chunk_changes(changes, chunk_size=900)
    assert [len(batch) for batch in batches] == [900, 900, 1]


def test_restore_ttl_change_set_reinstates_original_ttl() -> None:
    result_snapshot = {
        "changes": [
            {
                "record": {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                "original_ttl": 3600,
            }
        ]
    }
    live_lookup = build_record_lookup(
        [{"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]}]
    )
    changes, skipped = build_restore_ttl_change_set(result_snapshot, live_lookup)
    assert skipped == []
    assert changes[0]["ResourceRecordSet"]["TTL"] == 3600


def test_build_undelegation_change_set_deletes_live_ns_record() -> None:
    live_lookup = build_record_lookup(
        [{"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1.awsdns.com."}]}]
    )
    changes, skipped = build_undelegation_change_set(manifest(), live_lookup)
    assert len(changes) == 1
    assert changes[0]["Action"] == "DELETE"
    assert skipped[0]["reason"] == "delegation_record_missing_from_live_parent_zone"


def test_build_restore_parent_change_set_restores_non_delegation_records() -> None:
    inventory_snapshot = {
        "targets": [
            {
                "name": "abc.xyz.com.",
                "source_records": [
                    {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1"}]},
                    {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                ],
            }
        ]
    }
    changes, skipped = build_restore_parent_change_set(inventory_snapshot)
    assert len(changes) == 1
    assert changes[0]["Action"] == "UPSERT"
    assert changes[0]["ResourceRecordSet"]["Name"] == "a.abc.xyz.com."
    assert skipped[0]["reason"] == "delegation_record_preserved"
