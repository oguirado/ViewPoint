"""PR1 regression controls for engine wheel headers and archive validation."""
import base64
import csv
import hashlib
import importlib.util
import io
from pathlib import Path
import unittest
from zipfile import ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pr1_wheel_builder", ROOT / "tools/build_engine_wheel.py"
)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

VERSION = "1.2.3"
PROJECT = {"name": "vision-engine", "version": VERSION}
DIST_INFO = f"vision_engine-{VERSION}.dist-info/"
PACKAGE = "visioncore/__init__.py"
PACKAGE_DATA = b"# reviewed fixture\n"
OTHER_PACKAGE_DATA = b"# different fixture\n"
WHEEL_HEADERS = (
    b"Wheel-Version: 1.0\n"
    b"Root-Is-Purelib: true\n"
    b"Tag: py3-none-any\n"
)
VERSION_HEADER = b"Version: 1.2.3\n"
METADATA_HEADERS = b"Metadata-Version: 2.1\nName: vision-engine\n" + VERSION_HEADER
METADATA = METADATA_HEADERS + b"\nFixture README body.\n"


class FixtureSource:
    files = {PACKAGE: PACKAGE_DATA}

    def read(self, name):
        return self.files[name]


def make_wheel(
    *, wheel=WHEEL_HEADERS, metadata=METADATA, package_name=PACKAGE,
    package_data=PACKAGE_DATA, wrong_record_hash=False, extra_directory=False,
):
    record_name = DIST_INFO + "RECORD"
    members = {
        package_name: package_data,
        DIST_INFO + "WHEEL": wheel,
        DIST_INFO + "METADATA": metadata,
    }
    rows = io.StringIO(newline="")
    writer = csv.writer(rows, lineterminator="\n")
    for name, data in members.items():
        hashed = OTHER_PACKAGE_DATA if wrong_record_hash and name == package_name else data
        digest = base64.urlsafe_b64encode(hashlib.sha256(hashed).digest())
        writer.writerow((name, "sha256=" + digest.rstrip(b"=").decode("ascii"), len(data)))
    writer.writerow((record_name, "", ""))
    members[record_name] = rows.getvalue().encode("utf-8")
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for name, data in members.items():
            archive.writestr(ZipInfo(name), data)
        if extra_directory:
            archive.writestr(ZipInfo("unlisted/"), b"")
    return output.getvalue()


class WheelHeaders(unittest.TestCase):
    def failures(self, *, pinned_version=VERSION, **entries):
        return builder.wheel_failures(make_wheel(**entries), FixtureSource(), PROJECT, pinned_version)

    def test_legacy_flat_wheel_and_matching_metadata_with_readme_body(self):
        self.assertEqual(self.failures(), [])

    def test_utf8_generator(self):
        self.assertEqual(self.failures(wheel=WHEEL_HEADERS + "Generator: générateur\n".encode("utf-8")), [])

    def test_each_required_wheel_header_is_checked(self):
        for good, wrong in (
            (b"Wheel-Version: 1.0\n", b"Wheel-Version: 2.0\n"),
            (b"Root-Is-Purelib: true\n", b"Root-Is-Purelib: false\n"),
            (b"Tag: py3-none-any\n", b"Tag: cp311-none-any\n"),
        ):
            for replacement in (b"", wrong, good + good):
                with self.subTest(header=good, replacement=replacement):
                    wheel = WHEEL_HEADERS.replace(good, replacement)
                    self.assertEqual(self.failures(wheel=wheel), ["wheel_wheel_metadata"])

    def test_rejects_invalid_wheel_text(self):
        for suffix in (
            b"Generator: \xff\n",
            b"\nUnexpected WHEEL body.\n",
            b"Generator: \n",
            b"Generator: fixture\nGenerator: other fixture\n",
            b"Build: 1\n",
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.failures(wheel=WHEEL_HEADERS + suffix), ["wheel_wheel_metadata"])

    def test_rejects_missing_wrong_or_conflicting_metadata_version(self):
        wrong = b"Version: 9.9.9\n"
        for replacement in (b"", wrong, VERSION_HEADER + wrong):
            with self.subTest(replacement=replacement):
                metadata = METADATA.replace(VERSION_HEADER, replacement)
                self.assertEqual(self.failures(metadata=metadata), ["wheel_metadata_mismatch"])

    def test_rejects_mismatched_metadata_name_and_pin(self):
        with self.subTest(field="Name"):
            metadata = METADATA.replace(b"Name: vision-engine\n", b"Name: other-project\n")
            self.assertEqual(self.failures(metadata=metadata), ["wheel_metadata_mismatch"])
        with self.subTest(field="pin"):
            self.assertEqual(self.failures(pinned_version="9.9.9"), ["wheel_metadata_mismatch"])

    def test_rejects_invalid_or_nonflat_metadata(self):
        multipart = METADATA_HEADERS + (
            b'Content-Type: multipart/mixed; boundary="fixture"\n\n'
            b"--fixture\nContent-Type: text/plain\n\nFixture README body.\n"
            b"--fixture--\n"
        )
        for metadata in (
            METADATA + b"\xff\n",
            METADATA_HEADERS + b"Missing header-body separator.\n",
            multipart,
        ):
            with self.subTest(metadata=metadata):
                self.assertEqual(self.failures(metadata=metadata), ["wheel_metadata_mismatch"])

    def test_rejects_unexpected_archive_member(self):
        self.assertEqual(self.failures(extra_directory=True), ["wheel_archive_inventory"])

    def test_rejects_incorrect_record_hash(self):
        self.assertEqual(self.failures(wrong_record_hash=True), ["wheel_record"])

    def test_rejects_unexpected_package_member(self):
        failures = self.failures(package_name="visioncore/unexpected.py")
        self.assertEqual(failures, ["wheel_package_inventory"])

    def test_rejects_changed_package_bytes_even_with_valid_record(self):
        failures = self.failures(package_data=OTHER_PACKAGE_DATA)
        self.assertEqual(failures, [f"{PACKAGE}:wheel_source_mismatch"])
