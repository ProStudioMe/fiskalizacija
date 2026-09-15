"""Testovi ne smiju zvati živi EXTFISK — forsiraj mock partner."""
import os

os.environ["SEPKO_PARTNER_MODE"] = "mock"

from sepko.config import get_settings  # noqa: E402

get_settings.cache_clear()
