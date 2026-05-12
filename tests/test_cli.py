from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from route53_delegation import cli


MANIFEST = """
parent_zone:
  name: xyz.com
  hosted_zone_id: Z123
targets:
  - name: abc.xyz.com
    pre_cutover_ttl: 300
"""


class FakeRoute53Service:
    def __init__(self) -> None:
        self.applied_batches = []
        self.created_zones = []

    def resolve_public_hosted_zone(self, zone_name: str, hosted_zone_id: str | None = None) -> dict[str, object]:
        if zone_name == "abc.xyz.com":
            return {"id": "ZCHILD1", "name": "abc.xyz.com.", "private_zone": False}
        return {"id": hosted_zone_id or "Z123", "name": f"{zone_name}.", "private_zone": False}

    def find_public_hosted_zone(self, zone_name: str) -> dict[str, object] | None:
        if zone_name == "abc.xyz.com":
            return {"id": "ZCHILD1", "name": "abc.xyz.com.", "private_zone": False}
        return None

    def get_hosted_zone_details(self, hosted_zone_id: str) -> dict[str, object]:
        if hosted_zone_id == "ZCHILD1":
            return {
                "id": "ZCHILD1",
                "name": "abc.xyz.com.",
                "private_zone": False,
                "name_servers": ["ns-1.awsdns.com", "ns-2.awsdns.net"],
            }
        return {
            "id": hosted_zone_id,
            "name": "xyz.com.",
            "private_zone": False,
            "name_servers": [],
        }

    def create_public_hosted_zone(self, zone_name: str, caller_reference: str, comment: str | None = None) -> dict[str, object]:
        self.created_zones.append((zone_name, caller_reference, comment))
        return {
            "id": "ZNEW1",
            "name": f"{zone_name}.",
            "private_zone": False,
            "name_servers": ["ns-11.awsdns.com", "ns-22.awsdns.net"],
        }

    def list_all_record_sets(self, hosted_zone_id: str) -> list[dict[str, object]]:
        if hosted_zone_id == "ZCHILD1":
            return [
                {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1.awsdns.com."}]},
                {"Name": "abc.xyz.com.", "Type": "SOA", "TTL": 900, "ResourceRecords": [{"Value": "ns-1.awsdns.com. hostmaster.awsdns.com. 1 7200 900 1209600 86400"}]},
            ]
        return [
            {"Name": "abc.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.10"}]},
            {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.11"}]},
            {"Name": "other.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.12"}]},
        ]

    def apply_change_batch(self, hosted_zone_id: str, changes: list[dict[str, object]]) -> dict[str, object]:
        self.applied_batches.append((hosted_zone_id, changes))
        return {"ChangeInfo": {"Id": "C123", "Status": "PENDING"}}


def write_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    return path


def test_version_command_prints_package_version(capsys) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["version"])
    with patch("route53_delegation.cli._package_version", return_value="1.2.3"):
        assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "1.2.3"


