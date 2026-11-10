#!/usr/bin/env python3
"""Build a BlueOS recovery package for Xiaomi Mi 11 Lite 4G (courbet).

The package is the LineageOS 24.0 base OTA with its system partition replaced
by the BlueOS GSI system image. Everything device-specific (boot, dtbo, vbmeta,
vendor, product, system_ext, odm, firmware) comes from the base unchanged.

Runs on Linux (GitHub Actions); needs openssl, python3-brotli and the AOSP
releasetools modules vendored in ./releasetools.
"""

import argparse
import bz2
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "releasetools"))

import common as releasetools  # noqa: E402

releasetools.OPTIONS = releasetools.Options()
releasetools.OPTIONS.cache_size = 2 * 1024 ** 3
releasetools.OPTIONS.stash_threshold = 0.8

from blockimgdiff import BlockImageDiff  # noqa: E402
from images import FileImage  # noqa: E402

BLOCK = 4096
SIGN_MSG = b"signed by SignApk"
SYSTEM_DATA_SUFFIXES = (
    ".new.dat",
    ".new.dat.br",
    ".new.dat.bz2",
    ".new.dat.gz",
    ".transfer.list",
    ".patch.dat",
)


def log(message):
    print(message, flush=True)


def die(message):
    print("ERROR: " + message, file=sys.stderr, flush=True)
    sys.exit(1)


