"""Per-vendor-product knowledge, authored once and reused by every capability."""

from .meridian_core import MERIDIAN_CORE, AppProfile, PROFILES, profile_for

__all__ = ["AppProfile", "MERIDIAN_CORE", "PROFILES", "profile_for"]