def test_inventory_command_writes_yaml_snapshot(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path)
    output_path = tmp_path / "inventory.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(manifest=str(manifest_path), output=str(output_path))
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_inventory(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["source_zone"]["hosted_zone_id"] == "Z123"
    assert payload["targets"][0]["matched_record_count"] == 2


def test_plan_command_uses_inventory_file(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [
                    {
                        "name": "abc.xyz.com.",
                        "pre_cutover_ttl": 300,
                        "source_records": [
                            {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.11"}]}
                        ],
                        "records": [
                            {"name": "a.abc.xyz.com.", "type": "A", "ttl": 3600, "resource_records": ["192.0.2.11"]}
                        ],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "plan.yaml"
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path))
    assert cli.run_plan(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["summary"]["eligible_ttl_update_count"] == 1


def test_reduce_ttl_dry_run_writes_changes_without_applying(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [
                    {
                        "name": "abc.xyz.com.",
                        "eligible_ttl_updates": [
                            {
                                "record_key": "abc.xyz.com.|A",
                                "target_ttl": 300,
                            }
                        ],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "result.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(plan=str(plan_path), output=str(output_path), apply=False)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_reduce_ttl(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry-run"
    assert payload["attempted_change_count"] == 1
    assert fake_service.applied_batches == []


def test_reduce_ttl_apply_calls_route53(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [
                    {
                        "name": "abc.xyz.com.",
                        "eligible_ttl_updates": [
                            {
                                "record_key": "a.abc.xyz.com.|A",
                                "target_ttl": 300,
                            }
                        ],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "result.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(plan=str(plan_path), output=str(output_path), apply=True)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_reduce_ttl(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "apply"
    assert payload["aws_change_info"]["batch_count"] == 1
    assert payload["aws_change_info"]["batches"][0]["change_info"]["Status"] == "PENDING"
    assert len(fake_service.applied_batches) == 1


def test_create_child_zones_dry_run_reports_existing_zone(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [{"name": "abc.xyz.com.", "pre_cutover_ttl": 300}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "create-child-zones.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path), apply=False)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_create_child_zones(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["targets"][0]["action"] == "exists"
    assert payload["targets"][0]["hosted_zone_id"] == "ZCHILD1"


def test_populate_child_zones_dry_run_writes_upserts(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [
                    {
                        "name": "abc.xyz.com.",
                        "pre_cutover_ttl": 300,
                        "source_records": [
                            {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1.awsdns.com."}]},
                            {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                        ],
                        "records": [],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "populate.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path), apply=False)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_populate_child_zones(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["targets"][0]["attempted_change_count"] == 1
    assert payload["targets"][0]["skipped_changes"][0]["reason"] == "child_zone_apex_managed_by_route53"


def test_populate_child_zones_dry_run_skips_apex_cname(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [
                    {
                        "name": "abc.xyz.com.",
                        "pre_cutover_ttl": 300,
                        "source_records": [
                            {
                                "Name": "abc.xyz.com.",
                                "Type": "CNAME",
                                "TTL": 300,
                                "ResourceRecords": [{"Value": "target.example.net."}],
                            }
                        ],
                        "records": [],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "populate-cname.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path), apply=False)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_populate_child_zones(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["targets"][0]["attempted_change_count"] == 0
    assert payload["targets"][0]["skipped_changes"][0]["reason"] == "apex_cname_not_permitted_in_child_zone"


def test_delegate_subdomains_apply_calls_parent_zone(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [{"name": "abc.xyz.com.", "pre_cutover_ttl": 300}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "delegate.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path), apply=True)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_delegate_subdomains(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["attempted_change_count"] == 1
    assert payload["aws_change_info"]["batch_count"] == 1
    assert fake_service.applied_batches[0][0] == "Z123"


def test_delegate_subdomains_blocks_parent_apex_cname_and_continues(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [
                    {"name": "abc.xyz.com.", "pre_cutover_ttl": 300},
                    {"name": "def.xyz.com.", "pre_cutover_ttl": 120},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "delegate-blocked.yaml"
    fake_service = FakeRoute53Service()

    def resolve_public_hosted_zone(zone_name: str, hosted_zone_id: str | None = None) -> dict[str, object]:
        if zone_name == "abc.xyz.com":
            return {"id": "ZCHILD1", "name": "abc.xyz.com.", "private_zone": False}
        if zone_name == "def.xyz.com":
            return {"id": "ZCHILD2", "name": "def.xyz.com.", "private_zone": False}
        return {"id": hosted_zone_id or "Z123", "name": f"{zone_name}.", "private_zone": False}

    def get_hosted_zone_details(hosted_zone_id: str) -> dict[str, object]:
        if hosted_zone_id == "ZCHILD1":
            return {
                "id": "ZCHILD1",
                "name": "abc.xyz.com.",
                "private_zone": False,
                "name_servers": ["ns-1.awsdns.com", "ns-2.awsdns.net"],
            }
        if hosted_zone_id == "ZCHILD2":
            return {
                "id": "ZCHILD2",
                "name": "def.xyz.com.",
                "private_zone": False,
                "name_servers": ["ns-3.awsdns.org", "ns-4.awsdns.co.uk"],
            }
        return {
            "id": hosted_zone_id,
            "name": "xyz.com.",
            "private_zone": False,
            "name_servers": [],
        }

    fake_service.resolve_public_hosted_zone = resolve_public_hosted_zone  # type: ignore[method-assign]
    fake_service.get_hosted_zone_details = get_hosted_zone_details  # type: ignore[method-assign]
    fake_service.list_all_record_sets = lambda hosted_zone_id: [  # type: ignore[method-assign]
        {
            "Name": "abc.xyz.com.",
            "Type": "CNAME",
            "TTL": 300,
            "ResourceRecords": [{"Value": "target.example.net."}],
        }
    ]

    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path), apply=True)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_delegate_subdomains(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["attempted_change_count"] == 1
    assert payload["blocked_target_count"] == 1
    assert payload["targets"] == [
        {
            "name": "abc.xyz.com.",
            "status": "blocked",
            "reason": "parent_apex_cname_conflicts_with_delegation",
            "message": "Cannot create NS delegation for abc.xyz.com. because the parent zone still has a CNAME record at the same name.",
            "blocking_record": {
                "name": "abc.xyz.com.",
                "type": "CNAME",
                "ttl": 300,
                "resource_records": ["target.example.net."],
            },
            "guidance": "Remove or replace the parent-zone apex CNAME before retrying delegate-subdomains.",
        },
        {
            "name": "def.xyz.com.",
            "status": "planned",
            "change": {
                "action": "UPSERT",
                "record": {
                    "Name": "def.xyz.com.",
                    "Type": "NS",
                    "TTL": 120,
                    "ResourceRecords": [{"Value": "ns-3.awsdns.org."}, {"Value": "ns-4.awsdns.co.uk."}],
                },
            },
        },
    ]
    assert payload["blocked_targets"][0]["reason"] == "parent_apex_cname_conflicts_with_delegation"
    assert fake_service.applied_batches == [
        (
            "Z123",
            [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": "def.xyz.com.",
                        "Type": "NS",
                        "TTL": 120,
                        "ResourceRecords": [{"Value": "ns-3.awsdns.org."}, {"Value": "ns-4.awsdns.co.uk."}],
                    },
                }
            ],
        )
    ]


def test_delegate_subdomains_prints_terminal_warning_for_blocked_target(tmp_path: Path, capsys) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [{"name": "abc.xyz.com.", "pre_cutover_ttl": 300}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "delegate-warning.yaml"
    fake_service = FakeRoute53Service()
    fake_service.list_all_record_sets = lambda hosted_zone_id: [  # type: ignore[method-assign]
        {
            "Name": "abc.xyz.com.",
            "Type": "CNAME",
            "TTL": 300,
            "ResourceRecords": [{"Value": "target.example.net."}],
        }
    ]
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path), apply=False)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_delegate_subdomains(args) == 0
    captured = capsys.readouterr()
    assert "WARNING: delegation blocked for abc.xyz.com." in captured.err
    assert "conflicting CNAME record at the same name" in captured.err
    assert "Remove or replace that parent-zone record, then rerun delegate-subdomains." in captured.err
    assert "Current record value(s): target.example.net." in captured.err


def test_cleanup_parent_dry_run_skips_delegation_record(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [
                    {
                        "name": "abc.xyz.com.",
                        "pre_cutover_ttl": 300,
                        "source_records": [
                            {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1.awsdns.com."}]},
                            {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                        ],
                        "records": [],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "cleanup.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path), apply=False)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_cleanup_parent(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["attempted_change_count"] == 1
    assert payload["skipped_changes"][0]["reason"] == "delegation_record_preserved"


def test_export_zone_file_writes_bind_style_lines(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "targets": [
                    {
                        "name": "abc.xyz.com.",
                        "source_records": [
                            {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1"}]},
                            {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                        ],
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "abc.zone"
    args = SimpleNamespace(inventory=str(inventory_path), target="abc.xyz.com", output=str(output_path))
    assert cli.run_export_zone_file(args) == 0
    content = output_path.read_text(encoding="utf-8")
    assert "a.abc.xyz.com. 300 IN A 192.0.2.11" in content
    assert "skipped abc.xyz.com. NS" in content


def test_verify_delegation_writes_structured_results(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "targets": [
                    {
                        "name": "abc.xyz.com.",
                        "source_records": [
                            {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1"}]},
                            {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                        ],
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "verify.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path))
    with (
        patch("route53_delegation.cli.create_route53_service", return_value=fake_service),
        patch("route53_delegation.cli.dig_short", return_value=["ns-1.awsdns.com.", "ns-2.awsdns.net."]),
        patch(
            "route53_delegation.cli.dig_full",
            side_effect=[
                "abc.xyz.com. 300 IN NS ns-1.awsdns.com.\na.abc.xyz.com. 300 IN A 192.0.2.11\n",
                ";; flags: qr aa rd ra;\n;; ANSWER SECTION:\na.abc.xyz.com. 300 IN A 192.0.2.11\n;; Query time: 1 msec\n",
                ";; flags: qr aa rd ra;\n;; ANSWER SECTION:\na.abc.xyz.com. 300 IN A 192.0.2.11\n;; Query time: 1 msec\n",
            ],
        ),
    ):
        assert cli.run_verify_delegation(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["targets"][0]["delegation_matches"] is True
    assert payload["targets"][0]["authoritative_checks"][0]["authoritative"] is True


def test_verify_delegation_autogenerates_output_when_omitted(tmp_path: Path, monkeypatch) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [
                    {
                        "name": "abc.xyz.com.",
                        "source_records": [
                            {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                        ],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(inventory=str(inventory_path), output=None)
    monkeypatch.chdir(tmp_path)
    with (
        patch("route53_delegation.cli.create_route53_service", return_value=fake_service),
        patch("route53_delegation.cli.dig_short", return_value=["ns-1.awsdns.com.", "ns-2.awsdns.net."]),
        patch(
            "route53_delegation.cli.dig_full",
            side_effect=[
                "abc.xyz.com. 300 IN NS ns-1.awsdns.com.\na.abc.xyz.com. 300 IN A 192.0.2.11\n",
                ";; flags: qr aa rd ra;\n;; ANSWER SECTION:\na.abc.xyz.com. 300 IN A 192.0.2.11\n;; Query time: 1 msec\n",
                ";; flags: qr aa rd ra;\n;; ANSWER SECTION:\na.abc.xyz.com. 300 IN A 192.0.2.11\n;; Query time: 1 msec\n",
            ],
        ),
    ):
        assert cli.run_verify_delegation(args) == 0
    generated = sorted((tmp_path / "artifacts").glob("*verify-delegation*.yaml"))
    assert len(generated) == 1


def test_restore_ttl_dry_run_writes_restore_changes(tmp_path: Path) -> None:
    result_path = tmp_path / "reduce-ttl.yaml"
    result_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "changes": [
                    {
                        "record": {"Name": "abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.10"}]},
                        "original_ttl": 3600,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "restore-ttl.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(result=str(result_path), output=str(output_path), apply=False)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_restore_ttl(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["attempted_change_count"] == 1
    assert payload["changes"][0]["record"]["TTL"] == 3600


def test_undelegate_subdomains_apply_calls_route53(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [{"name": "abc.xyz.com.", "pre_cutover_ttl": 300}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "undelegate.yaml"
    fake_service = FakeRoute53Service()
    fake_service.list_all_record_sets = lambda hosted_zone_id: [  # type: ignore[method-assign]
        {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1.awsdns.com."}]},
        {"Name": "other.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.12"}]},
    ]
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path), apply=True)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_undelegate_subdomains(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["aws_change_info"]["batch_count"] == 1
    assert payload["changes"][0]["action"] == "DELETE"


def test_cleanup_parent_apply_batches_large_change_sets(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    source_records = [
        {"Name": f"r{index}.abc.xyz.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "192.0.2.11"}]}
        for index in range(1200)
    ]
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [{"name": "abc.xyz.com.", "source_records": source_records}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "cleanup-batched.yaml"
    fake_service = FakeRoute53Service()
    fake_service.list_all_record_sets = lambda hosted_zone_id: source_records  # type: ignore[method-assign]
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path), apply=True)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_cleanup_parent(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["aws_change_info"]["batch_count"] == 2
    assert [len(batch[1]) for batch in fake_service.applied_batches] == [900, 300]


def test_restore_parent_records_dry_run_writes_upserts(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "source_zone": {"name": "xyz.com.", "hosted_zone_id": "Z123", "private_zone": False},
                "targets": [
                    {
                        "name": "abc.xyz.com.",
                        "source_records": [
                            {"Name": "abc.xyz.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-1.awsdns.com."}]},
                            {"Name": "a.abc.xyz.com.", "Type": "A", "TTL": 3600, "ResourceRecords": [{"Value": "192.0.2.11"}]},
                        ],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "restore-parent.yaml"
    fake_service = FakeRoute53Service()
    args = SimpleNamespace(inventory=str(inventory_path), output=str(output_path), apply=False)
    with patch("route53_delegation.cli.create_route53_service", return_value=fake_service):
        assert cli.run_restore_parent_records(args) == 0
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["attempted_change_count"] == 1
    assert payload["changes"][0]["action"] == "UPSERT"
    assert payload["skipped_changes"][0]["reason"] == "delegation_record_preserved"
