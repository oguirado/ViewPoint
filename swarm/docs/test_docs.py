"""Automated documentation verification and honesty tests for ViewPoint Swarm.

Tests:
  - D1: Committed-only repository relative link and path checker (+ must-fail control).
  - D2: Proof index SHA-256 re-hash verification against disk and PROVENANCE.md (+ must-fail control).
  - D3: Banned overclaim phrases and actuation qualifier scanner (+ must-fail control).
  - D4: Two-track split enforcement between Track H (Gemini) and Track P (Claude) (+ must-fail control).
  - Additional: Verbatim honest boundary statement and projection guarantee verification.
"""

import hashlib
import os
import platform
import re
import subprocess
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Mandatory exact honesty boundary statement
HONEST_BOUNDARY_STATEMENT = (
    "Browser capsules (measured 13 Nov) run as containers with their own display. "
    "Today's Blender 4.0.2 swarm capsule runs as host processes inside `systemd-run` "
    "memory-capped scopes on a private Xvfb display (`:2`) under one shared UID: per-run bridge nonces "
    "and prefix claims catch accidental cross-writes, stale frames, and buggy agents, and are not an OS "
    "security boundary against a hostile same-UID process. Per-agent UIDs/containers are roadmap work."
)

# Mandatory exact projection guarantee statement
PROJECTION_GUARANTEE_STATEMENT = (
    "Projection to OpenSwitchboard (`viewpoint-capsule`) and TaskLight (`context-wall`) is best-effort; "
    "`swarm/run/events.jsonl` is the authoritative local record of truth, audited at end-of-run by `projector.py --finalize` (via `reconciler.py`)."
)

# Banned phrases that must NEVER appear in documentation or hackathon readme section
BANNED_PHRASES = [
    r"\bsecure against agents\b",
    r"\bsandboxed agents\b",
    r"\btamper[-\s]+proof\b",
    r"\bauthenticated agents\b",
    r"\bsealed[-\s]+bid\b",
    r"\btruthful auction\b",
    r"\bguaranteed delivery\b",
    r"\bexactly[-\s]+once\b",
    r"\bat[-\s]+least[-\s]+once\b",
    r"\brewinds everything\b",
    r"\baudit ok\b",
    r"\bisolated\b",
]

ORIGINAL_README_HEADINGS = [
    "## Problem",
    "## What ViewPoint does",
    "## Where Gemini fits",
    "## What is in this repo vs private",
    "## Relationship to MetaViewer",
    "## Quick start",
    "## Measured results",
    "## Honest limits",
    "## License",
]

KERNEL_MEASURED_RE = re.compile(
    r"`?(\d+\.\d+\.\d+-\d+-[a-zA-Z0-9._-]+)`?(?:[^.\n]{0,30})\[MEASURED\]"
)
STALE_RECONCILER_FINALIZE_RE = re.compile(
    r"\breconciler(?:\.py)?[`\s\(\)]+--finalize\b"
)

DOC_FILES = [
    "swarm/docs/ONE_PAGER_IDEA.md",
    "swarm/docs/ONE_PAGER_TEAM.md",
    "swarm/docs/PROOF.md",
    "swarm/docs/OPERATOR_CHECKLIST.md",
]


def get_allowed_files(repo_root: str) -> set[str]:
    """Return union of git-tracked files in HEAD and newly created swarm/docs/* files."""
    try:
        git_out = subprocess.check_output(
            ["git", "ls-tree", "-r", "HEAD", "--name-only"],
            cwd=repo_root,
        ).decode("utf-8")
        tracked = set(git_out.splitlines())
    except Exception:
        tracked = set()

    docs_created = {
        "swarm/docs/__init__.py",
        "swarm/docs/ONE_PAGER_IDEA.md",
        "swarm/docs/ONE_PAGER_TEAM.md",
        "swarm/docs/PROOF.md",
        "swarm/docs/OPERATOR_CHECKLIST.md",
        "swarm/docs/test_docs.py",
    }
    return tracked.union(docs_created)


def check_path_exists_in_allowed(resolved_path: str, allowed_files: set[str]) -> bool:
    """Check if resolved path is in allowed files, is a valid prefix/dir, or is an exempted target."""
    clean = resolved_path.strip().strip("/")
    if not clean:
        return True
    if clean in allowed_files:
        return True
    # Check if it matches a directory containing allowed files
    dir_prefix = clean + "/"
    if any(f.startswith(dir_prefix) for f in allowed_files):
        return True
    return False


def expand_braces(path_str: str) -> list[str]:
    """Expand bash-style {a,b} brace notation into individual candidate paths."""
    match = re.search(r"\{([^{}]+)\}", path_str)
    if not match:
        return [path_str]
    prefix = path_str[: match.start()]
    suffix = path_str[match.end() :]
    options = [opt.strip() for opt in match.group(1).split(",")]
    expanded = []
    for opt in options:
        expanded.extend(expand_braces(prefix + opt + suffix))
    return expanded


