"""Contract tests for approved role-specific Model Profiles."""

import json
import unittest
from pathlib import Path

from packages.contracts import ModelProfile, ModelProfileRole

FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "m0" / "assessment_model_profile.json"


class ModelProfileContractTest(unittest.TestCase):
    def test_assessment_profile_pins_the_virginia_nova_lite_model(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text())
        profile = ModelProfile(
            model_profile_id=fixture["model_profile_id"],
            role=ModelProfileRole(fixture["role"]),
            region=fixture["region"],
            model_id=fixture["model_id"],
            prompt_version=fixture["prompt_version"],
            rubric_version=fixture["rubric_version"],
            golden_dataset_version=fixture["golden_dataset_version"],
        )

        self.assertEqual(profile.to_dict(), fixture)
