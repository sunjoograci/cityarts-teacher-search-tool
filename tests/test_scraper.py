from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scraper import resolution_method_for_email


def test_resolution_method_for_real_email():
    assert resolution_method_for_email("jane.smith@usd123.org") == "scraped"


def test_resolution_method_for_contact_form_url():
    assert resolution_method_for_email("https://www.usd123.org/staff/directory") == "send_message_button"


def test_resolution_method_for_nothing_found():
    assert resolution_method_for_email(None) == "unresolved"
    assert resolution_method_for_email("") == "unresolved"