def extract_readme_hackathon_section(readme_content: str) -> str:
    """Return the '## Hackathon build (swarm-1114)' section up to '## License'."""
    h_idx = readme_content.find("## Hackathon build (swarm-1114)")
    if h_idx == -1:
        return ""
    lic_idx = readme_content.find("\n## License", h_idx)
    if lic_idx == -1:
        return readme_content[h_idx:]
    return readme_content[h_idx:lic_idx]


def validate_kernel_versions(
    content: str,
    source_name: str,
    expected_kernel: str,
) -> list[str]:
    """Validate that any [MEASURED] kernel version matches the live host kernel."""
    errors = []
    for line_idx, line in enumerate(content.splitlines(), 1):
        for found_kernel in KERNEL_MEASURED_RE.findall(line):
            if found_kernel != expected_kernel:
                errors.append(
                    f"{source_name}:{line_idx}: kernel '{found_kernel}' [MEASURED] != host '{expected_kernel}'"
                )
    return errors


def scan_stale_reconciler_finalize(content: str, source_name: str) -> list[str]:
    """Detect forbidden references to non-existent 'reconciler --finalize' CLI invocations."""
    errors = []
    for line_idx, line in enumerate(content.splitlines(), 1):
        m = STALE_RECONCILER_FINALIZE_RE.search(line)
        if m:
            errors.append(
                f"{source_name}:{line_idx}: non-existent CLI flag reference '{m.group(0)}' (use projector.py --finalize)"
            )
    return errors


def validate_links_in_content(
    content: str,
    source_rel_path: str,
    allowed_files: set[str],
) -> list[str]:
    """Scan content for Markdown links and backticked repository paths, returning error list."""
    errors = []
    lines = content.splitlines()
    source_dir = os.path.dirname(source_rel_path)

    for line_idx, line in enumerate(lines, 1):
        # 1. Markdown links [text](target)
        for text, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", line):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            clean_target = target.split("#")[0].split("?")[0].strip()
            if not clean_target:
                continue

            # Special case for swarm/viewer marked target or pending
            if "swarm/viewer" in clean_target and (
                "[TARGET]" in line or "pending" in line or "Wave 2" in line
            ):
                continue

            for expanded_target in expand_braces(clean_target):
                resolved = os.path.normpath(os.path.join(source_dir, expanded_target))
                if not check_path_exists_in_allowed(resolved, allowed_files):
                    errors.append(
                        f"{source_rel_path}:{line_idx}: target '{target}' resolved to '{resolved}' not found in HEAD"
                    )

        # 2. Backticked repo paths `path/...` (including {a,b} brace notation)
        for raw_bt in re.findall(r"`([a-zA-Z0-9_\-\.\/\{\},]+)`", line):
            for bt in expand_braces(raw_bt):
                # Check repo relative roots
                if bt.startswith(
                    ("swarm/", "product/", "docs/", "gateway/", "launcher/", "tools/", "mcp/")
                ) or bt in ("PROVENANCE.md", "README.md", "LICENSE"):
                    # Skip only runtime directory swarm/run/; all proof/*.jsonl paths are verified against HEAD
                    if bt.startswith("swarm/run/"):
                        continue
                    # Special exemption for swarm/viewer marked target or pending
                    if "swarm/viewer" in bt and (
                        "[TARGET]" in line or "pending" in line or "Wave 2" in line
                    ):
                        continue

                    if not check_path_exists_in_allowed(bt, allowed_files):
                        errors.append(
                            f"{source_rel_path}:{line_idx}: backticked repo path '{bt}' not found in HEAD"
                        )

    return errors


def parse_proof_rows(proof_content: str) -> list[tuple[str, str, str, str, str]]:
    """Parse proof table rows: (unit, track, model, path, sha256)."""
    rows = []
    for line in proof_content.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        parts = [p.strip().strip("`") for p in line.split("|")[1:-1]]
        if len(parts) >= 5 and parts[3].endswith(".jsonl"):
            unit, track, model, path, sha = parts[0], parts[1], parts[2], parts[3], parts[4]
            rows.append((unit, track, model, path, sha))
    return rows


def parse_review_md_rows(proof_content: str) -> list[tuple[str, str, str, str, str]]:
    """Parse post-13:59 cross-family review rows: (review, model, mode, path, sha256)."""
    rows = []
    for line in proof_content.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        parts = [p.strip().strip("`") for p in line.split("|")[1:-1]]
        if len(parts) >= 5 and parts[3].endswith(".md") and parts[3].startswith("product/reviews/"):
            review, model, mode, path, sha = parts[0], parts[1], parts[2], parts[3], parts[4]
            rows.append((review, model, mode, path, sha))
    return rows


