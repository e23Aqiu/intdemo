import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrated_client.online.uos_delta import (
    UosDeltaCache,
    UosDeltaCancelled,
    UosDeltaError,
)


class FakeXdeltaProcess:
    output = b""

    def __init__(self, arguments, **_kwargs):
        self.arguments = arguments
        Path(arguments[-1]).write_bytes(self.output)
        self.returncode = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


class RunningFakeXdeltaProcess(FakeXdeltaProcess):
    def __init__(self, arguments, **kwargs):
        super().__init__(arguments, **kwargs)
        self.returncode = None


class UosDeltaCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_pending_deb_is_promoted_only_after_target_version_starts(self):
        target = b"complete-uos-deb"
        digest = hashlib.sha256(target).hexdigest()
        cache = UosDeltaCache(self.root, current_version="1.2.1")
        pending = cache.pending_path(
            name="IntDemo-UOS-arm64-1.2.1.deb",
            version="1.2.1",
        )
        pending.write_bytes(target)
        cache.register_pending(
            pending,
            version="1.2.1",
            size=len(target),
            sha256=digest,
        )

        old_runtime = UosDeltaCache(self.root, current_version="1.2.0")
        self.assertFalse(old_runtime.confirm_pending())
        self.assertTrue(pending.is_file())

        self.assertTrue(cache.confirm_pending())
        base = (
            self.root
            / "updates"
            / "base"
            / "IntDemo-UOS-arm64-1.2.1.deb"
        )
        self.assertEqual(base.read_bytes(), target)
        metadata = json.loads(
            (base.parent / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["version"], "1.2.1")
        self.assertEqual(metadata["sha256"], digest)
        self.assertFalse(
            (self.root / "updates" / "pending" / "pending-install.json").exists()
        )

    def test_register_pending_keeps_only_the_latest_verified_deb(self):
        cache = UosDeltaCache(self.root, current_version="1.2.1")
        first = cache.pending_path(
            name="IntDemo-UOS-arm64-1.2.1.deb",
            version="1.2.1",
        )
        first.write_bytes(b"first")
        cache.register_pending(
            first,
            version="1.2.1",
            size=5,
            sha256=hashlib.sha256(b"first").hexdigest(),
        )
        second = cache.pending_path(
            name="IntDemo-UOS-arm64-1.2.2.deb",
            version="1.2.2",
        )
        second.write_bytes(b"second")
        cache.register_pending(
            second,
            version="1.2.2",
            size=6,
            sha256=hashlib.sha256(b"second").hexdigest(),
        )

        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_legacy_full_download_is_adopted_only_after_manifest_hash_matches(self):
        released = b"released-1.2.1-deb"
        cache = UosDeltaCache(self.root, current_version="1.2.1")
        legacy = self.root / "updates" / "IntDemo-UOS-arm64-1.2.1.deb"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(released)

        self.assertIsNone(
            cache.locate_base(
                version="1.2.1",
                size=len(released),
                sha256="0" * 64,
            )
        )
        adopted = cache.locate_base(
            version="1.2.1",
            size=len(released),
            sha256=hashlib.sha256(released).hexdigest(),
        )
        self.assertIsNotNone(adopted)
        self.assertEqual(adopted.read_bytes(), released)
        self.assertFalse(legacy.exists())

    def test_reconstruct_verifies_output_and_records_pending_install(self):
        base = self.root / "base.deb"
        patch_file = self.root / "update.intdelta"
        target = b"byte-identical-target-deb"
        base.write_bytes(b"old-deb")
        patch_file.write_bytes(b"patch")
        FakeXdeltaProcess.output = target
        cache = UosDeltaCache(
            self.root,
            current_version="1.2.0",
            engine_path=sys.executable,
        )
        states = []

        with patch(
            "integrated_client.online.uos_delta.subprocess.Popen",
            FakeXdeltaProcess,
        ):
            result = cache.reconstruct(
                base_path=base,
                patch_path=patch_file,
                target_name="IntDemo-UOS-arm64-1.2.1.deb",
                target_version="1.2.1",
                target_size=len(target),
                target_sha256=hashlib.sha256(target).hexdigest(),
                state_callback=lambda state, message: states.append(
                    (state, message)
                ),
            )

        self.assertEqual(result.read_bytes(), target)
        self.assertFalse(patch_file.exists())
        self.assertTrue(cache.pending_metadata_path.is_file())
        self.assertEqual(
            [state for state, _message in states],
            ["reconstructing", "verifying_target"],
        )

    def test_reconstruct_discards_output_with_wrong_target_hash(self):
        base = self.root / "base.deb"
        patch_file = self.root / "update.intdelta"
        base.write_bytes(b"old-deb")
        patch_file.write_bytes(b"patch")
        FakeXdeltaProcess.output = b"tampered-target"
        cache = UosDeltaCache(
            self.root,
            current_version="1.2.0",
            engine_path=sys.executable,
        )

        with patch(
            "integrated_client.online.uos_delta.subprocess.Popen",
            FakeXdeltaProcess,
        ), self.assertRaisesRegex(UosDeltaError, "SHA-256"):
            cache.reconstruct(
                base_path=base,
                patch_path=patch_file,
                target_name="IntDemo-UOS-arm64-1.2.1.deb",
                target_version="1.2.1",
                target_size=len(FakeXdeltaProcess.output),
                target_sha256="0" * 64,
            )

        self.assertFalse(
            (
                self.root
                / "updates"
                / "pending"
                / "IntDemo-UOS-arm64-1.2.1.deb.part"
            ).exists()
        )
        self.assertFalse(cache.pending_metadata_path.exists())

    def test_reconstruct_cancellation_stops_engine_and_cleans_partial_output(self):
        base = self.root / "base.deb"
        patch_file = self.root / "update.intdelta"
        base.write_bytes(b"old-deb")
        patch_file.write_bytes(b"patch")
        RunningFakeXdeltaProcess.output = b"partial-target"
        cache = UosDeltaCache(
            self.root,
            current_version="1.2.0",
            engine_path=sys.executable,
        )
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls > 1

        with patch(
            "integrated_client.online.uos_delta.subprocess.Popen",
            RunningFakeXdeltaProcess,
        ), self.assertRaises(UosDeltaCancelled):
            cache.reconstruct(
                base_path=base,
                patch_path=patch_file,
                target_name="IntDemo-UOS-arm64-1.2.1.deb",
                target_version="1.2.1",
                target_size=14,
                target_sha256="0" * 64,
                cancelled_callback=cancelled,
            )

        self.assertFalse(
            (
                self.root
                / "updates"
                / "pending"
                / "IntDemo-UOS-arm64-1.2.1.deb.part"
            ).exists()
        )
        self.assertFalse(cache.lock_path.exists())


if __name__ == "__main__":
    unittest.main()
