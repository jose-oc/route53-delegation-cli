from __future__ import annotations

import pytest
from botocore.exceptions import NoCredentialsError

from route53_delegation.aws import Route53Service


class NoCredentialsPaginator:
    def paginate(self):
        raise NoCredentialsError()


class NoCredentialsClient:
    def get_paginator(self, name: str):  # noqa: ANN001
        return NoCredentialsPaginator()


def test_find_public_hosted_zone_reports_missing_credentials_cleanly() -> None:
    service = Route53Service(NoCredentialsClient())
    with pytest.raises(RuntimeError, match="AWS credentials not found"):
        service.find_public_hosted_zone("abc.example.com")