def get_tracked_proof_jsonl(repo_root: str) -> set[str]:
    """Return the exact set of git-tracked .jsonl files under swarm/proof/ and product/proof/."""
    git_out = subprocess.check_output(
        ["git", "ls-tree", "-r", "HEAD", "--name-only"],
        cwd=repo_root,
    ).decode("utf-8")
    return {
        line
        for line in git_out.splitlines()
        if re.match(r"^(swarm|product)/proof/[^/]+\.jsonl$", line)
    }


def _verify_indexed_artifact(
    path: str,
    expected_sha: str,
    kind: str,
    provenance_content: str,
    repo_root: str,
) -> list[str]:
    """Verify an indexed artifact matches on-disk SHA-256 and both basename + SHA-256 in PROVENANCE.md."""
    errors = []
    full_path = os.path.join(repo_root, path)
    if not os.path.exists(full_path):
        errors.append(f"{kind} file missing on disk: {path}")
        return errors

    with open(full_path, "rb") as f:
        actual_sha = hashlib.sha256(f.read()).hexdigest()

    if actual_sha != expected_sha:
        errors.append(
            f"SHA-256 mismatch for {path}: expected {expected_sha}, computed {actual_sha}"
        )

    filename = os.path.basename(path)
    if filename not in provenance_content:
        errors.append(f"{kind} filename {filename} not cited in PROVENANCE.md")
    if expected_sha not in provenance_content:
        errors.append(f"{kind} SHA-256 {expected_sha} for {path} not cited in PROVENANCE.md")
    return errors


def validate_proof_index(
    proof_content: str,
    provenance_content: str,
    repo_root: str,
) -> list[str]:
    """Validate proof rows for uniqueness, exact git-tracked .jsonl set, disk sha256, and PROVENANCE.md."""
    errors = []
    rows = parse_proof_rows(proof_content)
    if len(rows) < 44:
        errors.append(f"Expected at least 44 proof log rows, found {len(rows)}")

    seen_jsonl_paths: set[str] = set()
    for unit, track, model, path, expected_sha in rows:
        if path in seen_jsonl_paths:
            errors.append(f"Duplicate proof path in PROOF.md: {path}")
        seen_jsonl_paths.add(path)
        errors.extend(
            _verify_indexed_artifact(path, expected_sha, "Proof", provenance_content, repo_root)
        )

    try:
        tracked_jsonl = get_tracked_proof_jsonl(repo_root)
    except Exception as exc:
        errors.append(f"Failed to query git HEAD tracked proof logs: {exc}")
        tracked_jsonl = None

    if tracked_jsonl is not None:
        for missing in sorted(tracked_jsonl - seen_jsonl_paths):
            if re.match(r"^swarm/proof/H6_r4_[^/]+\.jsonl$", missing):
                if os.path.basename(missing) not in provenance_content:
                    errors.append(f"Post-commit proof log {missing} not cited in PROVENANCE.md")
            else:
                errors.append(f"Tracked proof log missing from PROOF.md: {missing}")
        for extra in sorted(seen_jsonl_paths - tracked_jsonl):
            if not re.match(r"^swarm/proof/H6_r4_[^/]+\.jsonl$", extra):
                errors.append(f"Untracked proof log in PROOF.md not in git HEAD: {extra}")

    seen_review_paths: set[str] = set()
    for review, model, mode, path, expected_sha in parse_review_md_rows(proof_content):
        if path in seen_review_paths:
            errors.append(f"Duplicate review path in PROOF.md: {path}")
        seen_review_paths.add(path)
        errors.extend(
            _verify_indexed_artifact(path, expected_sha, "Review", provenance_content, repo_root)
        )

    return errors


def scan_for_banned_phrases(content: str, source_name: str) -> list[str]:
    """Scan content for forbidden overclaim terms and improper parallel actuation claims."""
    violations = []
    for pattern in BANNED_PHRASES:
        m = re.search(pattern, content, re.IGNORECASE)
        if m:
            violations.append(f"{source_name}: banned phrase matched '{m.group(0)}'")

    # Check for "in parallel" specifically applied to Blender actuation/execution
    for m in re.finditer(r"\bin parallel\b", content, re.IGNORECASE):
        start = max(0, m.start() - 100)
        end = min(len(content), m.end() + 100)
        snippet = content[start:end].lower()
        if any(w in snippet for w in ("blender", "actuat", "turn", "queue", "viewport", ".blend")):
            violations.append(
                f"{source_name}: 'in parallel' used when describing Blender actuation in context: '{content[start:end]}'"
            )

    return violations


