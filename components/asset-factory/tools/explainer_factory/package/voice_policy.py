# SPDX-License-Identifier: AGPL-3.0-only
"""Local media voice admission policy shared with the intake CLI."""

import re


KNOWN_FIXTURE_WAV_DIGESTS = frozenset({
    "df45e52ea3d3a15e17e279721507505eab8fe6ee7a420d78492bc5c8f6bf893c",
    "44f4f38856b7a8503b8feb9e2b85a04faa622c924f2d55770eed259d781147ce",
    "94e1ee5309d085ca5ba5f425f59657927c12bd46678f3a00deb3f59c73851a2d",
})
KNOWN_FIXTURE_PCM_DIGESTS = frozenset({
    "ef7135405569c26c2cc7e95a5b3bf0dd2768338377a8c08f81808029c2a06be4",
    "2340b93c63ecac3b2c9a54ddaf87e7bd730c185756f856da82cf3f8644f71253",
})

FORBIDDEN_PRODUCT_DOMAINS = frozenset({
    "tasklight_product_narrator",
    "firebase-hybrid",
    "firebase-ondevice",
    "firebase-cloud",
    "gemini-nano",
    "gemini-4-hydrogen",
    "rules-summary",
})
LEGACY_VOICE_DOMAIN = "film_explainer_voiceover"
LEGACY_VOICE_PURPOSE = "tasklight_hackathon_explainer"


def allowed_domain(value):
    return (isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", value)
            and value not in FORBIDDEN_PRODUCT_DOMAINS)


def valid_purpose(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip()