def human(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024


def align_up(value, alignment=BLOCK):
    return (value + alignment - 1) // alignment * alignment


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def zip_entries(zf):
    return {item.filename: item for item in zf.infolist()}


def detect_ota(base_zip):
    with zipfile.ZipFile(base_zip) as zf:
        names = set(zf.namelist())
        info = zip_entries(zf)
        result = {
            "entries": len(names),
            "payload_bin": "payload.bin" in names,
            "system_entries": {
                name: info[name].file_size
                for name in sorted(names)
                if any(name == "system" + suffix for suffix in SYSTEM_DATA_SUFFIXES)
            },
            "has_dynamic_partitions_opinfo": "dynamic_partitions_opinfo" in names,
            "has_boot_img": "boot.img" in names,
            "has_dtbo_img": "dtbo.img" in names,
            "has_vbmeta_img": "vbmeta.img" in names,
            "has_recovery_img": "recovery.img" in names,
            "metadata": None,
            "transfer_version": None,
            "transfer_blocks": None,
        }
        if "META-INF/com/android/metadata" in names:
            raw = zf.read("META-INF/com/android/metadata").decode("utf-8", "replace")
            result["metadata"] = {
                line.split("=", 1)[0]: line.split("=", 1)[1]
                for line in raw.splitlines()
                if "=" in line
            }
        if "system.transfer.list" in names:
            lines = zf.read("system.transfer.list").decode().splitlines()
            if lines:
                result["transfer_version"] = int(lines[0])
            if len(lines) > 1:
                result["transfer_blocks"] = int(lines[1])
        if "dynamic_partitions_opinfo" in names:
            result["dynamic_partitions_opinfo"] = zf.read(
                "dynamic_partitions_opinfo"
            ).decode("utf-8", "replace")
        return result


def parse_opinfo(text):
    groups = {}
    partition_sizes = {}
    partition_groups = {}
    if text is None:
        return groups, partition_sizes, partition_groups
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        try:
            if fields[0] in ("add_group", "resize_group") and len(fields) >= 3:
                groups[fields[1]] = int(fields[2])
            elif fields[0] == "resize" and len(fields) >= 3:
                partition_sizes[fields[1]] = int(fields[2])
            elif fields[0] in ("add", "move") and len(fields) >= 3:
                partition_groups[fields[1]] = fields[2]
        except ValueError:
            continue
    return groups, partition_sizes, partition_groups


def resize_system_in_opinfo(text, new_size):
    groups, sizes, groups_of = parse_opinfo(text)
    if not sizes:
        return None, "dynamic_partitions_opinfo is not a text op list"
    old_size = sizes.get("system")
    if old_size is None:
        return None, "no 'resize system <size>' line in dynamic_partitions_opinfo"
    group = groups_of.get("system")
    if group and group in groups and groups[group] > 0:
        total = 0
        for name, size in sizes.items():
            if groups_of.get(name, group) == group:
                total += new_size if name == "system" else size
        if total > groups[group]:
            return None, (
                f"group {group} would need {human(total)}, "
                f"its max is {human(groups[group])}"
            )
    pattern = re.compile(r"^(\s*resize\s+system\s+)(\d+)(\s*)$", re.MULTILINE)
    if not pattern.search(text):
        return None, "cannot rewrite the system size line"
    return pattern.sub(lambda m: m.group(1) + str(new_size) + m.group(3), text, count=1), None


def inspect_gsi(gsi):
    with open(gsi, "rb") as f:
        head = f.read(16)
        if head[:2] != b"\x1f\x8b":
            die(f"{gsi} is not gzip-compressed")
        f.seek(-4, os.SEEK_END)
        isize = struct.unpack("<I", f.read(4))[0]
    with gzip.open(gsi, "rb") as f:
        first = f.read(4096)
    kind = "unknown"
    if first[:4] == b"\x3a\xff\x26\xed":
        kind = "sparse"
        blk_sz, total_blks = struct.unpack("<II", first[8:16])
        raw_size = blk_sz * total_blks
    elif first[1080:1082] == b"\x53\xef":
        kind = "ext4"
        raw_size = isize
    elif first[1024:1028] == b"\xe2\xe1\xf5\xe0":
        kind = "erofs"
        raw_size = isize
    else:
        raw_size = isize
    return {
        "compressed_size": os.path.getsize(gsi),
        "raw_size": raw_size,
        "filesystem": kind,
        "sha256": sha256_file(gsi),
    }


def decompress_gsi(gsi, target):
    log(f"Decompressing GSI -> {target}")
    with gzip.open(gsi, "rb") as src, open(target, "wb") as dst:
        while True:
            chunk = src.read(8 * 1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    size = os.path.getsize(target)
    if size % BLOCK:
        die(f"unpacked GSI size {size} is not a multiple of {BLOCK}")
    if open(target, "rb").read(4) == b"\x3a\xff\x26\xed":
        if not shutil.which("simg2img"):
            die("GSI is a sparse image and simg2img is not installed")
        raw = str(target) + ".raw"
        subprocess.run(["simg2img", str(target), raw], check=True)
        os.replace(raw, target)
    log(f"Unpacked GSI: {human(os.path.getsize(target))}")


def read_ota_signature(zip_path):
    size = os.path.getsize(zip_path)
    with open(zip_path, "rb") as f:
        f.seek(max(0, size - 6))
        tail = f.read()
    if len(tail) < 6:
        return None
    sig_start, marker, comment_size = struct.unpack("<HHH", tail)
    if marker != 0xFFFF or comment_size < 22 or comment_size > size:
        return None
    with open(zip_path, "rb") as f:
        f.seek(size - comment_size)
        comment = f.read()
    if not comment.startswith(SIGN_MSG + b"\0"):
        return None
    der_offset = len(SIGN_MSG) + 1
    der_size = sig_start - 6
    if der_size <= 0 or der_offset + der_size > len(comment):
        return None
    return {
        "comment_size": comment_size,
        "signature_start": sig_start,
        "der": comment[der_offset:der_offset + der_size],
        "signed_bytes": size - comment_size - 2,
    }


def openssl_cert_info(der, workdir, label):
    der_path = Path(workdir) / f"{label}.der"
    pem_path = Path(workdir) / f"{label}.pem"
    der_path.write_bytes(der)
    subprocess.run(
        ["openssl", "pkcs7", "-inform", "DER", "-in", str(der_path),
         "-print_certs", "-out", str(pem_path)],
        check=True, stdout=subprocess.DEVNULL,
    )
    subject = subprocess.run(
        ["openssl", "x509", "-in", str(pem_path), "-noout", "-subject"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    fingerprint = subprocess.run(
        ["openssl", "x509", "-in", str(pem_path), "-noout", "-fingerprint", "-sha256"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return subject.replace("subject=", ""), fingerprint.split("=", 1)[1]


def cert_fingerprint(cert_pem):
    out = subprocess.run(
        ["openssl", "x509", "-in", str(cert_pem), "-noout", "-fingerprint", "-sha256"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return out.split("=", 1)[1]


def head_to_stdin_sign(unsigned_zip, sig_der, key, cert, signed_bytes):
    if sys.platform.startswith("linux"):
        cmd = (
            f'head -c {signed_bytes} "{unsigned_zip}" | '
            f'openssl cms -sign -binary -noattr -md sha256 -in /dev/stdin '
            f'-signer "{cert}" -inkey "{key}" -outform DER -out "{sig_der}"'
        )
        subprocess.run(cmd, shell=True, executable="/bin/bash", check=True)
    else:
        prefix = Path(str(sig_der) + ".prefix")
        with open(unsigned_zip, "rb") as src, open(prefix, "wb") as dst:
            remaining = signed_bytes
            while remaining:
                chunk = src.read(min(8 * 1024 * 1024, remaining))
                if not chunk:
                    break
                dst.write(chunk)
                remaining -= len(chunk)
        subprocess.run(
            ["openssl", "cms", "-sign", "-binary", "-noattr", "-md", "sha256",
             "-in", str(prefix), "-signer", str(cert), "-inkey", str(key),
             "-outform", "DER", "-out", str(sig_der)],
            check=True,
        )
        prefix.unlink()


def verify_ota_signature(zip_path, sig_der, signed_bytes):
    if sys.platform.startswith("linux"):
        cmd = (
            f'head -c {signed_bytes} "{zip_path}" | '
            f'openssl cms -verify -binary -inform DER -in "{sig_der}" '
            f'-content /dev/stdin -noverify -out /dev/null'
        )
        subprocess.run(cmd, shell=True, executable="/bin/bash", check=True)
        return
    prefix = Path(str(sig_der) + ".verify")
    with open(zip_path, "rb") as src, open(prefix, "wb") as dst:
        remaining = signed_bytes
        while remaining:
            chunk = src.read(min(8 * 1024 * 1024, remaining))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)
    subprocess.run(
        ["openssl", "cms", "-verify", "-binary", "-inform", "DER",
         "-in", str(sig_der), "-content", str(prefix), "-noverify",
         "-out", os.devnull],
        check=True,
    )
    prefix.unlink()


def sign_ota(unsigned_zip, out_zip, key, cert, workdir):
    size = os.path.getsize(unsigned_zip)
    with open(unsigned_zip, "rb") as f:
        f.seek(size - 22)
        eocd = f.read(22)
    if eocd[:4] != b"PK\x05\x06" or struct.unpack("<H", eocd[20:22])[0] != 0:
        die("unsigned package has an unexpected EOCD or a ZIP comment")

    sig_der = Path(workdir) / "ota-signature.der"
    signed_bytes = size - 2
    head_to_stdin_sign(unsigned_zip, sig_der, key, cert, signed_bytes)
    der = sig_der.read_bytes()

    comment = SIGN_MSG + b"\0" + der + struct.pack("<H", len(der) + 6) + b"\xff\xff"
    total = len(comment) + 2
    if total > 0xFFFF:
        die("signature does not fit into a ZIP comment")
    comment += struct.pack("<H", total)

    with open(unsigned_zip, "rb") as src, open(out_zip, "wb") as dst:
        remaining = signed_bytes
        while remaining:
            chunk = src.read(min(8 * 1024 * 1024, remaining))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)
        dst.write(struct.pack("<H", total))
        dst.write(comment)

    verify_ota_signature(out_zip, sig_der, signed_bytes)
    with zipfile.ZipFile(out_zip) as zf:
        bad = zf.testzip()
        if bad:
            die(f"corrupt entry in signed package: {bad}")
    log(f"Signed package: {human(os.path.getsize(out_zip))}")
    return total


def parse_transfer_list(path):
    lines = [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
    if len(lines) < 4:
        die("transfer list is too short")
    version = int(lines[0])
    total_blocks = int(lines[1])
    commands = []
    new_blocks = 0
    for line in lines[4:]:
        fields = line.split()
        command = fields[0]
        if command in ("new", "zero", "erase"):
            numbers = [int(x) for x in fields[1].split(",")]
            count = numbers[0]
            values = numbers[1:]
            if len(values) != count:
                die(f"bad range count in transfer list line: {line}")
            pairs = list(zip(values[::2], values[1::2]))
            commands.append((command, pairs))
            if command == "new":
                new_blocks += sum(end - start for start, end in pairs)
        elif command == "move":
            commands.append((command, line))
        else:
            die(f"unexpected transfer list command: {line}")
    return version, total_blocks, commands, new_blocks


def compress_new_dat(new_dat, target, mode):
    log(f"Compressing {human(os.path.getsize(new_dat))} -> {target} ({mode})")
    if mode == "br":
        import brotli

        compressor = brotli.Compressor(quality=9, lgwin=24)
        with open(new_dat, "rb") as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(8 * 1024 * 1024)
                if not chunk:
                    break
                dst.write(compressor.process(chunk))
            dst.write(compressor.finish())
    elif mode == "bz2":
        with open(new_dat, "rb") as src, bz2.BZ2File(target, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
    elif mode == "none":
        shutil.move(new_dat, target)
    else:
        die(f"unsupported system compression: {mode}")


def rebuild_zip(base_zip, out_zip, replacements, replacement_bytes, drop):
    log(f"Rebuilding {out_zip}")
    with zipfile.ZipFile(base_zip) as zin, zipfile.ZipFile(
        out_zip, "w", allowZip64=True
    ) as zout:
        written = set()
        for item in zin.infolist():
            name = item.filename
            src_path = replacements.get(name)
            data = replacement_bytes.get(name)
            if src_path is None and data is None and name in drop:
                continue
            if src_path is None and data is None:
                copy_info = zipfile.ZipInfo(name, item.date_time)
                copy_info.compress_type = item.compress_type
                copy_info.external_attr = item.external_attr
                copy_info.create_system = item.create_system
                if item.comment:
                    copy_info.comment = item.comment
                with zin.open(item) as src, zout.open(copy_info, "w") as dst:
                    shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
                continue

            new_info = zipfile.ZipInfo(name, item.date_time)
            new_info.external_attr = item.external_attr
            new_info.create_system = item.create_system
            new_info.compress_type = zipfile.ZIP_STORED
            with zout.open(new_info, "w") as dst:
                if src_path is not None:
                    with open(src_path, "rb") as src:
                        shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
                else:
                    dst.write(data)
            written.add(name)

        for name, src_path in replacements.items():
            if name in written:
                continue
            new_info = zipfile.ZipInfo(name)
            new_info.compress_type = zipfile.ZIP_STORED
            with open(src_path, "rb") as src, zout.open(new_info, "w") as dst:
                shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
        for name, data in replacement_bytes.items():
            if name in written:
                continue
            new_info = zipfile.ZipInfo(name)
            new_info.compress_type = zipfile.ZIP_STORED
            with zout.open(new_info, "w") as dst:
                dst.write(data)


def run_inspect(args, report):
    base = detect_ota(args.base)
    gsi = inspect_gsi(args.gsi)
    groups, sizes, groups_of = parse_opinfo(base.pop("dynamic_partitions_opinfo", None))
    gsi_blocks = align_up(gsi["raw_size"]) // BLOCK
    partition_size = sizes.get("system")
    base_image_size = (
        base["transfer_blocks"] * BLOCK if base["transfer_blocks"] else None
    )
    fit_limit = max(filter(None, [partition_size, base_image_size]), default=None)
    report.update(
        {
            "base": base,
            "gsi": gsi,
            "opinfo": {
                "groups": groups,
                "partition_sizes": {k: human(v) for k, v in sizes.items()},
                "partition_groups": groups_of,
            },
            "fit": {
                "system_partition_size": human(partition_size) if partition_size else None,
                "base_image_size": human(base_image_size) if base_image_size else None,
                "gsi_raw_size": human(gsi["raw_size"]),
                "gsi_blocks": gsi_blocks,
                "fits_without_resize": bool(fit_limit and gsi["raw_size"] <= fit_limit),
                "needs_opinfo_resize": bool(fit_limit and gsi["raw_size"] > fit_limit),
            },
        }
    )


def run_build(args, report):
    workdir = Path(args.work_dir)
    workdir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base = detect_ota(args.base)
    gsi = inspect_gsi(args.gsi)
    report["base"] = base
    report["gsi"] = gsi

    with zipfile.ZipFile(args.base) as zf:
        names = set(zf.namelist())
        if base["payload_bin"]:
            die("base is an A/B payload.bin OTA; this port expects a non-A/B recovery zip")
        if "system.transfer.list" not in names:
            die("base has no system.transfer.list")
        base_transfer = zf.read("system.transfer.list").decode().splitlines()
        base_version = int(base_transfer[0])
        base_blocks = int(base_transfer[1])
        if base_version != 4:
            die(f"unsupported base transfer list version {base_version}")
        opinfo = zf.read("dynamic_partitions_opinfo").decode() if "dynamic_partitions_opinfo" in names else None

    compression = None
    for suffix, mode in ((".new.dat.br", "br"), (".new.dat.bz2", "bz2"), (".new.dat", "none")):
        if "system" + suffix in names:
            compression = mode
            break
    if compression is None:
        die("base has no known system.new.dat entry")

    partition_size = parse_opinfo(opinfo)[1].get("system")
    fit_limit = max(filter(None, [partition_size, base_blocks * BLOCK]), default=None)
    report["fit"] = {
        "system_partition_size": human(partition_size) if partition_size else None,
        "base_image_size": human(base_blocks * BLOCK),
        "gsi_raw_size": human(gsi["raw_size"]),
        "fits_without_resize": bool(fit_limit and gsi["raw_size"] <= fit_limit),
    }

    gsi_img = workdir / "system.img"
    decompress_gsi(args.gsi, gsi_img)
    gsi_size = os.path.getsize(gsi_img)
    gsi_blocks = gsi_size // BLOCK

    replacement_bytes = {}
    if fit_limit is not None and gsi_size > fit_limit:
        if not opinfo:
            die("GSI does not fit and there is no dynamic_partitions_opinfo to resize")
        new_opinfo, error = resize_system_in_opinfo(opinfo, gsi_size)
        if error:
            die(f"cannot resize system partition: {error}")
        replacement_bytes["dynamic_partitions_opinfo"] = new_opinfo.encode()
        opinfo = new_opinfo
        log(f"Resized system in dynamic_partitions_opinfo to {human(gsi_size)}")

    log("Computing block transfer list")
    image = FileImage(str(gsi_img))
    diff = BlockImageDiff(
        image, None, threads=max(1, (os.cpu_count() or 2) // 2),
        version=base_version, disable_imgdiff=True,
    )
    prefix = workdir / "system"
    diff.Compute(str(prefix))
    new_dat = Path(str(prefix) + ".new.dat")
    transfer_list = Path(str(prefix) + ".transfer.list")
    if not new_dat.exists() or not transfer_list.exists():
        die("blockimgdiff did not produce new.dat/transfer.list")

    version, total_blocks, commands, new_blocks = parse_transfer_list(transfer_list)
    report["transfer"] = {
        "version": version,
        "total_blocks": total_blocks,
        "commands": len(commands),
        "new_blocks": new_blocks,
        "new_dat_size": human(os.path.getsize(new_dat)),
        "expected_new_dat_size": human(new_blocks * BLOCK),
    }
    if total_blocks != gsi_blocks:
        die(f"transfer list total {total_blocks} != GSI blocks {gsi_blocks}")
    if os.path.getsize(new_dat) != new_blocks * BLOCK:
        die("new.dat size does not match the 'new' ranges in transfer list")

    compressed = workdir / ("system.new.dat." + compression if compression != "none" else "system.new.dat")
    compress_new_dat(new_dat, compressed, compression)

    system_data_drop = {
        name for name in names
        if name.startswith("system.")
        and (name.endswith(".new.dat") or name.endswith(".new.dat.br")
             or name.endswith(".new.dat.bz2") or name.endswith(".new.dat.gz")
             or name.endswith(".transfer.list") or name.endswith(".patch.dat"))
    }
    replacements = {
        "system" + (".new.dat.br" if compression == "br" else
                    ".new.dat.bz2" if compression == "bz2" else ".new.dat"): compressed,
        "system.transfer.list": transfer_list,
    }
    replacement_bytes["system.patch.dat"] = b""

    unsigned = workdir / "BlueOS-unsigned.zip"
    rebuild_zip(args.base, unsigned, replacements, replacement_bytes, system_data_drop)

    if os.path.getsize(unsigned) > 4 * 1024 ** 3:
        log("WARNING: unsigned zip is over 4 GiB; some recoveries cannot handle it")

    sig = read_ota_signature(args.base)
    base_signer = None
    if sig:
        subject, fingerprint = openssl_cert_info(sig["der"], workdir, "base-signer")
        base_signer = {"subject": subject, "sha256": fingerprint}
        report["base_signer"] = base_signer
        verify_ota_signature(args.base, Path(workdir) / "base-signer.der", sig["signed_bytes"])
        expected = cert_fingerprint(args.cert)
        if fingerprint != expected and not args.allow_key_mismatch:
            die(
                "base is signed with a different key:\n"
                f"  base:    {subject} {fingerprint}\n"
                f"  testkey: {expected}\n"
                "The base recovery would reject a testkey-signed port. "
                "Pass --allow-key-mismatch only for flashing via a recovery that trusts test-keys."
            )
    else:
        report["base_signer"] = None
        log("WARNING: base package has no whole-file signature")

    signed = out_dir / args.output_name
    total_comment = sign_ota(unsigned, signed, args.key, args.cert, workdir)
    report["output"] = {
        "file": signed.name,
        "size": signed.stat().st_size,
        "size_human": human(signed.stat().st_size),
        "sha256": sha256_file(signed),
        "zip_comment": total_comment,
    }
    unsigned.unlink()
    (out_dir / "SHA256SUMS").write_text(
        f"{report['output']['sha256']}  {signed.name}\n"
    )
    log(f"Done: {signed} ({human(signed.stat().st_size)})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["inspect", "build"])
    parser.add_argument("--base", required=True)
    parser.add_argument("--gsi", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--device", default="courbet")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--key")
    parser.add_argument("--cert")
    parser.add_argument("--version", default="0.8.1")
    parser.add_argument("--output-name", default=None)
    parser.add_argument("--allow-key-mismatch", action="store_true")
    args = parser.parse_args()

    report = {
        "device": args.device,
        "mode": args.mode,
        "base_file": os.path.basename(args.base),
        "gsi_file": os.path.basename(args.gsi),
    }
    if args.mode == "inspect":
        run_inspect(args, report)
    else:
        if not args.key or not args.cert:
            die("--key and --cert are required for build")
        args.work_dir = args.work_dir or os.path.join(args.out_dir, "work")
        if not args.output_name:
            from datetime import datetime, timezone
            args.output_name = (
                f"BlueOS-{args.version}-{args.device}-"
                f"{datetime.now(timezone.utc):%Y%m%d}.zip"
            )
        run_build(args, report)

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    log(f"Report: {args.report}")


if __name__ == "__main__":
    main()