def validate_two_track_integrity(
    proof_content: str,
    readme_content: str,
) -> list[str]:
    """Validate that Track H and Track P remain strictly disjoint in models and paths."""
    errors = []
    rows = parse_proof_rows(proof_content)

    for unit, track, model, path, _ in rows:
        if track == "H":
            if not path.startswith("swarm/"):
                errors.append(f"Track H unit {unit} has non-swarm path: {path}")
            if not model.lower().startswith("gemini-"):
                errors.append(f"Track H unit {unit} has non-Gemini model: {model}")
        elif track == "P":
            if not path.startswith("product/"):
                errors.append(f"Track P unit {unit} has non-product path: {path}")
            if not model.lower().startswith("claude-"):
                errors.append(f"Track P unit {unit} has non-Claude model: {model}")
        else:
            errors.append(f"Unknown track '{track}' for unit {unit}")

    # Verify README hackathon build section adheres to two-track declaration
    h_idx = readme_content.find("## Hackathon build (swarm-1114)")
    if h_idx == -1:
        errors.append("README.md missing section '## Hackathon build (swarm-1114)'")
    else:
        h_section = readme_content[h_idx:]
        if "Track H (`swarm/`)" not in h_section:
            errors.append("README.md missing Track H declaration")
        if "Track P (`product/`)" not in h_section:
            errors.append("README.md missing Track P declaration")
        if "Exactly 0 files" not in h_section and "0 files" not in h_section:
            errors.append("README.md missing explicit 0 files written under swarm/ declaration")

    return errors


