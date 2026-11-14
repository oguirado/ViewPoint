import ast, importlib.util, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WL = os.path.dirname(HERE)
QT = os.path.join(WL, "qtile")
PROFILES = os.path.join(os.path.dirname(os.path.dirname(WL)), "docker", "cu", "profiles")
sys.path.insert(0, WL)
import recorder as R

_spec = importlib.util.spec_from_file_location("wl_qtile", os.path.join(QT, "__init__.py"))
Q = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(Q)

RES = ((1920, 1080, "layout_1080p.json"), (3840, 2160, "layout_4k.json"))


def _rects(lay):
    return [lay["master"], lay["stack"]]


class QtileProfile(unittest.TestCase):
    GOOD = {"schema": "os.cu.qtile-image-attestation/v1", "image": "opensuperficies-desktop-arm64:1.0.0",
            "digest": "sha256:" + "ab" * 32, "qtile_version": "0.33.0", "approval_id": "OPR-0001"}

    def _gate(self, content):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "att")
            with open(p, "w") as f:
                f.write(content if isinstance(content, str) else json.dumps(content))
            return Q.profile_gate("qtile", p)

    def test_qtile_attestation_rejects_blank_strings(self):
        for k in ("image", "qtile_version", "approval_id"):
            for blank in ("   ", "\t\n"):
                self.assertEqual(self._gate(dict(self.GOOD, **{k: blank})), (5, "image_layer_missing"), k)
        self.assertEqual(self._gate(self.GOOD), (0, "ok"))

    def test_qtile_attestation_rejects_non_json_and_bad_schema(self):
        bad = (json.dumps(self.GOOD) + "x", "ok", "[]", "null", "127.0.0.1 localhost\n")
        for c in bad:
            self.assertEqual(self._gate(c), (5, "image_layer_missing"), c)
        for k in self.GOOD:
            d = dict(self.GOOD)
            del d[k]
            self.assertEqual(self._gate(d), (5, "image_layer_missing"), "missing " + k)
            d = dict(self.GOOD)
            d[k] = ""
            self.assertEqual(self._gate(d), (5, "image_layer_missing"), "empty " + k)
            d = dict(self.GOOD)
            d[k] = 7
            self.assertEqual(self._gate(d), (5, "image_layer_missing"), "type " + k)
        for k, v in (("schema", "other/v1"), ("digest", "sha256:zz"), ("digest", "sha256:" + "a" * 63),
                     ("digest", "md5:" + "a" * 32)):
            d = dict(self.GOOD)
            d[k] = v
            self.assertEqual(self._gate(d), (5, "image_layer_missing"), k)
        d = dict(self.GOOD)
        d["extra"] = 1
        self.assertEqual(self._gate(d), (5, "image_layer_missing"), "closed schema")
        self.assertEqual(self._gate(self.GOOD), (0, "ok"))

    def test_qtile_attestation_directory_or_unreadable_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Q.profile_gate("qtile", d), (5, "image_layer_missing"))
            self.assertEqual(Q.profile_gate("qtile", ""), (5, "image_layer_missing"))

    def _file(self, name):
        with open(os.path.join(QT, name)) as f:
            return json.load(f)

    def test_qtile_layout_rects_integer_nonoverlap(self):
        for w, h, name in RES:
            lay = self._file(name)
            self.assertEqual(lay["schema"], "os.cu.qtile-layout/v1")
            a, b = _rects(lay)
            for r in (a, b, lay["rect"]):
                self.assertEqual(len(r), 4)
                self.assertTrue(all(type(v) is int for v in r))
            self.assertEqual(lay["rect"], lay["master"])
            self.assertLessEqual(a[0] + a[2], b[0])  # disjoint on x
            self.assertEqual(Q.load_layout(w, h), lay)

    def test_qtile_layout_covers_screen_1080p_4k(self):
        for w, h, name in RES:
            lay = self._file(name)
            self.assertEqual((lay["width"], lay["height"]), (w, h))
            a, b = _rects(lay)
            self.assertEqual(a, [0, 0, w // 2, h])
            self.assertEqual(b, [w // 2, 0, w - w // 2, h])
            self.assertEqual(a[2] * a[3] + b[2] * b[3], w * h)
            self.assertEqual(a[0] + a[2], b[0])
            self.assertEqual(b[0] + b[2], w)

    def test_qtile_config_data_only_no_exec(self):
        with open(os.path.join(QT, "config.py")) as f:
            tree = ast.parse(f.read())
        for n in tree.body:
            self.assertIsInstance(n, (ast.Import, ast.ImportFrom, ast.Assign), type(n).__name__)
            if isinstance(n, ast.Import):
                self.assertEqual([a.name for a in n.names], [])
            if isinstance(n, ast.ImportFrom):
                self.assertTrue(n.module.startswith("libqtile"))
        banned = {"exec", "eval", "open", "__import__", "compile"}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                f = n.func
                self.assertFalse(isinstance(f, ast.Name) and f.id in banned)
                self.assertFalse(isinstance(f, ast.Attribute) and f.attr in ("system", "Popen", "run", "spawn"))
        with open(os.path.join(PROFILES, "qtile.env")) as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        self.assertIn("WM=qtile", lines)
        for l in lines:
            k, _, v = l.partition("=")
            self.assertTrue(k.isidentifier() and k.isupper(), l)
            self.assertNotIn("$(", v)
            self.assertNotIn("`", v)

    def test_qtile_profile_refused_without_image_attestation(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "image-has-qtile")
            self.assertEqual(Q.profile_gate("qtile", missing), (5, "image_layer_missing"))
            open(missing, "w").close()
            self.assertEqual(Q.profile_gate("qtile", missing), (5, "image_layer_missing"))
            with open(missing, "w") as f:
                json.dump(self.GOOD, f)
            self.assertEqual(Q.profile_gate("qtile", missing), (0, "ok"))
            self.assertEqual(Q.profile_gate("default", missing + "x"), (0, "ok"))
            self.assertEqual(Q.profile_gate("kde", missing), (2, "E_SCHEMA"))

    def test_labels_qtile_uses_layout_rects(self):
        with open(os.path.join(WL, "prereg_wl.json")) as f:
            m = json.load(f)
        for w, h, name in RES:
            lay = self._file(name)
            rows = R.labels_for(m, "WL-6", w, h, "qtile", lay)
            self.assertTrue(rows)
            for r in rows:
                self.assertEqual(r["rect"], lay["rect"])
                self.assertEqual(r["expected_kind"], "NOT_CHANGED")


if __name__ == "__main__":
    unittest.main()
