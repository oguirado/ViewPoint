#!/usr/bin/env python3
# @anchor:tools.specs.check_citations
"""Stdlib-only citation checker for Markdown spec documents in docs/."""

import argparse
import glob
import json
import os
import re
import subprocess
import sys


CITATION_RE = re.compile(
    r'(?<![a-zA-Z0-9_/\-.:])([a-zA-Z0-9_\-./]+):(\d+)(?:-(\d+))?(?![0-9a-zA-Z_])'
)
BACKTICK_RE = re.compile(r'`([^`]+)`')


def load_file_lines(path, ref=None):
    """Load lines from path either from working tree or git ref."""
    if ref is None:
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()
        except Exception:
            return None
    else:
        try:
            proc = subprocess.run(
                ["git", "show", f"{ref}:{path}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode != 0:
                return None
            return proc.stdout.splitlines()
        except Exception:
            return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Check spec document citations against file contents."
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Markdown spec files to check (defaults to docs/*.md and docs/spec/*.md).",
    )
    parser.add_argument(
        "--ref",
        default=None,
        help="Git ref to read target files from via git show REF:path.",
    )
    return parser.parse_args(argv)


def check_spec_citations(files=None, ref=None):
    if not files:
        matched = glob.glob("docs/*.md") + glob.glob("docs/spec/*.md")
        files = sorted(set(matched))
    else:
        files = sorted(set(files))

    target_cache = {}
    problems = []
    docs_count = 0
    citations_count = 0
    errors_count = 0
    warnings_count = 0

    for doc in files:
        if not os.path.isfile(doc):
            problems.append({
                "doc": doc,
                "line": 0,
                "citation": "",
                "problem": f"missing spec document: {doc}",
            })
            errors_count += 1
            continue

        docs_count += 1
        try:
            with open(doc, "r", encoding="utf-8", errors="replace") as f:
                doc_lines = f.read().splitlines()
        except Exception as e:
            problems.append({
                "doc": doc,
                "line": 0,
                "citation": "",
                "problem": f"cannot read spec document: {e}",
            })
            errors_count += 1
            continue

        for line_num, line_text in enumerate(doc_lines, 1):
            matches = CITATION_RE.findall(line_text)
            for raw_path, start_str, end_str in matches:
                raw_path = raw_path.rstrip(".,;")
                if "://" in raw_path or raw_path.startswith(("http:", "https:", "ftp:", "file:")):
                    continue
                if not re.search(r"[a-zA-Z./]", raw_path):
                    continue

                start_line = int(start_str)
                end_line = int(end_str) if end_str is not None and end_str != "" else start_line
                citation_str = (
                    f"{raw_path}:{start_line}"
                    if end_str is None or end_str == ""
                    else f"{raw_path}:{start_line}-{end_line}"
                )

                citations_count += 1

                if raw_path not in target_cache:
                    target_cache[raw_path] = load_file_lines(raw_path, ref=ref)
                target_lines = target_cache[raw_path]

                if target_lines is None:
                    problems.append({
                        "doc": doc,
                        "line": line_num,
                        "citation": citation_str,
                        "problem": f"missing file: {raw_path}",
                    })
                    errors_count += 1
                elif start_line < 1 or end_line < 1 or start_line > end_line:
                    problems.append({
                        "doc": doc,
                        "line": line_num,
                        "citation": citation_str,
                        "problem": f"reversed or invalid range: {start_line}-{end_line}",
                    })
                    errors_count += 1
                elif start_line > len(target_lines) or end_line > len(target_lines):
                    problems.append({
                        "doc": doc,
                        "line": line_num,
                        "citation": citation_str,
                        "problem": f"line range out of bounds ({start_line}-{end_line} exceeds {len(target_lines)} lines)",
                    })
                    errors_count += 1
                else:
                    # Range valid and file exists. Check backtick identifier warnings.
                    backtick_matches = BACKTICK_RE.findall(line_text)
                    candidate_idents = set()
                    for bt in backtick_matches:
                        bt_clean = bt.strip()
                        if not bt_clean:
                            continue
                        if bt_clean == citation_str or bt_clean == raw_path:
                            continue
                        if raw_path in bt_clean and (":" in bt_clean):
                            continue
                        candidate_idents.add(bt_clean)

                    cited_text = "\n".join(target_lines[start_line - 1 : end_line])
                    for ident in sorted(candidate_idents):
                        if ident not in cited_text:
                            problems.append({
                                "doc": doc,
                                "line": line_num,
                                "citation": citation_str,
                                "problem": f"warning: identifier '{ident}' not found in cited lines",
                            })
                            warnings_count += 1

    problems.sort(key=lambda x: (x["doc"], x["line"], x["citation"], x["problem"]))

    result = {
        "counts": {
            "citations": citations_count,
            "docs": docs_count,
            "errors": errors_count,
            "warnings": warnings_count,
        },
        "problems": problems,
    }
    return result


def main(argv=None):
    try:
        args = parse_args(argv)
    except SystemExit as e:
        sys.exit(e.code)

    res = check_spec_citations(files=args.files, ref=args.ref)
    print(json.dumps(res, indent=2, sort_keys=True))
    if res["counts"]["errors"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
