# SPDX-License-Identifier: AGPL-3.0-only
"""The orchestrator's comparison of a sealed first-viewing account with the intended action.

protocol/RUBRIC.md assigns this match to the orchestrator, and a viewer is never asked whether
it recognized the action. Whether an account names the action is a judgment, so the comparison
is made by an independent judge seat over the sealed account only (rpr_panel, stage `match`).
What this module does is hold the judge to the account:

  an element counts as named only when the judgment carries a quotation that is copied verbatim
  from the sealed account of that viewer, is affirmative, and sits in the field and the segment
  the judgment names. The orchestrator derives the match from the validated judgment.

A lexical comparison was tried first and withdrawn: on the sealed accounts of the first run it
counted denials as the action (evidence/withdrawn/RETROACTIVE_DIAGNOSTIC.json).
"""
import re
import unicodedata

import rpr_common as C

SCHEMA = "rp4.action-contract.v2"
FIELDS = ("account", "events", "evidence")  # `unclear` lists what a viewer could not make out: never quoted
MIN_QUOTE = 12
NEGATION = re.compile(r"\b(?:not|never|no|nothing|none|cannot|without)\b|n't\b", re.IGNORECASE)
SENTENCE = re.compile(r"(?<=[.!?\u2026])\s+|\n+")
QUOTE_FAILURES = ("judgment_shape", "quote_not_verbatim", "quote_negated", "quote_too_short", "quote_field_invalid")


class BadContract(Exception):
    """The contract is not an rp4.action-contract.v2 document. Process exit 2."""


def compile_contract(contract):
    """Validate an action contract and return its elements."""
    if not isinstance(contract, dict) or contract.get("schema") != SCHEMA or not isinstance(contract.get("elements"), list):
        raise BadContract("an action contract has schema rp4.action-contract.v2 and a list of elements")
    elements, ids = [], set()
    for element in contract["elements"]:
        if (not isinstance(element, dict) or not isinstance(element.get("id"), str) or not element["id"]
                or not isinstance(element.get("label"), str) or not element["label"]):
            raise BadContract(f"element {element.get('id')!r} needs an id and a label")
        ids.add(element["id"])
        elements.append({"id": element["id"], "label": element["label"], "required": bool(element.get("required", True)),
                         "after": element.get("after")})
    if not any(item["required"] for item in elements):
        raise BadContract("at least one element is required")
    for element in elements:
        if element["after"] is not None and (element["after"] == element["id"] or element["after"] not in ids):
            raise BadContract(f"element {element['id']!r} follows unknown element {element['after']!r}")
    return elements


def load_contract(path):
    try:
        return C.read_json(path)
    except (C.Harness, ValueError):
        raise BadContract(f"{path} is not a readable JSON contract")


def normalise(text):
    text = unicodedata.normalize("NFKC", str(text)).replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", text).strip()


def field_text(seals, segment, field, refuse):
    """One field of one segment of the sealed account, or a refusal naming what is wrong."""
    if not isinstance(segment, int) or isinstance(segment, bool) or not 0 <= segment < len(seals):
        raise C.Refused("quote_field_invalid", f"segment {segment!r} is not a sealed segment of this viewer", refuse)
    if field not in FIELDS:
        raise C.Refused("quote_field_invalid", f"{field!r} is not account, events or evidence", refuse)
    response = seals[segment][1]
    return normalise({"account": lambda: response["account"],
                      "events": lambda: " ".join(event["description"] for event in response["events"]),
                      "evidence": lambda: " ".join(item["observation"] for item in response["evidence"])}[field]())


BOUNDARIES = (". ", "! ", "? ", "; ")


def sentence_of(text, position):
    """The sentence of `text` that holds the quote starting at `position`."""
    starts = [text.rfind(mark, 0, position) for mark in BOUNDARIES]
    start = max(starts) + 2 if max(starts) >= 0 else 0
    ends = [found.start() for mark in BOUNDARIES if (found := re.search(re.escape(mark), text[position:]))]
    return text[start:position + min(ends) + 1] if ends else text[start:]


def verify_judgment(judgment, seals, contract, refuse=None):
    """Check a judgment against the sealed account it claims to describe and derive the match."""
    refuse = refuse or ["python3", "-I", str(C.ROOT / "rp_review.py"), "score"]
    elements = contract if isinstance(contract, list) else compile_contract(contract)
    ids = [element["id"] for element in elements]
    claimed = judgment.get("elements")
    if (not isinstance(claimed, dict) or sorted(claimed) != sorted(ids)
            or not isinstance(judgment.get("order_ok"), bool)):
        raise C.Refused("judgment_shape", f"a judgment decides exactly the elements {ids} and states order_ok", refuse)
    outcome, missing = {}, []
    for element in elements:
        item = claimed[element["id"]]
        if (not isinstance(item, dict) or set(item) != {"named", "quote", "segment", "field"}
                or not isinstance(item["named"], bool) or not isinstance(item["quote"], str)):
            raise C.Refused("judgment_shape", f"element {element['id']!r} needs named, quote, segment and field", refuse)
        entry = {"required": element["required"], "named": item["named"], "label": element["label"]}
        if item["named"]:
            text = field_text(seals, item.get("segment"), item.get("field"), refuse)
            quote = normalise(item["quote"]).strip(" '\"")
            if len(quote) < MIN_QUOTE:
                raise C.Refused("quote_too_short", f"element {element['id']!r}: a quotation names at least {MIN_QUOTE} characters", refuse)
            position = text.find(quote)
            if position < 0:
                raise C.Refused("quote_not_verbatim", f"element {element['id']!r}: the quotation is not copied from the sealed account", refuse)
            sentence = sentence_of(text, position)
            if NEGATION.search(sentence):
                raise C.Refused("quote_negated", f"element {element['id']!r}: the quotation is a denial or a negation", refuse)
            entry.update(quote=quote, segment=item["segment"], field=item["field"])
        if element["required"] and not item["named"]:
            missing.append(element["id"])
        outcome[element["id"]] = entry
    if not judgment["order_ok"]:
        missing.append("order")
    return {"match": not missing, "missing": missing, "elements": outcome, "order_ok": judgment["order_ok"],
            "method": "independent judge over the sealed account; every quotation verified verbatim and affirmative by the orchestrator"}


def judge_token(movie_sha, manifest_sha, slot, blind_sha, nonce):
    return C.sha_text(f"{C.VERSION}|judge|{movie_sha}|{manifest_sha}|{slot}|{blind_sha}|{nonce}")[:16]
