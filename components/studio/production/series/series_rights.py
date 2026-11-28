# SPDX-License-Identifier: AGPL-3.0-only
"""Apply operator-pinned provenance and destination policy to immutable asset IDs."""
import sqlite3
from collections import Counter
from contextlib import closing

from series_contract import (
    JSON_LIMIT, Refusal, asset_file, digest, fields, file_digest, identifier,
    identifiers, need, parse_json, sha,
)

POLICY = "series-rights/v1"


class Rights:
    def __init__(self, args):
        self.destination = args.destination
        identifier(self.destination, "destination")
        sha(args.policy_sha256, "policy pin")
        with args.policy.open("rb") as source:
            raw = source.read(JSON_LIMIT + 1)
        need(digest(raw) == args.policy_sha256, "policy_digest_mismatch", args.policy.name)
        policy = parse_json(raw, args.policy.name)
        fields(policy, "schema mode destinations inventory_sha256 grants", "rights policy")
        need(policy["schema"] == POLICY, "unsupported_schema", "rights policy")
        need(policy["mode"] == ("fixture" if args.fixture else "production"),
             "policy_mode_mismatch", policy["mode"])
        self.pin = args.policy_sha256
        self.evidence = args.evidence
        destinations = policy["destinations"]
        need(isinstance(destinations, dict), "invalid_contract", "destinations")
        for key, backend in destinations.items():
            identifier(key, "destination")
            need(backend in ("local", "drive"), "unknown_backend", key)
            if args.fixture:
                need(key == f"fixture-{backend}", "fixture_destination_invalid", key)
        need(self.destination in destinations, "destination_unknown", self.destination)
        self.backend = destinations[self.destination]
        self.grants = policy["grants"]
        need(isinstance(self.grants, dict), "invalid_contract", "grants")
        self.inventory = args.inventory
        self.rows = {}
        pin = policy["inventory_sha256"]
        if pin is not None:
            sha(pin, "inventory pin")
            need(self.inventory is not None, "inventory_required", "rights policy")
        if self.inventory is not None:
            need(pin is not None, "inventory_pin_absent", "rights policy")
            need(file_digest(self.inventory) == pin, "inventory_digest_mismatch", self.inventory.name)
            with self.connect() as db:
                self.rows = dict((row[0], row[1:]) for row in db.execute(
                    "SELECT id, kind, rights_scope, verdict FROM curation ORDER BY id"))
            need(file_digest(self.inventory) == pin, "inventory_digest_mismatch", self.inventory.name)
        self.inventory_pin = pin

    def connect(self):
        location = self.inventory.resolve().as_uri() + "?mode=ro&immutable=1"
        return closing(sqlite3.connect(location, uri=True))

    def search(self, query):
        need(self.inventory is not None, "inventory_required", "search")
        with self.connect() as db:
            try:
                rows = db.execute(
                    "SELECT id FROM curation_fts WHERE curation_fts MATCH ? ORDER BY rank, id",
                    (query,)).fetchall()
            except sqlite3.OperationalError as exc:
                raise Refusal("invalid_search_query", str(exc)) from exc
        need(file_digest(self.inventory) == self.inventory_pin,
             "inventory_digest_mismatch", self.inventory.name)
        return [row[0] for row in rows]

    def receipt(self, pin, *, structured=True):
        sha(pin, "evidence pin")
        need(self.evidence is not None, "rights_evidence_absent", pin)
        try:
            path = asset_file(self.evidence, pin, pin)
            if not structured:
                need(file_digest(path) == pin, "rights_evidence_digest_mismatch", pin)
                return None
            with path.open("rb") as source:
                raw = source.read(JSON_LIMIT + 1)
        except FileNotFoundError as exc:
            raise Refusal("rights_evidence_absent", pin) from exc
        except Refusal as exc:
            if exc.reason == "asset_missing":
                raise Refusal("rights_evidence_absent", pin) from exc
            raise
        need(digest(raw) == pin, "rights_evidence_digest_mismatch", pin)
        return parse_json(raw, pin)

    def check_factory(self, asset_pin, receipt):
        fields(receipt, "schema asset_sha256 inputs", "generation receipt")
        need(receipt["schema"] == "series-generation/v1" and receipt["asset_sha256"] == asset_pin,
             "generation_receipt_mismatch", asset_pin)
        inputs = receipt["inputs"]
        need(isinstance(inputs, dict) and inputs, "factory_inputs_absent", asset_pin)
        for input_pin, clearance_pin in inputs.items():
            sha(input_pin, "factory input pin")
            clearance = self.receipt(clearance_pin)
            fields(clearance, "schema input_sha256 status destinations", "operator clearance")
            need(clearance["schema"] == "series-input-clearance/v1"
                 and clearance["input_sha256"] == input_pin, "input_clearance_mismatch", input_pin)
            identifiers(clearance["destinations"], "input clearance destinations")
            need(clearance["status"] == "approved" and self.destination in clearance["destinations"],
                 "input_not_cleared", input_pin)

    def check_cc0(self, asset, receipt):
        fields(receipt, "schema asset_sha256 license source license_document", "CC0 review")
        source = receipt["source"]
        need(receipt["schema"] == "series-cc0-review/v1" and asset["kind"] == "motion"
             and receipt["asset_sha256"] == asset["sha256"] and receipt["license"] == "CC0-1.0"
             and isinstance(source, str) and source.strip(), "cc0_review_mismatch", asset["sha256"])
        self.receipt(receipt["license_document"], structured=False)

    def decision(self, key, asset=None):
        row = self.rows.get(key)
        result = {"id": key, "kind": row[0] if row else asset["kind"] if asset else None,
                  "provenance": "unknown", "status": "withheld",
                  "upstream_rights": row[1] if row else None,
                  "upstream_verdict": row[2] if row else None}
        try:
            origin = self._approval(key, asset, row)
        except Refusal as exc:
            return {**result, "reason": exc.reason}
        return {**result, "provenance": origin, "status": "eligible", "reason": "reviewed_evidence"}

    def _approval(self, key, asset, row):
        need(row is None or (row[1] != "withheld" and row[2] != "WITHHELD"),
             "upstream_withheld", key)
        need(key in self.grants, "rights_evidence_absent", key)
        grant = self.grants[key]
        fields(grant, "asset_sha256 origin status destinations receipt", key)
        need(grant["status"] != "revoked", "grant_revoked", key)
        need(grant["status"] == "approved", "grant_unapproved", key)
        identifiers(grant["destinations"], f"{key} grant destinations")
        need(self.destination in grant["destinations"], "grant_destination_mismatch", key)
        need(asset is not None, "asset_digest_absent", key)
        need(row is None or row[0] == asset["kind"], "asset_kind_mismatch", key)
        need(grant["asset_sha256"] == asset["sha256"], "grant_asset_mismatch", key)
        origin = grant["origin"]
        need(origin in ("factory-cleared", "cc0-mocap"), "rights_evidence_absent", key)
        receipt = self.receipt(grant["receipt"])
        if origin == "factory-cleared":
            self.check_factory(asset["sha256"], receipt)
        else:
            self.check_cc0(asset, receipt)
        return origin

    def require(self, key, asset):
        result = self.decision(key, asset)
        need(result["status"] == "eligible", result["reason"], key)
        return result

    def report(self, assets, *, query=None, eligible_only=False, limit=None):
        candidates = self.search(query) if query is not None else sorted(self.rows.keys() | assets.keys())
        decisions = [self.decision(key, assets.get(key)) for key in candidates]
        if eligible_only:
            decisions = [row for row in decisions if row["status"] == "eligible"]
        result = {"destination": self.destination, "policy_sha256": self.pin,
                  "inventory_sha256": self.inventory_pin, "count": len(decisions),
                  "status": dict(Counter(row["status"] for row in decisions)),
                  "provenance": dict(Counter(row["provenance"] for row in decisions)),
                  "upstream_rights": dict(Counter(row["upstream_rights"] or "absent" for row in decisions)),
                  "reasons": dict(Counter(row["reason"] for row in decisions))}
        if limit is not None:
            result["assets"] = decisions[:limit]
        return result
