# @anchor:tests.test_check_spec_citations
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tools.check_spec_citations import check_spec_citations, main


class TestCheckSpecCitations(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.temp_dir)

    def test_valid_single_line(self):
        with open("code.py", "w") as f:
            f.write("line 1\nline 2\nline 3\n")
        with open("doc.md", "w") as f:
            f.write("See code.py:2 for details.\n")

        res = check_spec_citations(["doc.md"])
        self.assertEqual(res["counts"]["errors"], 0)
        self.assertEqual(res["counts"]["warnings"], 0)
        self.assertEqual(res["counts"]["citations"], 1)
        self.assertEqual(res["counts"]["docs"], 1)
        self.assertEqual(res["problems"], [])

    def test_valid_range(self):
        with open("code.py", "w") as f:
            f.write("line 1\nline 2\nline 3\n")
        with open("doc.md", "w") as f:
            f.write("See code.py:1-3 for details.\n")

        res = check_spec_citations(["doc.md"])
        self.assertEqual(res["counts"]["errors"], 0)
        self.assertEqual(res["counts"]["warnings"], 0)
        self.assertEqual(res["counts"]["citations"], 1)

    def test_missing_file(self):
        with open("doc.md", "w") as f:
            f.write("See missing.py:1 for details.\n")

        res = check_spec_citations(["doc.md"])
        self.assertEqual(res["counts"]["errors"], 1)
        self.assertEqual(len(res["problems"]), 1)
        self.assertIn("missing file", res["problems"][0]["problem"])

    def test_line_beyond_end(self):
        with open("code.py", "w") as f:
            f.write("line 1\nline 2\n")
        with open("doc.md", "w") as f:
            f.write("See code.py:5 for details.\n")

        res = check_spec_citations(["doc.md"])
        self.assertEqual(res["counts"]["errors"], 1)
        self.assertEqual(len(res["problems"]), 1)
        self.assertIn("out of bounds", res["problems"][0]["problem"])

    def test_reversed_range(self):
        with open("code.py", "w") as f:
            f.write("line 1\nline 2\nline 3\nline 4\nline 5\n")
        with open("doc.md", "w") as f:
            f.write("See code.py:4-2 for details.\n")

        res = check_spec_citations(["doc.md"])
        self.assertEqual(res["counts"]["errors"], 1)
        self.assertEqual(len(res["problems"]), 1)
        self.assertIn("reversed or invalid range", res["problems"][0]["problem"])

    def test_identifier_warning(self):
        with open("code.py", "w") as f:
            f.write("def foo():\n    return 42\n")
        with open("doc.md", "w") as f:
            f.write("Check `bar` in code.py:1-2.\n")

        res = check_spec_citations(["doc.md"])
        self.assertEqual(res["counts"]["errors"], 0)
        self.assertEqual(res["counts"]["warnings"], 1)
        self.assertEqual(len(res["problems"]), 1)
        self.assertIn("warning: identifier 'bar' not found", res["problems"][0]["problem"])

    def test_ref_mode_temp_git_repo(self):
        subprocess.run(["git", "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)

        with open("code.py", "w") as f:
            f.write("def foo():\n    pass\n")
        subprocess.run(["git", "add", "code.py"], check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        with open("code.py", "w") as f:
            f.write("def foo():\n    # new comment\n    pass\n    # extra line\n")

        with open("doc.md", "w") as f:
            f.write("See code.py:1-2 for details.\n")

        res_ref = check_spec_citations(["doc.md"], ref="HEAD")
        self.assertEqual(res_ref["counts"]["errors"], 0)
        self.assertEqual(res_ref["counts"]["citations"], 1)

        with open("doc.md", "w") as f:
            f.write("See code.py:4 for details.\n")

        res_ref_err = check_spec_citations(["doc.md"], ref="HEAD")
        self.assertEqual(res_ref_err["counts"]["errors"], 1)


if __name__ == "__main__":
    unittest.main()
