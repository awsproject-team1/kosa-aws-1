"""Approved Model Profile lookup for Assessment evaluation calls."""

from collections.abc import Iterable
from typing import Protocol

from packages.contracts import ModelProfile, ModelProfileRole


class ModelProfileNotFoundError(LookupError):
    """Raised when a worker has no approved profile for an evaluation."""


class ModelProfileRegistry(Protocol):
    def get_assessment_profile(self, model_profile_id: str) -> ModelProfile: ...


class InMemoryModelProfileRegistry:
    """Immutable local registry; production persistence can implement the same port."""

    def __init__(self, profiles: Iterable[ModelProfile]) -> None:
        self._profiles: dict[str, ModelProfile] = {}
        for profile in profiles:
            if not isinstance(profile, ModelProfile):
                raise TypeError("profiles must contain ModelProfile values")
            if profile.model_profile_id in self._profiles:
                raise ValueError("profiles contains duplicate model_profile_id")
            self._profiles[profile.model_profile_id] = profile

    def get_assessment_profile(self, model_profile_id: str) -> ModelProfile:
        profile = self._profiles.get(model_profile_id)
        if profile is None or profile.role is not ModelProfileRole.ASSESSMENT:
            raise ModelProfileNotFoundError("approved assessment model profile not found")
        return profile
