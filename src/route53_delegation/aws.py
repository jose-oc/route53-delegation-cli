from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, PartialCredentialsError, ProfileNotFound


class Route53Service:
    def __init__(self, client: Any) -> None:
        self.client = client

    def get_hosted_zone_details(self, hosted_zone_id: str) -> dict[str, Any]:
        try:
            response = self.client.get_hosted_zone(Id=hosted_zone_id)
        except Exception as exc:  # noqa: BLE001
            raise _translate_aws_exception(exc, f"read hosted zone {hosted_zone_id}") from exc
        hosted_zone = response["HostedZone"]
        return {
            "id": hosted_zone["Id"].split("/")[-1],
            "name": hosted_zone["Name"],
            "private_zone": hosted_zone.get("Config", {}).get("PrivateZone", False),
            "name_servers": response.get("DelegationSet", {}).get("NameServers", []),
        }

    def find_public_hosted_zone(self, zone_name: str) -> dict[str, Any] | None:
        dns_name = zone_name.rstrip(".").lower() + "."
        try:
            paginator = self.client.get_paginator("list_hosted_zones")
            matches: list[dict[str, Any]] = []
            for page in paginator.paginate():
                for hosted_zone in page["HostedZones"]:
                    if hosted_zone["Name"].lower() != dns_name:
                        continue
                    if hosted_zone.get("Config", {}).get("PrivateZone"):
                        continue
                    matches.append(hosted_zone)
        except Exception as exc:  # noqa: BLE001
            raise _translate_aws_exception(exc, f"list hosted zones while looking for {zone_name}") from exc

        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"Multiple public hosted zones found for {zone_name}; specify hosted_zone_id.")

        hosted_zone = matches[0]
        return {
            "id": hosted_zone["Id"].split("/")[-1],
            "name": hosted_zone["Name"],
            "private_zone": hosted_zone.get("Config", {}).get("PrivateZone", False),
        }

    def resolve_public_hosted_zone(self, zone_name: str, hosted_zone_id: str | None = None) -> dict[str, Any]:
        if hosted_zone_id:
            details = self.get_hosted_zone_details(hosted_zone_id)
            if details["private_zone"]:
                raise ValueError(f"Hosted zone {hosted_zone_id} is private; v1 supports public zones only.")
            return details

        match = self.find_public_hosted_zone(zone_name)
        if match is None:
            raise ValueError(f"No public hosted zone found for {zone_name}.")
        return match

    def list_all_record_sets(self, hosted_zone_id: str) -> list[dict[str, Any]]:
        try:
            paginator = self.client.get_paginator("list_resource_record_sets")
            records: list[dict[str, Any]] = []
            for page in paginator.paginate(HostedZoneId=hosted_zone_id):
                records.extend(page["ResourceRecordSets"])
        except Exception as exc:  # noqa: BLE001
            raise _translate_aws_exception(exc, f"list record sets for hosted zone {hosted_zone_id}") from exc
        return records

    def create_public_hosted_zone(self, zone_name: str, caller_reference: str, comment: str | None = None) -> dict[str, Any]:
        try:
            response = self.client.create_hosted_zone(
                Name=zone_name,
                CallerReference=caller_reference,
                HostedZoneConfig={
                    "Comment": comment or f"Child zone for {zone_name}",
                    "PrivateZone": False,
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_aws_exception(exc, f"create hosted zone {zone_name}") from exc
        hosted_zone = response["HostedZone"]
        return {
            "id": hosted_zone["Id"].split("/")[-1],
            "name": hosted_zone["Name"],
            "private_zone": hosted_zone.get("Config", {}).get("PrivateZone", False),
            "name_servers": response.get("DelegationSet", {}).get("NameServers", []),
        }

    def apply_change_batch(self, hosted_zone_id: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            return self.client.change_resource_record_sets(
                HostedZoneId=hosted_zone_id,
                ChangeBatch={"Changes": changes},
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_aws_exception(exc, f"apply a change batch to hosted zone {hosted_zone_id}") from exc



def _translate_aws_exception(exc: Exception, action: str) -> RuntimeError:
    if isinstance(exc, NoCredentialsError):
        return RuntimeError(
            f"AWS credentials not found while trying to {action}. "
            "Configure credentials first, for example by setting AWS_PROFILE or running aws configure."
        )
    if isinstance(exc, PartialCredentialsError):
        return RuntimeError(
            f"Incomplete AWS credentials while trying to {action}. "
            "Check your AWS_PROFILE, environment variables, or credentials file."
        )
    if isinstance(exc, ProfileNotFound):
        return RuntimeError(
            f"AWS profile not found while trying to {action}: {exc}. "
            "Check AWS_PROFILE or your AWS config."
        )
    if isinstance(exc, ClientError):
        message = exc.response.get("Error", {}).get("Message", str(exc))
        return RuntimeError(f"Route 53 rejected a request while trying to {action}: {message}")
    if isinstance(exc, BotoCoreError):
        return RuntimeError(f"AWS client error while trying to {action}: {exc}")
    return RuntimeError(f"Unexpected AWS error while trying to {action}: {exc}")
