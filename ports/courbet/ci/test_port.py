import contextlib
import os
import shutil
import io
import struct
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

def find_bash():
    candidates = [
        shutil.which("bash"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Git", "bin", "bash.exe"),
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            subprocess.run(
                [candidate, "-c", "exit 0"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        return candidate
    return None


BASH = find_bash()

import port


class UpdaterScriptTests(unittest.TestCase):
    def test_remove_backup_and_restore_without_changing_partition_writes(self):
        writes = (
            'assert(update_dynamic_partitions(package_extract_file("dynamic_partitions_op_list")));\n'
            'block_image_update(map_partition("system"), package_extract_file("system.transfer.list"), "system.new.dat.br", "system.patch.dat");\n'
            'package_extract_file("boot.img", "/dev/block/bootdevice/by-name/boot");\n'
        )
        source = (
            'run_program("/tmp/install/bin/backuptool.sh", "backup", map_partition("system"), "ext4");\n'
            + writes
            + 'run_program("/tmp/install/bin/backuptool.sh", "restore", map_partition("system"), "ext4");\n'
        )
        self.assertEqual(port.prepare_updater_script(source), writes)

    def test_handles_crlf_and_indentation(self):
        source = (
            'ui_print("BlueOS");\r\n'
            '    run_program("/tmp/install/bin/backuptool.sh", "backup", map_partition("system"), "ext4");\r\n'
            'set_progress(1.000000);\r\n'
        )
        self.assertEqual(
            port.prepare_updater_script(source),
            'ui_print("BlueOS");\r\nset_progress(1.000000);\r\n',
        )

    def test_no_addon_calls_is_unchanged(self):
        source = 'ui_print("BlueOS");\nset_progress(1.000000);\n'
        self.assertEqual(port.prepare_updater_script(source), source)

    def test_unknown_invocation_is_rejected(self):
        source = 'run_program("/tmp/install/bin/backuptool.sh", "restore", "/system", "ext4");\n'
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            port.prepare_updater_script(source)

    def test_zip_contains_replaced_script_and_unchanged_images(self):
        entry = "META-INF/com/google/android/updater-script"
        source = (
            'ui_print("BlueOS");\n'
            'run_program("/tmp/install/bin/backuptool.sh", "restore", map_partition("system"), "ext4");\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base.zip"
            output = Path(directory) / "output.zip"
            with zipfile.ZipFile(base, "w") as archive:
                archive.writestr(entry, source)
                archive.writestr("boot.img", b"unchanged boot")
                archive.writestr("vendor.new.dat.br", b"unchanged vendor")
            replacement = port.prepare_updater_script(source).encode()
            port.rebuild_zip(base, output, {}, {entry: replacement}, set())
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read(entry), b'ui_print("BlueOS");\n')
                self.assertEqual(archive.read("boot.img"), b"unchanged boot")
                self.assertEqual(archive.read("vendor.new.dat.br"), b"unchanged vendor")
                self.assertEqual(archive.namelist().count(entry), 1)


class VndkDetectionTests(unittest.TestCase):
    def test_sdk_fallback_removed_and_stale_37_cleared(self):
        source = (
            b'if [ -z "$(getprop persist.sys.vndk)" ]; then\n'
            b"\tvndk_version=$(grep -m1 '^ro\\.vndk\\.version=' /vendor/build.prop 2>/dev/null | cut -d= -f2)\n"
            b'\tif [ -z "$vndk_version" ]; then\n'
            b"\t\tvndk_version=$(grep -m1 '^ro\\.vendor\\.build\\.version\\.sdk=' /vendor/build.prop 2>/dev/null | cut -d= -f2)\n"
            b'\tfi\n'
            b'\tif [ -n "$vndk_version" ]; then\n'
            b'\t\tsetprop persist.sys.vndk "$vndk_version"\n'
            b'\tfi\n'
            b'fi\n'
        )
        patched = port.patch_vndk_script(source)
        self.assertEqual(len(patched), len(source))
        self.assertNotIn(b'ro\\.vendor\\.build\\.version\\.sdk=', patched)
        self.assertIn(b'ro\\.vndk\\.version=', patched)
        self.assertIn(b'if [ "$(getprop persist.sys.vndk)" = 37 ]; then', patched)
        self.assertIn(b'setprop persist.sys.vndk ""', patched)
        self.assertLess(patched.index(b'= 37'), patched.index(b'if [ -z "$(getprop'))

    def test_unexpected_script_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            port.patch_vndk_script(b"#!/system/bin/sh\n")

    def test_changed_file_length_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            port.rewrite_ext4_file("unused.img", "/unused", b"original", b"short")


class CourbetAudioTests(unittest.TestCase):
    BLOCK = (
        b'if [ $(find /vendor/etc/audio -type f |wc -l) -le 3 ];then\n'
        b'\tmount -o bind /mnt/phh/empty_dir /vendor/etc/audio || true\n'
        b'fi\n'
    )

    def test_only_audio_hiding_block_removed_and_length_preserved(self):
        before = b'#!/system/bin/sh\nsetprop test.before true\n'
        after = (
            b'mount -o bind /mnt/phh/empty_dir /vendor/lib64/soundfx\n'
            b'setprop test.after true\n'
        )
        source = before + self.BLOCK + after
        patched = port.patch_courbet_audio_script(source)
        self.assertEqual(len(patched), len(source))
        self.assertEqual(patched, before + after + b" " * len(self.BLOCK))
        self.assertNotIn(b'/vendor/etc/audio', patched)

    def test_missing_or_duplicate_heuristic_rejected(self):
        for source in (b'#!/system/bin/sh\n', self.BLOCK * 2):
            with self.subTest(source=source):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    port.patch_courbet_audio_script(source)

    def test_changed_heuristic_rejected(self):
        source = self.BLOCK.replace(b'-le 3', b'-le 4')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            port.patch_courbet_audio_script(source)

    @unittest.skipUnless(BASH, "a POSIX shell is required for this test")
    def test_shell_does_not_hide_sparse_or_symlink_audio_directories(self):
        for count in (0, 3, 4):
            bootstrap = (
                f'find() {{ for ((i=0; i<{count}; i++)); do printf "config\\n"; done; }}\n'
                'mount() { printf "unexpected-audio-mount\\n"; }\n'
            )
            source = self.BLOCK + b'printf "after\\n"\n'
            for patched in (False, True):
                with self.subTest(count=count, patched=patched):
                    script = port.patch_courbet_audio_script(source) if patched else source
                    result = subprocess.run(
                        [BASH, "-c", bootstrap + script.decode()],
                        check=True, capture_output=True, text=True, timeout=5,
                    )
                    expected = "after\n"
                    if not patched and count <= 3:
                        expected = "unexpected-audio-mount\n" + expected
                    self.assertEqual(result.stdout, expected)
                    self.assertEqual(result.stderr, "")


class Ext4PathTests(unittest.TestCase):
    def test_falls_back_to_system_prefixed_path(self):
        responses = [
            SimpleNamespace(returncode=1, stdout=b"File not found"),
            SimpleNamespace(returncode=0, stdout=b"Inode: 123"),
        ]
        with mock.patch.object(port.subprocess, "run", side_effect=responses):
            self.assertEqual(
                port.find_ext4_path(
                    "system.img", ["/bin/vndk-detect", "/system/bin/vndk-detect"]
                ),
                "/system/bin/vndk-detect",
            )

    def test_missing_paths_are_rejected(self):
        response = SimpleNamespace(returncode=1, stdout=b"File not found")
        with mock.patch.object(port.subprocess, "run", return_value=response):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                port.find_ext4_path("system.img", ["/bin/missing"])

class Ext4RewriteTests(unittest.TestCase):
    def test_only_mapped_file_bytes_change(self):
        old = b"A" * 5000
        new = b"B" * 5000
        before = bytearray(b"X" * (port.BLOCK * 5))
        before[1080:1082] = b"\x53\xef"
        struct.pack_into("<I", before, 1048, 2)
        before[port.BLOCK * 3:port.BLOCK * 3 + len(old)] = old
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "system.img"
            image.write_bytes(before)
            mapped = [SimpleNamespace(stdout="3\n"), SimpleNamespace(stdout="4\n")]
            with mock.patch.object(port.subprocess, "run", side_effect=mapped):
                port.rewrite_ext4_file(image, "/system/bin/vndk-detect", old, new)
            expected = before.copy()
            expected[port.BLOCK * 3:port.BLOCK * 3 + len(old)] = new
            self.assertEqual(image.read_bytes(), bytes(expected))

    def test_mapping_mismatch_never_partially_writes(self):
        old = b"A" * 5000
        before = bytearray(b"X" * (port.BLOCK * 5))
        before[1080:1082] = b"\x53\xef"
        struct.pack_into("<I", before, 1048, 2)
        before[port.BLOCK * 3:port.BLOCK * 4] = old[:port.BLOCK]
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "system.img"
            image.write_bytes(before)
            mapped = [SimpleNamespace(stdout="3\n"), SimpleNamespace(stdout="4\n")]
            with mock.patch.object(port.subprocess, "run", side_effect=mapped):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    port.rewrite_ext4_file(image, "/system/bin/vndk-detect", old, b"B" * len(old))
            self.assertEqual(image.read_bytes(), bytes(before))


if __name__ == "__main__":
    unittest.main()