class TestDocumentationIntegrity(unittest.TestCase):
    """Test suite for ViewPoint Swarm documentation and honesty verification."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = REPO_ROOT
        cls.allowed_files = get_allowed_files(cls.repo_root)

    def test_d1_committed_links_pass(self):
        """D1 Pass: All Markdown links and backticked repo paths across docs exist in git HEAD."""
        all_errors = []
        # Check README.md
        readme_path = os.path.join(self.repo_root, "README.md")
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_text = f.read()
        all_errors.extend(validate_links_in_content(readme_text, "README.md", self.allowed_files))

        # Check swarm/docs/*.md
        for doc_rel in DOC_FILES:
            full_p = os.path.join(self.repo_root, doc_rel)
            with open(full_p, "r", encoding="utf-8") as f:
                content = f.read()
            all_errors.extend(validate_links_in_content(content, doc_rel, self.allowed_files))

        self.assertEqual(all_errors, [], f"Found invalid links/paths: {all_errors}")

    def test_d1_must_fail_control(self):
        """D1 Must-Fail Control: Planted untracked path link MUST fail verification."""
        mutant_content = (
            "Here is a reference to an uncommitted draft: [fake link](product/narrative/FAKE_UNTRACKED.md)."
        )
        errors = validate_links_in_content(mutant_content, "swarm/docs/SAMPLE.md", self.allowed_files)
        self.assertTrue(len(errors) > 0, "D1 control FAILED: mutant link was not detected!")
        self.assertTrue(any("FAKE_UNTRACKED.md" in e for e in errors))

    def test_d2_proof_hashes_pass(self):
        """D2 Pass: All proof log SHA-256 digests in PROOF.md match disk and PROVENANCE.md."""
        proof_path = os.path.join(self.repo_root, "swarm/docs/PROOF.md")
        prov_path = os.path.join(self.repo_root, "PROVENANCE.md")
        with open(proof_path, "r", encoding="utf-8") as f:
            proof_content = f.read()
        with open(prov_path, "r", encoding="utf-8") as f:
            prov_content = f.read()

        errors = validate_proof_index(proof_content, prov_content, self.repo_root)
        self.assertEqual(errors, [], f"Proof hash validation failed: {errors}")

    def test_d2_must_fail_control(self):
        """D2 Must-Fail Control: Mutated SHA-256 in PROOF.md or PROVENANCE.md MUST fail verification."""
        proof_path = os.path.join(self.repo_root, "swarm/docs/PROOF.md")
        prov_path = os.path.join(self.repo_root, "PROVENANCE.md")
        with open(proof_path, "r", encoding="utf-8") as f:
            proof_content = f.read()
        with open(prov_path, "r", encoding="utf-8") as f:
            prov_content = f.read()

        # 1. Mutate 1 character in the first SHA-256 in PROOF.md
        mutant_proof = re.sub(
            r"afb086816ce4029f07a0d75f6a465036ca1c900ff314feb8b6dbc15e1a2a9741",
            r"afb086816ce4029f07a0d75f6a465036ca1c900ff314feb8b6dbc15e1a2a9740",
            proof_content,
        )
        errors = validate_proof_index(mutant_proof, prov_content, self.repo_root)
        self.assertTrue(len(errors) > 0, "D2 control FAILED: mutated proof hash was not detected!")
        self.assertTrue(any("SHA-256 mismatch" in e for e in errors))

        # 2. Mutate 1 character in PROVENANCE.md (proof .jsonl hash) while PROOF.md still matches disk
        mutant_prov_jsonl = prov_content.replace(
            "afb086816ce4029f07a0d75f6a465036ca1c900ff314feb8b6dbc15e1a2a9741",
            "afb086816ce4029f07a0d75f6a465036ca1c900ff314feb8b6dbc15e1a2a9740",
        )
        prov_jsonl_errors = validate_proof_index(proof_content, mutant_prov_jsonl, self.repo_root)
        self.assertTrue(
            any("not cited in PROVENANCE.md" in e for e in prov_jsonl_errors),
            "D2 control FAILED: mutated PROVENANCE.md .jsonl SHA-256 was not detected!",
        )

        # 3. Mutate 1 character in PROVENANCE.md (review .md hash) while PROOF.md still matches disk
        mutant_prov_review = prov_content.replace(
            "b71d1ab3df7c7070617fb9abfcc032c8dbd94f6b555491f3a6c52becded37288",
            "b71d1ab3df7c7070617fb9abfcc032c8dbd94f6b555491f3a6c52becded37289",
        )
        prov_review_errors = validate_proof_index(proof_content, mutant_prov_review, self.repo_root)
        self.assertTrue(
            any("not cited in PROVENANCE.md" in e for e in prov_review_errors),
            "D2 control FAILED: mutated PROVENANCE.md review SHA-256 was not detected!",
        )

    def test_d3_banned_phrases_pass(self):
        """D3 Pass: Zero occurrences of banned overclaim phrases in docs or README hackathon section."""
        violations = []
        # Check README hackathon section (between ## Hackathon build and ## License)
        readme_path = os.path.join(self.repo_root, "README.md")
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_text = f.read()
        h_section = extract_readme_hackathon_section(readme_text)
        self.assertTrue(h_section, "README.md must contain '## Hackathon build (swarm-1114)'")
        violations.extend(scan_for_banned_phrases(h_section, "README.md (swarm-1114)"))

        # Check swarm/docs/*.md
        for doc_rel in DOC_FILES:
            full_p = os.path.join(self.repo_root, doc_rel)
            with open(full_p, "r", encoding="utf-8") as f:
                content = f.read()
            violations.extend(scan_for_banned_phrases(content, doc_rel))

        self.assertEqual(violations, [], f"Found banned phrases: {violations}")

    def test_d3_must_fail_control(self):
        """D3 Must-Fail Control: Planted banned phrases (hyphenated, spaced, and isolated) MUST be detected."""
        mutant_doc_1 = "The projection layer provides guaranteed delivery and projector is at-least-once."
        violations_1 = scan_for_banned_phrases(mutant_doc_1, "MUTANT_DOC_1")
        self.assertTrue(len(violations_1) > 0, "D3 control 1 FAILED: 'at-least-once' was not detected!")
        self.assertTrue(any("at-least-once" in v for v in violations_1))

        mutant_doc_2 = "The system executes with sandboxed agents and is secure against agents."
        violations_2 = scan_for_banned_phrases(mutant_doc_2, "MUTANT_DOC_2")
        self.assertTrue(len(violations_2) > 0, "D3 control 2 FAILED: 'sandboxed agents' was not detected!")
        self.assertTrue(any("sandboxed agents" in v for v in violations_2))

        mutant_doc_3 = "Agents act in parallel on the shared Blender viewport and scene."
        violations_3 = scan_for_banned_phrases(mutant_doc_3, "MUTANT_DOC_3")
        self.assertTrue(len(violations_3) > 0, "D3 control 3 FAILED: 'in parallel' was not detected!")

        for phrase in (
            "tamper proof",
            "exactly once",
            "at least once",
            "sealed bid",
            "isolated",
        ):
            violations_4 = scan_for_banned_phrases(f"The Blender capsule uses {phrase} state.", "MUTANT_DOC_4")
            self.assertTrue(
                len(violations_4) > 0,
                f"D3 control 4 FAILED: '{phrase}' was not detected!",
            )

    def test_d4_two_track_split_pass(self):
        """D4 Pass: Track H and Track P are cleanly split between Gemini and Claude models."""
        proof_path = os.path.join(self.repo_root, "swarm/docs/PROOF.md")
        readme_path = os.path.join(self.repo_root, "README.md")
        with open(proof_path, "r", encoding="utf-8") as f:
            proof_content = f.read()
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_content = f.read()

        errors = validate_two_track_integrity(proof_content, readme_content)
        self.assertEqual(errors, [], f"Two-track split validation failed: {errors}")

    def test_d4_must_fail_control(self):
        """D4 Must-Fail Control: Claiming a product/ file under Track H MUST fail validation."""
        proof_path = os.path.join(self.repo_root, "swarm/docs/PROOF.md")
        readme_path = os.path.join(self.repo_root, "README.md")
        with open(proof_path, "r", encoding="utf-8") as f:
            proof_content = f.read()
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_content = f.read()

        # Plant product/ path into Track H
        mutant_proof = proof_content + (
            "\n| `H_ROGUE` | `H` | `gemini-4-hydrogen-high` | `product/admission/admission.py` | `0000000000000000000000000000000000000000000000000000000000000000` |"
        )
        # Note: adjust row parser to see .py as .jsonl for test mutation
        mutant_proof_jsonl = mutant_proof.replace("admission.py", "admission.jsonl")
        errors = validate_two_track_integrity(mutant_proof_jsonl, readme_content)
        self.assertTrue(len(errors) > 0, "D4 control FAILED: rogue Track H file was not detected!")
        self.assertTrue(any("non-swarm path" in e for e in errors))

    def test_honest_boundary_statement_verbatim(self):
        """Verify the exact mandatory honest boundary statement is present verbatim."""
        idea_path = os.path.join(self.repo_root, "swarm/docs/ONE_PAGER_IDEA.md")
        readme_path = os.path.join(self.repo_root, "README.md")

        with open(idea_path, "r", encoding="utf-8") as f:
            idea_content = f.read()
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_content = f.read()

        self.assertIn(
            HONEST_BOUNDARY_STATEMENT,
            idea_content,
            "ONE_PAGER_IDEA.md missing verbatim honest boundary statement",
        )
        self.assertIn(
            HONEST_BOUNDARY_STATEMENT,
            readme_content,
            "README.md missing verbatim honest boundary statement",
        )

    def test_projection_guarantee_statement_verbatim(self):
        """Verify the exact mandatory projection guarantee statement is present verbatim."""
        idea_path = os.path.join(self.repo_root, "swarm/docs/ONE_PAGER_IDEA.md")
        readme_path = os.path.join(self.repo_root, "README.md")

        with open(idea_path, "r", encoding="utf-8") as f:
            idea_content = f.read()
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_content = f.read()

        self.assertIn(
            PROJECTION_GUARANTEE_STATEMENT,
            idea_content,
            "ONE_PAGER_IDEA.md missing verbatim projection guarantee statement",
        )
        self.assertIn(
            PROJECTION_GUARANTEE_STATEMENT,
            readme_content,
            "README.md missing verbatim projection guarantee statement",
        )

    def test_lp1_kernel_matches_uname_r(self):
        """LP-1: Any [MEASURED] kernel version in swarm/docs/*.md must equal platform.release()."""
        host_kernel = platform.release()
        self.assertEqual(host_kernel, os.uname().release)

        errors = []
        for doc_rel in DOC_FILES:
            full_p = os.path.join(self.repo_root, doc_rel)
            with open(full_p, "r", encoding="utf-8") as f:
                content = f.read()
            errors.extend(validate_kernel_versions(content, doc_rel, host_kernel))
        self.assertEqual(errors, [], f"Kernel version mismatch: {errors}")

        # Must-fail control: stale kernel 6.17.0-1018-nvidia [MEASURED] MUST fail
        mutant = "Running on Linux aarch64 kernel 6.17.0-1018-nvidia [MEASURED]."
        mutant_errors = validate_kernel_versions(mutant, "MUTANT_KERNEL", host_kernel)
        self.assertTrue(
            len(mutant_errors) > 0,
            "LP-1 must-fail control FAILED: stale kernel 6.17.0-1018-nvidia [MEASURED] was not detected!",
        )

    def test_lp2_cited_cli_flags_exist(self):
        """LP-2: 'reconciler --finalize' must never appear in docs/README, and projector --finalize must exist."""
        errors = []
        for doc_rel in DOC_FILES + ["README.md"]:
            full_p = os.path.join(self.repo_root, doc_rel)
            with open(full_p, "r", encoding="utf-8") as f:
                content = f.read()
            errors.extend(scan_stale_reconciler_finalize(content, doc_rel))
        self.assertEqual(errors, [], f"Stale CLI flag references found: {errors}")

        # Must-fail controls for stale flag detection
        for bad_line in (
            "Run `python3 -m swarm.projection.reconciler --finalize` at end of run.",
            "Audited by `reconciler.py --finalize`.",
            "Audited by `reconciler.py` (`--finalize`).",
        ):
            bad_hits = scan_stale_reconciler_finalize(bad_line, "MUTANT_CLI")
            self.assertTrue(
                len(bad_hits) > 0,
                f"LP-2 must-fail control FAILED for: {bad_line}",
            )

        # Verify actual projector --help exposes --finalize
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "swarm.projection.projector", "--help"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn(
            "--finalize",
            proc.stdout,
            "python3 -m swarm.projection.projector --help missing --finalize flag",
        )

    def test_bl8_proof_paths_and_brace_expansion(self):
        """BL-8 Must-Fail Controls: Uncommitted proof jsonl paths and {a,b} brace paths must fail."""
        proof_mutant = (
            "Untracked logs `swarm/proof/run_fake_9999.jsonl` and `product/proof/P4_fake_9999.jsonl`."
        )
        proof_errors = validate_links_in_content(
            proof_mutant, "swarm/docs/SAMPLE.md", self.allowed_files
        )
        self.assertTrue(
            any("swarm/proof/run_fake_9999.jsonl" in e for e in proof_errors),
            "BL-8 FAILED: untracked swarm/proof/run_* path was skipped!",
        )
        self.assertTrue(
            any("product/proof/P4_fake_9999.jsonl" in e for e in proof_errors),
            "BL-8 FAILED: untracked product/proof/P4_* path was skipped!",
        )

        brace_mutant = "See `swarm/broker/{broker,nonexistent_module}.py`."
        brace_errors = validate_links_in_content(
            brace_mutant, "swarm/docs/SAMPLE.md", self.allowed_files
        )
        self.assertTrue(
            any("swarm/broker/nonexistent_module.py" in e for e in brace_errors),
            "BL-8 FAILED: {a,b} brace expansion did not detect missing file!",
        )

    def test_bl9_no_latex_math_blocks(self):
        """BL-9: Plain-text one-pagers and README hackathon section must not use $$...$$ LaTeX blocks."""
        for doc_rel in DOC_FILES:
            full_p = os.path.join(self.repo_root, doc_rel)
            with open(full_p, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("$$", content, f"{doc_rel} contains forbidden $$ LaTeX block")

        readme_path = os.path.join(self.repo_root, "README.md")
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_text = f.read()
        self.assertNotIn(
            "$$",
            extract_readme_hackathon_section(readme_text),
            "README.md swarm-1114 section contains forbidden $$ LaTeX block",
        )

    def test_readme_preserves_original_headings(self):
        """BL-8: README.md must preserve all original section headings intact."""
        readme_path = os.path.join(self.repo_root, "README.md")
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_text = f.read()
        for heading in ORIGINAL_README_HEADINGS:
            self.assertRegex(
                readme_text,
                rf"(?m)^{re.escape(heading)}$",
                f"README.md missing preserved original heading: {heading}",
            )

    def test_h6_r2_wave2_claims_and_licence(self):
        """T038/T042: Verify Wave 2 takes, proof counts, licence, and claims-ledger corrections."""
        idea_path = os.path.join(self.repo_root, "swarm/docs/ONE_PAGER_IDEA.md")
        team_path = os.path.join(self.repo_root, "swarm/docs/ONE_PAGER_TEAM.md")
        proof_path = os.path.join(self.repo_root, "swarm/docs/PROOF.md")
        readme_path = os.path.join(self.repo_root, "README.md")

        with open(idea_path, "r", encoding="utf-8") as f:
            idea_text = f.read()
        with open(team_path, "r", encoding="utf-8") as f:
            team_text = f.read()
        with open(proof_path, "r", encoding="utf-8") as f:
            proof_text = f.read()
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_h = extract_readme_hackathon_section(f.read())

        self.assertNotIn("historically", idea_text.lower())
        for text, name in ((idea_text, "ONE_PAGER_IDEA.md"), (readme_h, "README.md (swarm-1114)")):
            self.assertNotIn("0 dropped events", text, f"{name} contains unverified '0 dropped events'")
            for token in (
                "take2_w2_4agents",
                "take3_w2_guards",
                "take1_w1_2agents",
                "JOB_ERROR",
                "CLAIM_REFUSED",
            ):
                self.assertIn(token, text, f"{name} missing required token '{token}'")

        for text, name in ((idea_text, "ONE_PAGER_IDEA.md"), (team_text, "ONE_PAGER_TEAM.md")):
            self.assertIn("PolyForm Strict 1.0.0", text, f"{name} missing PolyForm Strict 1.0.0")
            self.assertIn("AGPL-3.0-only", text, f"{name} missing AGPL-3.0-only")

        jsonl_rows = parse_proof_rows(proof_text)
        track_h_rows = [r for r in jsonl_rows if r[1] == "H"]
        track_p_rows = [r for r in jsonl_rows if r[1] == "P"]
        self.assertEqual(len(track_h_rows), 25, f"Expected 25 Track H logs, found {len(track_h_rows)}")
        self.assertEqual(len(track_p_rows), 19, f"Expected 19 Track P logs, found {len(track_p_rows)}")

        md_reviews = parse_review_md_rows(proof_text)
        self.assertEqual(len(md_reviews), 4, f"Expected 4 Sol 6.1 review rows, found {len(md_reviews)}")

    def test_bl8_proof_uniqueness_and_tracked_set_must_fail(self):
        """BL-8 Must-Fail Control: Duplicate proof rows and missing tracked .jsonl rows MUST fail."""
        proof_path = os.path.join(self.repo_root, "swarm/docs/PROOF.md")
        prov_path = os.path.join(self.repo_root, "PROVENANCE.md")
        with open(proof_path, "r", encoding="utf-8") as f:
            proof_content = f.read()
        with open(prov_path, "r", encoding="utf-8") as f:
            prov_content = f.read()

        # 1. Replace H6_r2_1527.jsonl row with a duplicate of H6_r1_1350.jsonl (count remains 43!)
        h6_r1_line = (
            "| `H6` (`r1`) | `H` | `gemini-4-hydrogen-high` | `swarm/proof/H6_r1_1350.jsonl` | "
            "`68114f654efe97dae675a40d3c5cec8762fa4f121597bd1407c4d326e7ffa922` |"
        )
        h6_r2_line = (
            "| `H6` (`r2`) | `H` | `gemini-4-hydrogen-high` | `swarm/proof/H6_r2_1527.jsonl` | "
            "`de070cd4dcce5dcfb005cd78c4f982fc3ab58088d95eb012d54c4cecaf205a27` |"
        )
        self.assertIn(h6_r1_line, proof_content)
        self.assertIn(h6_r2_line, proof_content)
        mutant_dup = proof_content.replace(h6_r2_line, h6_r1_line)
        dup_errors = validate_proof_index(mutant_dup, prov_content, self.repo_root)
        self.assertTrue(
            any("Duplicate proof path" in e for e in dup_errors),
            f"BL-8 FAILED: duplicate proof path not detected: {dup_errors}",
        )
        self.assertTrue(
            any("Tracked proof log missing from PROOF.md: swarm/proof/H6_r2_1527.jsonl" in e for e in dup_errors),
            f"BL-8 FAILED: missing tracked .jsonl not detected: {dup_errors}",
        )

        # 2. Missing tracked .jsonl row without duplicate
        mutant_missing = proof_content.replace(h6_r2_line + "\n", "")
        missing_errors = validate_proof_index(mutant_missing, prov_content, self.repo_root)
        self.assertTrue(
            any("Tracked proof log missing from PROOF.md: swarm/proof/H6_r2_1527.jsonl" in e for e in missing_errors),
            f"BL-8 FAILED: omitted tracked .jsonl not detected: {missing_errors}",
        )

    def test_h6_r3_review_verdicts_and_checklist_receipt(self):
        """H6_r3: Verify true Sol 6.1 verdicts in TEAM/README/PROOF and real receipt commands in checklist."""
        team_path = os.path.join(self.repo_root, "swarm/docs/ONE_PAGER_TEAM.md")
        proof_path = os.path.join(self.repo_root, "swarm/docs/PROOF.md")
        readme_path = os.path.join(self.repo_root, "README.md")
        checklist_path = os.path.join(self.repo_root, "swarm/docs/OPERATOR_CHECKLIST.md")

        with open(team_path, "r", encoding="utf-8") as f:
            team_text = f.read()
        with open(proof_path, "r", encoding="utf-8") as f:
            proof_text = f.read()
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_h = extract_readme_hackathon_section(f.read())
        with open(checklist_path, "r", encoding="utf-8") as f:
            checklist_text = f.read()

        for text, name in (
            (team_text, "ONE_PAGER_TEAM.md"),
            (proof_text, "PROOF.md"),
            (readme_h, "README.md (swarm-1114)"),
        ):
            self.assertIn("Sol_H5_r1.md", text, f"{name} missing Sol_H5_r1.md")
            self.assertIn("Sol_H5_r2.md", text, f"{name} missing Sol_H5_r2.md")
            self.assertIn("Sol_H6_r2.md", text, f"{name} missing Sol_H6_r2.md")
            self.assertIn("Sol_H6_r3.md", text, f"{name} missing Sol_H6_r3.md")
            self.assertIn("REJECTED", text, f"{name} missing REJECTED verdict for initial Sol reviews")
            self.assertIn("APPROVED", text, f"{name} missing APPROVED verdict for Sol_H5_r2")
            self.assertNotIn("23` Track H", text, f"{name} still states stale 23 Track H count")
            self.assertNotIn("24` Track H", text, f"{name} still states stale 24 Track H count")

        self.assertIn("25` Track H", team_text)
        self.assertIn("25` Track H", readme_h)
        self.assertIn("25` Track H", proof_text)
        self.assertIn("gcloud run services describe", checklist_text)
        self.assertIn('echo "A $LINK $REV" | tee -a ~/vp-deploy/deploy_receipt.txt', checklist_text)
        self.assertIn('echo "B $VURL $(git log --oneline -1)" | tee -a ~/vp-deploy/deploy_receipt.txt', checklist_text)
        self.assertNotIn("https://viewpoint-gateway-xxxx.a.run.app", checklist_text)
        self.assertNotIn("https://viewpoint-swarm-viewer-xxxx.a.run.app", checklist_text)


if __name__ == "__main__":
    unittest.main()
