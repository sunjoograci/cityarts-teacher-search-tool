from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.email_decoder import (
    decode_cf_email,
    decode_textual_munge,
    decode_onclick_email,
    generate_pattern_candidates,
    infer_from_verified_pattern,
)


def _cf_encode(email: str, key: int = 0x2A) -> str:
    """Mirror of Cloudflare's own encoder, used to build test fixtures."""
    data = bytes([key]) + bytes(b ^ key for b in email.encode())
    return data.hex()


def test_cf_email_decode_roundtrip():
    encoded = _cf_encode("teacher@example.edu")
    result = decode_cf_email(encoded)
    assert result.email == "teacher@example.edu"
    assert result.source == "CF_DECODED"


def test_cf_email_decode_garbage_input():
    result = decode_cf_email("zz")
    assert result.email is None


def test_textual_munge_at_dot():
    result = decode_textual_munge("Contact: jsmith [at] burnhamwood [dot] org")
    assert result.email == "jsmith@burnhamwood.org"
    assert result.source == "JS_DECODED"


def test_textual_munge_parens_variant():
    result = decode_textual_munge("jsmith(at)burnhamwood.org")
    assert result.email is not None
    assert "@" in result.email


def test_textual_munge_plain_email_passthrough():
    result = decode_textual_munge("reach me at jsmith@burnhamwood.org please")
    assert result.email == "jsmith@burnhamwood.org"
    assert result.source == "MAILTO"


def test_onclick_mailto_literal():
    result = decode_onclick_email("location.href='mailto:jsmith@burnhamwood.org'")
    assert result.email == "jsmith@burnhamwood.org"


def test_onclick_concat_literals():
    result = decode_onclick_email("sendMail('jsmith' + '@' + 'burnhamwood.org')")
    assert result.email == "jsmith@burnhamwood.org"


def test_onclick_computed_value_declines_to_guess():
    result = decode_onclick_email("sendMail(buildAddr(user.id))")
    assert result.email is None


def test_pattern_candidates():
    candidates = generate_pattern_candidates("Jane", "Smith", "burnhamwood.org")
    assert "jane.smith@burnhamwood.org" in candidates
    assert "jsmith@burnhamwood.org" in candidates


def test_inference_requires_consistent_pattern():
    result = infer_from_verified_pattern(
        "Jane", "Smith", "burnhamwood.org",
        verified_addresses=["a.jones@burnhamwood.org", "b.lee@burnhamwood.org"],
    )
    assert result.email == "jane.smith@burnhamwood.org"
    assert result.source == "INFERRED"
    assert result.confidence == "low"


def test_inference_refuses_on_inconsistent_pattern():
    result = infer_from_verified_pattern(
        "Jane", "Smith", "burnhamwood.org",
        verified_addresses=["a.jones@burnhamwood.org", "blee@burnhamwood.org"],
    )
    assert result.email is None
