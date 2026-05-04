from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from route53_delegation.io import load_yaml_file


@dataclass(frozen=True)
class ParentZone:
    name: str
    hosted_zone_id: str | None


@dataclass(frozen=True)
class Target:
    name: str
    pre_cutover_ttl: int


@dataclass(frozen=True)
class Manifest:
    parent_zone: ParentZone
    targets: list[Target]


def _normalize_name(name: str) -> str:
    return name.rstrip(".").lower()


def load_manifest(path: str | Path) -> Manifest:
    payload = load_yaml_file(Path(path))
    parent_zone_payload = payload.get("parent_zone")
    if not isinstance(parent_zone_payload, dict):
        raise ValueError("Manifest must include a parent_zone mapping.")

    targets_payload = payload.get("targets")
    if not isinstance(targets_payload, list) or not targets_payload:
        raise ValueError("Manifest must include a non-empty targets list.")

    parent_zone_name = parent_zone_payload.get("name")
    if not isinstance(parent_zone_name, str) or not parent_zone_name.strip():
        raise ValueError("parent_zone.name must be a non-empty string.")

    hosted_zone_id = parent_zone_payload.get("hosted_zone_id")
    if hosted_zone_id is not None and not isinstance(hosted_zone_id, str):
        raise ValueError("parent_zone.hosted_zone_id must be a string when provided.")

    targets: list[Target] = []
    seen_names: set[str] = set()
    for entry in targets_payload:
        if not isinstance(entry, dict):
            raise ValueError("Each target must be a mapping.")
        name = entry.get("name")
        ttl = entry.get("pre_cutover_ttl")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Each target.name must be a non-empty string.")
        if not isinstance(ttl, int) or ttl <= 0:
            raise ValueError(f"Target {name!r} must define a positive integer pre_cutover_ttl.")
        normalized_name = _normalize_name(name)
        if normalized_name in seen_names:
            raise ValueError(f"Duplicate target {name!r} in manifest.")
        seen_names.add(normalized_name)
        targets.append(Target(name=normalized_name, pre_cutover_ttl=ttl))

    return Manifest(
        parent_zone=ParentZone(name=_normalize_name(parent_zone_name), hosted_zone_id=hosted_zone_id),
        targets=targets,
    )

