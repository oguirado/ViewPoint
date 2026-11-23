from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parents[1]))
import imagepack
from imagepack import ImagePackCatalogue, ImagePackError


class ImagePackTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory(dir=TEST_DIR))
        self.directory = Path(temporary)
        self.enterContext(patch.object(imagepack.platform, "machine", return_value="aarch64"))
        self.artifacts = {
            "kernel": b"\0" * 56 + b"ARM\x64" + b"\0" * 4,
            "initrd": b"synthetic initrd",
            "root": b"synthetic root",
        }
        fixture = TEST_DIR.parents[2] / "schemas/tests/examples/vp.image_pack_v1.valid.json"
        self.pack = json.loads(fixture.read_text(encoding="utf-8"))
        resolved = self.pack["resolved"]
        resolved.update(cpu=4, ram_bytes=6144 * (1 << 20), devices=["virtio-blk", "virtio-serial"])
        for name, content in self.artifacts.items():
            path = self.directory / name
            path.write_bytes(content)
            resolved[name] = {"path": str(path), "sha256": hashlib.sha256(content).hexdigest()}
        self.pack["requested"] = deepcopy(resolved)
        self.catalogue = ImagePackCatalogue(self.pack)

    def _change_config(self, field, value):
        pack = deepcopy(self.pack)
        for key in ("requested", "resolved"):
            pack[key][field] = deepcopy(value)
        return pack

    def test_closed_notepad_vm_lists_and_validates_with_frozen_contracts(self):
        self.assertEqual(self.catalogue.handle({"op": "pack_list"}), {"packs": [self.pack]})
        request = {"op": "pack_validate", "pack": self.pack}
        self.assertEqual(self.catalogue.handle(request), self.pack)
        for other in ("notepad-vm-clone", "microvm"):
            with self.subTest(profile=other):
                candidate = dict(self.pack, profile_id=other)
                with self.assertRaises(ImagePackError):
                    self.catalogue.handle(dict(request, pack=candidate))

    def _pack_without_initrd(self):
        pack = deepcopy(self.pack)
        del pack["requested"]["initrd"]
        del pack["resolved"]["initrd"]
        return pack

    def _assert_kernel_and_root_pack(self, pack):
        catalogue = ImagePackCatalogue(pack)
        with patch.object(imagepack, "_verify_artifact", wraps=imagepack._verify_artifact) as verify:
            self.assertEqual(catalogue.handle({"op": "pack_list"}), {"packs": [pack]})
            self.assertEqual(catalogue.handle({"op": "pack_validate", "pack": pack}), pack)
        self.assertEqual([call.args[0] for call in verify.call_args_list],
                         ["kernel", "root", "kernel", "root"])

    def test_kernel_and_root_pack_lists_and_validates_without_initrd(self):
        (self.directory / "initrd").unlink()
        self._assert_kernel_and_root_pack(self._pack_without_initrd())

    def test_candidate_cannot_drop_a_trusted_initrd_pin(self):
        with self.assertRaisesRegex(ImagePackError, "differs from the operator-pinned"):
            self.catalogue.pack_validate(self._pack_without_initrd())

    def test_pin_present_on_only_one_side_is_refused(self):
        for side in ("requested", "resolved"):
            with self.subTest(side=side):
                pack = self._pack_without_initrd()
                pack[side]["initrd"] = self.pack[side]["initrd"]
                with patch.object(imagepack, "_open_artifact") as opener:
                    with self.assertRaisesRegex(ImagePackError, r"vp\.image_pack/v1 refused at record"):
                        ImagePackCatalogue(pack)
                    opener.assert_not_called()

    def test_missing_pinned_initrd_is_refused(self):
        (self.directory / "initrd").unlink()
        with self.assertRaisesRegex(ImagePackError, "initrd: AUSENTE"):
            self.catalogue.pack_validate(self.pack)

    def test_mutant_unconditional_initrd_lookup_breaks_kernel_and_root_oracle(self):
        def require_initrd(catalogue):
            for name in ("kernel", "initrd", "root"):
                imagepack._verify_artifact(name, catalogue._trusted_pack["resolved"][name])

        pack = self._pack_without_initrd()
        self._assert_kernel_and_root_pack(pack)
        with patch.object(ImagePackCatalogue, "_verify_trusted_pack", require_initrd):
            with self.assertRaisesRegex(KeyError, "initrd"):
                self._assert_kernel_and_root_pack(pack)

    def test_catalogue_copies_configuration_and_results(self):
        expected = deepcopy(self.pack)
        self.pack["resolved"]["kernel"]["path"] = "/untrusted"
        self.catalogue.pack_list()["packs"][0]["resolved"]["devices"].clear()
        self.catalogue.pack_validate(expected)["requested"]["cpu"] = 1
        self.assertEqual(self.catalogue.pack_list(), {"packs": [expected]})

    def test_missing_configuration_and_unpinned_artifact_refused(self):
        empty = ImagePackCatalogue()
        self.assertEqual(empty.pack_list(), {"packs": []})
        with self.assertRaisesRegex(ImagePackError, "operator-pinned"):
            empty.pack_validate(self.pack)
        pinless = self._change_config("initrd", {"path": str(self.directory / "initrd")})
        with self.assertRaises(ImagePackError):
            ImagePackCatalogue(pinless)

    def _unpinned_candidate(self):
        other = self.directory / "other"
        other.write_bytes(b"other root")
        pin = {"path": str(other), "sha256": hashlib.sha256(b"other root").hexdigest()}
        return self._change_config("root", pin)

    def assert_unpinned_candidate_refused(self, validate, candidate):
        with self.assertRaisesRegex(ImagePackError, "differs from the operator-pinned"):
            validate(candidate)

    def test_valid_hash_on_an_unpinned_candidate_is_refused_before_read(self):
        candidate = self._unpinned_candidate()
        with patch.object(imagepack, "_open_artifact") as opener:
            self.assert_unpinned_candidate_refused(self.catalogue.pack_validate, candidate)
            opener.assert_not_called()

    def test_missing_artifact_blocks_catalogue_and_validation(self):
        (self.directory / "kernel").unlink()
        with self.assertRaisesRegex(ImagePackError, "kernel: AUSENTE"):
            self.catalogue.pack_list()
        with self.assertRaisesRegex(ImagePackError, "kernel: AUSENTE"):
            self.catalogue.pack_validate(self.pack)

    def assert_modified_artifact_refused(self, name):
        with self.assertRaisesRegex(ImagePackError, f"{name}: sha256 mismatch"):
            self.catalogue.pack_validate(self.pack)

    def test_each_artifact_hash_is_checked_again_after_validation(self):
        for name, content in self.artifacts.items():
            with self.subTest(artifact=name):
                self.catalogue.pack_validate(self.pack)
                path = self.directory / name
                path.write_bytes(b"x" + content[1:])
                self.assert_modified_artifact_refused(name)
                path.write_bytes(content)

    def test_host_isa_and_raw_kernel_architecture_are_refused(self):
        with patch.object(imagepack.platform, "machine", return_value="x86_64"):
            with self.assertRaisesRegex(ImagePackError, "ARM64 host required"):
                self.catalogue.pack_validate(self.pack)

        kernel = self.directory / "kernel"
        wrong_arch = b"\0" * 64
        kernel.write_bytes(wrong_arch)
        pinned = self._change_config("kernel", {
            "path": str(kernel), "sha256": hashlib.sha256(wrong_arch).hexdigest(),
        })
        with self.assertRaisesRegex(ImagePackError, "raw ARM64 Linux Image required"):
            ImagePackCatalogue(pinned).pack_validate(pinned)

    def test_wrong_arch_backend_modes_devices_and_resources_refused(self):
        for field, value in {
            "isa": "x86_64", "vmm": "firecracker", "capture": "host-xwd",
            "input": "host-atspi", "snapshot": "native", "acceleration": "tcg",
            "devices": ["virtio-blk", "virtio-serial", "virtio-net"],
            "cpu": 6, "ram_bytes": (512 << 20) + 1,
        }.items():
            with self.subTest(field=field):
                with self.assertRaises(ImagePackError):
                    ImagePackCatalogue(self._change_config(field, value))

    def test_requested_and_resolved_cannot_disagree(self):
        altered = deepcopy(self.pack)
        altered["resolved"]["cpu"] = 2
        with self.assertRaises(ImagePackError):
            self.catalogue.pack_validate(altered)

    def test_noncanonical_and_symlink_artifact_paths_refused(self):
        root = self.directory / "root"
        child_link = self.directory / "alias"
        child_link.symlink_to(root)
        directory_link = self.directory / "alias-directory"
        directory_link.symlink_to(self.directory, target_is_directory=True)
        for path in (str(child_link), str(directory_link / "root"), f"{self.directory}/./root"):
            with self.subTest(path=Path(path).name):
                pin = dict(self.pack["resolved"]["root"], path=path)
                altered = self._change_config("root", pin)
                with self.assertRaises(ImagePackError):
                    ImagePackCatalogue(altered).pack_validate(altered)

    def test_general_import_is_refused(self):
        with patch.object(imagepack, "_open_artifact") as opener:
            with self.assertRaisesRegex(ImagePackError, "pack_import is unsupported"):
                self.catalogue.handle({"op": "pack_import", "pack": self.pack})
            opener.assert_not_called()

    def assert_capability_has_no_creation_authority(self, handle):
        with self.assertRaisesRegex(ImagePackError, "outside image-pack authority"):
            handle({"op": "desktop_create", "profile_id": "notepad-vm"})

    def test_announced_capability_does_not_grant_creation(self):
        with patch.object(imagepack.os, "environ", {
            "MV_ARM64_CAPSULE_STATUS": '{"vm":{"available":true,"reason":""}}',
        }), patch.object(imagepack, "_open_artifact") as opener:
            self.assert_capability_has_no_creation_authority(self.catalogue.handle)
            opener.assert_not_called()

    def test_skip_pin_verification_mutant_is_killed(self):
        (self.directory / "root").write_bytes(b"tampered")
        self.assert_modified_artifact_refused("root")
        with patch.object(imagepack, "_verify_artifact", return_value=None):
            with self.assertRaisesRegex(AssertionError, "ImagePackError not raised"):
                self.assert_modified_artifact_refused("root")

    def test_accept_unpinned_pack_mutant_is_killed(self):
        candidate = self._unpinned_candidate()

        def mutated_validate(pack):
            return ImagePackCatalogue(pack).pack_validate(pack)

        self.assert_unpinned_candidate_refused(self.catalogue.pack_validate, candidate)
        with self.assertRaisesRegex(AssertionError, "ImagePackError not raised"):
            self.assert_unpinned_candidate_refused(mutated_validate, candidate)

    def test_announce_as_creation_rights_mutant_is_killed(self):
        real_handle = self.catalogue.handle

        def mutated_handle(args):
            if args["op"] == "desktop_create" and imagepack.os.environ.get("MV_ARM64_CAPSULE_STATUS"):
                return {"created": True}
            return real_handle(args)

        with patch.object(imagepack.os, "environ", {
            "MV_ARM64_CAPSULE_STATUS": '{"vm":{"available":true,"reason":""}}',
        }):
            self.assert_capability_has_no_creation_authority(real_handle)
            with self.assertRaisesRegex(AssertionError, "ImagePackError not raised"):
                self.assert_capability_has_no_creation_authority(mutated_handle)
