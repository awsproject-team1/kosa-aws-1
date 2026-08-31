"""Runtime construction for the regional Bedrock Converse client."""

from typing import Protocol

from apps.backend.assessment.bedrock import BedrockConverseClient
from packages.contracts import ModelProfile, ModelProfileRole


class Boto3Module(Protocol):
    def client(self, service_name: str, **kwargs: object) -> BedrockConverseClient: ...


class BedrockConverseClientFactory:
    """Bind an injected boto3 module to the Region approved in a Model Profile."""

    def __init__(self, boto3_module: Boto3Module) -> None:
        if boto3_module is None:
            raise TypeError("boto3_module is required")
        self._boto3 = boto3_module

    def for_assessment(self, profile: ModelProfile) -> BedrockConverseClient:
        if not isinstance(profile, ModelProfile):
            raise TypeError("profile must be a ModelProfile")
        if profile.role is not ModelProfileRole.ASSESSMENT:
            raise ValueError("profile must be approved for assessment")
        return self._boto3.client("bedrock-runtime", region_name=profile.region)
