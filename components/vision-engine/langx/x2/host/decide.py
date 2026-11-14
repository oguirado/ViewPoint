#!/usr/bin/env python3
"""Render the canonical LANG-X X2 comparison JSON as a decision report.

Input contract: langx/x2/host/compare.py:237-255,279-310,337-358 at
bridge/integration. VM labeling: langx/x2/host/README.md:30 at that ref.
"""

import argparse
import hashlib
import html
import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from statistics import median


# Fixed decision rules. The gap is a reduction relative to the slower median.
MIN_QUALIFYING_SEEDS = 2
MIN_STEPS_GAP_PERCENT = 20
LANGUAGES = ("py", "ts")
TIMING_FIELDS = ("launch_ms_median", "steps_ms_median")
TIMING_LABEL = "VM"


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _cell(value):
    """Keep source text inside one Markdown table cell or paragraph line."""
    if value is None:
        return "AUSENTE"
    text = html.escape(str(value), quote=False)
    return (text.replace("\\", "\\\\").replace("|", "\\|")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>"))


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    if not rows:
        rows = [[None] * len(headers)]
    lines.extend("| " + " | ".join(_cell(cell) for cell in row) + " |"
                 for row in rows)
    return lines


def _language(seed):
    prefix = seed.partition("_")[0]
    return prefix if prefix in LANGUAGES else None


def _determinism(seed, status, page):
    if status == "ABSENT":
        return "ABSENT"
    if status != "PRESENT":
        return "AUSENTE"
    value = _mapping(page.get("determinism")).get(seed)
    if value is True:
        return "YES"
    if value is False:
        return "NO"
    return "AUSENTE"


def _conformance(seed, status, page):
    if status == "ABSENT":
        return "ABSENT"
    if status != "PRESENT":
        return "AUSENTE"
    conformance = _mapping(page.get("conformance"))
    dissent = conformance.get("dissenting_seeds")
    if not isinstance(dissent, list):
        return "AUSENTE"
    if seed in dissent:
        return "DISSENT"
    majority = conformance.get("majority_sha256")
    if (not isinstance(majority, str) or not majority
            or _determinism(seed, status, page) != "YES"):
        return "AUSENTE"
    return "YES"


def _milliseconds(value):
    # Missing or unusable readings never become zero, or disappear from a cohort.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    number = Decimal(str(value))
    return Fraction(number) if number.is_finite() and number >= 0 else None


def _cohort_median(seeds, timing, field):
    values = [_milliseconds(_mapping(timing.get(seed)).get(field))
              for seed in seeds]
    if not values or any(value is None for value in values):
        return None
    return median(values)


def _number(value):
    if value is None:
        return "AUSENTE"
    # JSON decimal values and their medians have finite decimal expansions.
    # Render them exactly, independent of floating-point or Decimal contexts.
    denominator = value.denominator
    powers = []
    for factor in (2, 5):
        power = 0
        while denominator % factor == 0:
            denominator //= factor
            power += 1
        powers.append(power)
    if denominator != 1:
        # Percentage gaps can repeat; retain their exact rational value.
        return str(value)
    places = max(powers)
    scaled = abs(value.numerator) * (10 ** places // value.denominator)
    digits = str(scaled).zfill(places + 1)
    text = (digits[:-places] + "." + digits[-places:]).rstrip("0") if places else digits
    return ("-" if value < 0 else "") + text


def _decision(seeds, pages, timing, cohorts, medians):
    if not seeds:
        return "AUSENTE", "No seeds are available in the report."
    if not timing:
        return "AUSENTE", "No timing is available in the report."
    if not pages:
        return "AUSENTE", "No pages are available to establish qualification."
    qualified = [language for language in LANGUAGES
                 if len(cohorts[language]) >= MIN_QUALIFYING_SEEDS]
    if not qualified:
        return "AUSENTE", "No language meets the qualification rule."
    if any(medians[language]["steps_ms_median"] is None for language in qualified):
        return "AUSENTE", "Step timing is incomplete for a qualifying language."
    if len(qualified) == 1:
        return qualified[0], "Only this language qualifies and has complete step timing."

    ranked = sorted(qualified, key=lambda language:
                    (medians[language]["steps_ms_median"], language))
    faster, slower = ranked[0], ranked[-1]
    fast = medians[faster]["steps_ms_median"]
    slow = medians[slower]["steps_ms_median"]
    if fast == slow:
        return "TIE", "Step medians are equal; the operator must decide."
    # slow is positive here, since valid durations are nonnegative and unequal.
    gap = (slow - fast) * 100 / slow
    description = (f"Computed steps_ms_median ({TIMING_LABEL}) gap relative to "
                   f"the slower language: {_number(gap)}%.")
    if fast * 100 <= slow * (100 - MIN_STEPS_GAP_PERCENT):
        return faster, description + " The fixed gap threshold is met."
    return "TIE", description + " Below the fixed threshold; the operator must decide."


def render_report(report, report_sha256, report_path="report.json"):
    """Render without mutating input; the caller supplies its exact-byte hash.

    Language timing is the median of reported per-seed medians for seeds that
    pass every page. Missing values make that metric unavailable. A sole
    qualifying language wins with complete steps timing; launch is descriptive.
    """
    if not isinstance(report, dict):
        raise ValueError("compare report must be a JSON object")
    seeds = _mapping(report.get("seeds"))
    pages = _mapping(report.get("pages"))
    timing = _mapping(report.get("timing"))
    seed_names = sorted(seeds)
    page_names = sorted(pages)

    conformance_rows = []
    determinism_rows = []
    for page_name in page_names:
        page = _mapping(pages[page_name])
        majority = _mapping(page.get("conformance")).get("majority_sha256")
        for seed in seed_names:
            status = seeds[seed]
            details = _mapping(_mapping(page.get("dissenting_details")).get(seed))
            if status == "ABSENT":
                details = {}
            conformance_rows.append([
                page_name, seed, status, majority, _conformance(seed, status, page),
                details.get("first_differing_byte_offset"),
                details.get("first_differing_json_path"),
            ])
            determinism_rows.append([page_name, seed, _determinism(seed, status, page)])

    cohorts = {
        language: [seed for seed in seed_names
                   if _language(seed) == language and page_names
                   and all(_conformance(seed, seeds[seed], _mapping(pages[page])) == "YES"
                           for page in page_names)]
        for language in LANGUAGES
    }
    medians = {
        language: {field: _cohort_median(cohorts[language], timing, field)
                   for field in TIMING_FIELDS}
        for language in LANGUAGES
    }
    timing_rows = [
        [language, ", ".join(cohorts[language]) or None,
         *[_number(medians[language][field]) for field in TIMING_FIELDS]]
        for language in LANGUAGES
    ]
    qualification_rows = [
        [language, len(cohorts[language]),
         "YES" if len(cohorts[language]) >= MIN_QUALIFYING_SEEDS else "NO"]
        for language in LANGUAGES
    ]
    decision, explanation = _decision(seeds, pages, timing, cohorts, medians)

    lines = [
        "# LANG-X X2 Decision Report", "", "## Inputs", "",
        f"Report: {_cell(report_path)}", "",
        f"SHA-256 (exact report bytes): {_cell(report_sha256)}", "",
        "CITED source values: the input report's seeds, pages, and timing mappings. "
        "Missing evidence is AUSENTE; explicit missing-seed status is ABSENT.", "",
        "## Conformance", "",
        "CITED: `pages.<page>.conformance.majority_sha256` and "
        "`pages.<page>.conformance.dissenting_seeds`; "
        "`pages.<page>.dissenting_details.<seed>.first_differing_byte_offset` and "
        "`pages.<page>.dissenting_details.<seed>.first_differing_json_path`. "
        "The supplied majority and details are preserved.", "",
        *_table(["Page", "Seed", "Status", "Majority SHA-256", "Conformance",
                 "First differing byte offset", "First differing JSON path"],
                conformance_rows), "",
        "## Determinism", "",
        "CITED: `pages.<page>.determinism.<seed>` and `seeds.<seed>`.", "",
        *_table(["Page", "Seed", "Deterministic"], determinism_rows), "",
        f"## Timing ({TIMING_LABEL})", "",
        "Computed language medians of CITED per-seed medians from "
        "`timing.<seed>.launch_ms_median` and `timing.<seed>.steps_ms_median`. "
        "Only seeds passing every page enter the cohort. These are not pooled "
        "raw-sample medians. A missing or invalid value in the cohort makes "
        "that metric AUSENTE; missing launch timing does not block a step decision.", "",
        *_table(["Language", "Qualifying seeds",
                 *[f"{field} ({TIMING_LABEL})" for field in TIMING_FIELDS]], timing_rows), "",
        "## Decision", "",
        f"Fixed rules: at least {MIN_QUALIFYING_SEEDS} seeds of a language must be "
        "PRESENT, deterministic, and conform to the reported majority on every page. "
        "The same seeds must pass all pages. Language prefixes are py and ts. "
        "A sole qualifying language wins with complete step timing. When both "
        f"qualify, the lower steps_ms_median ({TIMING_LABEL}) wins only for a gap "
        f"of at least {MIN_STEPS_GAP_PERCENT}% relative to the slower median "
        "((slower - faster) / slower). Otherwise the operator decides a TIE. "
        "No qualifying language, no seeds/pages/timing, or incomplete qualifying "
        "step timing yields AUSENTE.", "",
        *_table(["Language", "Seeds passing every page", "Qualifies"], qualification_rows), "",
        f"Decision: {decision}", "", explanation, "",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path, help="Canonical compare JSON")
    parser.add_argument("--out", required=True, type=Path, help="Output decision Markdown")
    args = parser.parse_args(argv)
    try:
        if args.report.resolve() == args.out.resolve():
            raise ValueError("--report and --out must be different paths")
        report_bytes = args.report.read_bytes()
        report = json.loads(report_bytes, parse_float=Decimal)
        rendered = render_report(report, hashlib.sha256(report_bytes).hexdigest(), str(args.report))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(rendered.encode("utf-8"))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
