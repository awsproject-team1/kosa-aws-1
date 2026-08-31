"""Bedrock runtime construction follows the immutable approved Model Profile."""

import unittest

from apps.backend.assessment import BedrockConverseClientFactory
from packages.contracts import ModelProfile, ModelProfileRole


class Boto3:
    def __init__(self) -> None:
        self.calls = []

    def client(self, service_name, **kwargs):
        self.calls.append((service_name, kwargs))
        return object()


PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m1-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="v1",
    rubric_version="v1",
    golden_dataset_version="v1",
)


class BedrockConverseClientFactoryTest(unittest.TestCase):
    def test_uses_only_the_profile_approved_region(self):
        boto3 = Boto3()
        BedrockConverseClientFactory(boto3).for_assessment(PROFILE)
        self.assertEqual(boto3.calls, [("bedrock-runtime", {"region_name": "us-east-1"})])
