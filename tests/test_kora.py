"""Offline unit tests for kora (no VMs, no network, no clouddevbox).

The extensionless polyglot script is loaded via SourceFileLoader; the sh
header parses as a Python string expression, and everything below
`if __name__ == "__main__":` never runs on import.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

loader = importlib.machinery.SourceFileLoader(
    "kora", str(Path(__file__).resolve().parent.parent / "kora"))
spec = importlib.util.spec_from_loader("kora", loader)
kora = importlib.util.module_from_spec(spec)
loader.exec_module(kora)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "korahome"
    monkeypatch.setenv("KORA_HOME", str(h))
    return h


# ---------------------------------------------------------------------------
# util
# ---------------------------------------------------------------------------
def test_parse_size():
    assert kora.parse_size("2G", "x") == ("2G", 2048)
    assert kora.parse_size("2048M", "x") == ("2048M", 2048)
    assert kora.parse_size("30", "x") == ("30G", 30 * 1024)
    with pytest.raises(kora.CliError, match="invalid --ram"):
        kora.parse_size("lots", "--ram")


def test_alloc_port_skips_bound(monkeypatch):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    busy = s.getsockname()[1]
    try:
        got = kora.alloc_port(busy, busy + 3)
        assert got != busy and busy < got <= busy + 3
    finally:
        s.close()


def test_build_remote_command():
    # single arg = shell string verbatim; multiple args = quoted per token
    assert kora.build_remote_command(["a && b"]) == "a && b"
    assert kora.build_remote_command(["echo", "two words"]) == "echo 'two words'"


# ---------------------------------------------------------------------------
# home / state
# ---------------------------------------------------------------------------
def test_home_created_0700(home):
    h = kora.kora_home()
    assert h == home and h.is_dir()
    assert (h.stat().st_mode & 0o777) == 0o700


def test_state_roundtrip(home):
    assert kora.load_state() is None
    vm = {"name": "kora-debian-abc123", "os": "debian", "backend": "tart",
          "ssh": {"user": "admin", "host": "1.2.3.4", "port": 22, "key": "/k"}}
    kora.save_state(vm)
    assert kora.load_state() == vm
    kora.clear_state()
    assert kora.load_state() is None


def test_require_vm_errors_when_empty(home):
    with pytest.raises(kora.CliError, match="no VM exists"):
        kora.require_vm()


def test_corrupt_state_is_loud(home):
    kora.state_path().write_text("{nope")
    with pytest.raises(kora.CliError, match="corrupt state"):
        kora.load_state()


def test_new_fails_when_vm_exists(home):
    kora.save_state({"name": "kora-debian-x", "os": "debian", "backend": "tart"})
    with pytest.raises(kora.CliError, match="one VM at a time"):
        kora.main(["new", "ubuntu"])


# ---------------------------------------------------------------------------
# catalog / backend dispatch
# ---------------------------------------------------------------------------
def test_backend_dispatch_darwin(monkeypatch):
    monkeypatch.setattr(kora, "HOST_OS", "darwin")
    assert kora.backend_name_for("sequoia") == "tart"
    assert kora.backend_name_for("debian") == "tart"
    assert kora.backend_name_for("ubuntu") == "tart"
    assert kora.backend_name_for("arch") == "qemu-arch"


def test_backend_dispatch_linux(monkeypatch):
    monkeypatch.setattr(kora, "HOST_OS", "linux")
    for os_name in ("debian", "ubuntu", "arch"):
        assert kora.backend_name_for(os_name) == "qemu-cloudimg"
    with pytest.raises(kora.CliError, match="Apple Silicon"):
        kora.backend_name_for("sequoia")


def test_unknown_os():
    with pytest.raises(kora.CliError, match="unknown OS"):
        kora.backend_name_for("plan9")


def test_sve_fix_only_for_tart_linux_guests():
    assert kora.CATALOG["debian"]["sve_fix"] and kora.CATALOG["ubuntu"]["sve_fix"]
    assert not kora.CATALOG["sequoia"]["sve_fix"]
    assert not kora.CATALOG["arch"]["sve_fix"]


# ---------------------------------------------------------------------------
# ssh / scp argv
# ---------------------------------------------------------------------------
VM = {"name": "kora-debian-abc123", "os": "debian", "backend": "qemu-cloudimg",
      "ram": "4G", "cpu": 4, "hd": "30G",
      "ssh": {"user": "kora", "host": "127.0.0.1", "port": 22317, "key": "/tmp/k"},
      "qemu": {"ssh_port": 22317, "vnc_display": 17, "arch": "x86_64"}}


def test_ssh_argv():
    argv = kora.ssh_argv(VM, "uname -a")
    assert argv[0] == "ssh" and argv[-1] == "uname -a"
    assert "BatchMode=yes" in argv and "kora@127.0.0.1" in argv
    assert argv[argv.index("-p") + 1] == "22317"
    interactive = kora.ssh_argv(VM, interactive=True)
    assert "BatchMode=yes" not in interactive


def test_scp_argv():
    up = kora.scp_argv(VM, "/tmp/f", "/home/kora/f", to_vm=True)
    assert up[0] == "scp" and up[-1] == "kora@127.0.0.1:/home/kora/f"
    assert up[up.index("-P") + 1] == "22317"
    down = kora.scp_argv(VM, "/etc/os-release", "./rel", to_vm=False)
    assert down[-2] == "kora@127.0.0.1:/etc/os-release" and down[-1] == "./rel"


def test_copy_spec():
    assert kora.parse_copy_spec("a.txt", "vm:/tmp/") == ("/tmp/", "a.txt", True)
    assert kora.parse_copy_spec("vm:/etc/f", "./f") == ("/etc/f", "./f", False)
    for src, dst in (("a", "b"), ("vm:/a", "vm:/b")):
        with pytest.raises(kora.CliError, match="exactly one"):
            kora.parse_copy_spec(src, dst)


# ---------------------------------------------------------------------------
# qemu argv builders
# ---------------------------------------------------------------------------
def test_qemu_cloudimg_argv(home):
    argv = kora.qemu_cloudimg_argv(VM)
    joined = " ".join(argv)
    assert argv[0] == "qemu-system-x86_64"
    assert "-enable-kvm" in argv
    # loopback-only hostfwd preserves the devbox zero-ingress invariant
    assert "hostfwd=tcp:127.0.0.1:22317-:22" in joined
    assert "-vnc" in argv and "127.0.0.1:17" in argv
    assert "-pidfile" in argv and "-daemonize" in argv
    assert "seed.iso" in joined and "disk.qcow2" in joined


def test_qemu_arch_argv(home):
    vm = dict(VM, backend="qemu-arch",
              qemu={"ssh_port": 22301, "vnc_display": 11, "arch": "aarch64"})
    run_phase = " ".join(kora.qemu_arch_argv(vm))
    assert "qemu-system-aarch64" in run_phase and "accel=hvf" in run_phase
    assert "hostfwd=tcp:127.0.0.1:22301-:22" in run_phase
    assert "-cdrom" not in run_phase
    assert "file:" in run_phase and "console.log" in run_phase
    install = " ".join(kora.qemu_arch_argv(vm, install_iso="/x/a.iso"))
    # install phase forwards archboot's sshd, not 22, boots the ISO, and
    # exposes the serial console on a unix socket for _serial_netfix
    assert "hostfwd=tcp:127.0.0.1:22301-:11838" in install
    assert "-cdrom /x/a.iso" in install and "-boot d" in install
    # serial socket lives in a SHORT /tmp path (macOS AF_UNIX 104-byte cap),
    # not under a possibly-deep KORA_HOME
    assert "unix:/tmp/kora-arch-22301.sock" in install
    assert kora.arch_serial_sock(vm) == "/tmp/kora-arch-22301.sock"


def test_user_data_render():
    ud = kora.render_user_data("kora-debian-x", "ssh-ed25519 AAAA test")
    assert ud.startswith("#cloud-config")
    assert "name: kora" in ud
    assert "NOPASSWD:ALL" in ud
    assert "ssh-ed25519 AAAA test" in ud
    assert "ssh_pwauth: false" in ud
    md = kora.render_meta_data("kora-debian-x")
    assert "instance-id: kora-debian-x" in md


# ---------------------------------------------------------------------------
# cloud proxy plumbing
# ---------------------------------------------------------------------------
def test_proxy_argv_quoting():
    argv = kora.proxy_argv("p1", ["cmd", "echo 'a b'"])
    assert argv[:5] == ["clouddevbox", "ssh", "kvm", "--profile", "p1"]
    assert "--" in argv and "~/bin/kora" in argv
    # the kora arg survives the box shell as ONE token
    import shlex as _sh
    tail = argv[argv.index("~/bin/kora"):]
    assert _sh.split(" ".join(tail)) == ["~/bin/kora", "cmd", "echo 'a b'"]


def test_proxy_argv_tty():
    assert "-t" in kora.proxy_argv("p", ["ssh"], tty=True)
    assert "-t" not in kora.proxy_argv("p", ["ssh"])


def test_resolve_profile_passthrough():
    assert kora.resolve_profile("given") == "given"


def test_archboot_installer_embedded():
    # attribution must survive (GPL-3.0-or-later, upstream author)
    assert "SPDX-License-Identifier: GPL-3.0-or-later" in kora.ARCHBOOT_INSTALL_SH
    assert "Tobias Powalowski" in kora.ARCHBOOT_INSTALL_SH
    assert "/tmp/test_key.pub" in kora.ARCHBOOT_INSTALL_SH


# ---------------------------------------------------------------------------
# cli surface
# ---------------------------------------------------------------------------
def test_parser_surface():
    p = kora.build_parser()
    a = p.parse_args(["new", "debian", "--ram", "2G", "--cpu", "4", "--hd", "20G"])
    assert (a.os, a.ram, a.cpu, a.hd, a.cloud) == ("debian", "2G", 4, "20G", False)
    a = p.parse_args(["new", "debian", "--cloud", "--profile", "p", "--yes", "--force"])
    assert a.cloud and a.profile == "p" and a.yes and a.force
    a = p.parse_args(["status"])
    assert a.side is None and not a.json
    a = p.parse_args(["status", "cloud"])
    assert a.side == "cloud"
    a = p.parse_args(["tun", "8080", "80"])
    assert (a.local_port, a.remote_port) == (8080, 80)
    a = p.parse_args(["cmd", "uname", "-a"])
    assert a.command == ["uname", "-a"]


def test_status_json_no_vm(home, capsys):
    kora.main(["status", "--json"])
    assert json.loads(capsys.readouterr().out) == {"vm": None}


def test_status_json_local(home, capsys, monkeypatch):
    kora.save_state(dict(VM))
    monkeypatch.setattr(kora.QemuCloudimgBackend, "is_running",
                        classmethod(lambda cls, vm: True))
    kora.main(["status", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["running"] is True
    assert out["vm"]["name"] == VM["name"]
    assert out["vm"]["qemu"]["ssh_port"] == 22317
    assert "vm_dir" in out and "home" in out


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        kora.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"kora {kora.VERSION}"


# ---------------------------------------------------------------------------
# omarchy (cloud-only ISO installer) + reset (v0.2.0)
# ---------------------------------------------------------------------------
OMARCHY_VM = {
    "name": "kora-omarchy-abc123", "os": "omarchy", "backend": "qemu-omarchy",
    "ram": "8G", "cpu": 4, "hd": "40G",
    "ssh": {"user": "gonzalo", "host": "127.0.0.1", "port": 22350,
            "key": "/cache/omarchy-base/aa-bb/id_ed25519"},
    "qemu": {"ssh_port": 22350, "vnc_display": 22, "arch": "x86_64",
             "ovmf_code": "/usr/share/OVMF/OVMF_CODE_4M.fd",
             "iso": "/cache/omarchy/4.0.2/omarchy-4.0.2.iso",
             "qmp_sock": "/tmp/vms/kora-omarchy-abc123/qmp.sock"},
}


def test_omarchy_catalog_shape():
    o = kora.CATALOG["omarchy"]
    assert o["tart"] is None and o["cloudimg"] is None
    assert o["cloud_only"] and o["backend"] == "qemu-omarchy"
    assert o["instance_type"] == "m7i.xlarge"
    assert "{ver}" in o["iso"]


def test_omarchy_backend_dispatch(monkeypatch):
    # darwin: cloud-only, rejected locally
    monkeypatch.setattr(kora, "HOST_OS", "darwin")
    with pytest.raises(kora.CliError, match="cloud-only"):
        kora.backend_name_for("omarchy")
    # linux + /dev/kvm: resolves to the omarchy backend
    monkeypatch.setattr(kora, "HOST_OS", "linux")
    monkeypatch.setattr(kora.Path, "exists", lambda self: True)
    assert kora.backend_name_for("omarchy") == "qemu-omarchy"


def test_omarchy_iso_url(monkeypatch):
    monkeypatch.setattr(kora, "OMARCHY_VERSION", "4.0.2")
    assert kora.omarchy_iso_url() == "https://iso.omarchy.org/omarchy-4.0.2.iso"


def test_omarchy_argv_run_vs_install(home):
    run = " ".join(str(a) for a in kora.qemu_omarchy_argv(OMARCHY_VM, install=False))
    assert "qemu-system-x86_64" in run and "q35,accel=kvm" in run and "-enable-kvm" in run
    assert "if=pflash" in run and "OVMF_CODE_4M.fd" in run          # UEFI, not SeaBIOS
    assert "virtio-blk-pci,drive=disk0,bootindex=1" in run
    assert "virtio-vga" in run and "127.0.0.1:22" in run            # display for kora vnc
    assert "hostfwd=tcp:127.0.0.1:22350-:22" in run                 # loopback-only
    assert "usb-tablet" in run and "qmp" in run
    assert "cidata" not in run and "media=cdrom" not in run         # no installer on run
    inst = " ".join(str(a) for a in kora.qemu_omarchy_argv(OMARCHY_VM, install=True))
    assert "omarchy-4.0.2.iso,media=cdrom" in inst
    assert "ide-cd,drive=cd0,bootindex=2" in inst                   # ISO lower boot prio
    assert "cidata.iso,media=cdrom" in inst and "ide-cd,drive=cd1" in inst


def test_omarchy_cidata_render(tmp_path, monkeypatch):
    monkeypatch.setattr(kora, "ensure_iso_tool", lambda: ["true"])
    monkeypatch.setattr(kora, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    iso = tmp_path / "cidata.iso"
    key = kora.build_cidata_iso(iso, "gonzalo", "$6$deadbeef$hash",
                                "ssh-ed25519 AAAA test")
    staged = tmp_path / "cidata"
    cfg = json.loads((staged / "user_configuration.json").read_text())
    creds = json.loads((staged / "user_credentials.json").read_text())
    ak = (staged / "authorized_keys").read_text()
    # no LUKS: no disk_encryption block, no encrypt flag file
    assert "disk_encryption" not in cfg
    assert not (staged / "user_encrypt_installation.txt").exists()
    # user gonzalo, sudo, installs to the virtio disk
    assert creds["users"][0]["username"] == "gonzalo"
    assert creds["users"][0]["sudo"] is True
    assert creds["users"][0]["enc_password"] == "$6$deadbeef$hash"
    assert cfg["disk_config"]["device_modifications"][0]["device"] == "/dev/vda"
    assert "ssh-ed25519 AAAA test" in ak
    assert isinstance(key, str) and len(key) == 12    # base cache key


def test_omarchy_credentials_no_root():
    c = kora.omarchy_user_credentials("gonzalo", "H")
    assert c["root_enc_password"] is None
    assert c["users"] == [{"username": "gonzalo", "enc_password": "H", "sudo": True}]


def test_omarchy_defaults(monkeypatch, home):
    # cmd_new (local path on a faked linux+kvm box) applies omarchy headroom
    monkeypatch.setattr(kora, "HOST_OS", "linux")
    monkeypatch.setattr(kora.Path, "exists", lambda self: True)
    captured = {}
    monkeypatch.setattr(kora.QemuOmarchyBackend, "create",
                        classmethod(lambda cls, vm: captured.update(vm)))
    monkeypatch.setattr(kora, "save_state", lambda vm: None)
    kora.main(["new", "omarchy"])
    assert captured["ram"] == "8G" and captured["cpu"] == 4 and captured["hd"] == "40G"


def test_cmd_list_omarchy_row(monkeypatch, capsys):
    monkeypatch.setattr(kora.shutil, "which", lambda x: "/usr/bin/" + x)
    monkeypatch.setattr(kora.Path, "exists", lambda self: True)
    kora.main(["list"])
    out = capsys.readouterr().out
    line = [ln for ln in out.splitlines() if ln.startswith("omarchy")][0]
    assert "cloud only" in line and "m7i.xlarge" in line


def test_ensure_devbox_fresh_recreates(monkeypatch):
    calls = []
    monkeypatch.setattr(kora, "box_status", lambda p: ("running", ""))
    monkeypatch.setattr(kora, "confirm", lambda *a, **k: True)
    def fake_cdb(*argv, capture=False):
        calls.append(argv)
        if capture:
            return (0, "fully provisioned", "")
        return 0
    monkeypatch.setattr(kora, "_cdb", fake_cdb)
    kora.ensure_devbox("p", True, instance_type="m7i.xlarge", fresh=True)
    # an existing box is destroyed then recreated at the requested type
    assert ("destroy", "kvm", "--yes", "--profile", "p") in calls
    newc = [c for c in calls if c[0] == "new"][0]
    assert "--type" in newc and "m7i.xlarge" in newc and "--kvm" in newc


def test_ensure_devbox_nonfresh_reuses(monkeypatch):
    calls = []
    monkeypatch.setattr(kora, "box_status", lambda p: ("running", ""))
    def fake_cdb(*argv, capture=False):
        calls.append(argv)
        return (0, "fully provisioned", "") if capture else 0
    monkeypatch.setattr(kora, "_cdb", fake_cdb)
    kora.ensure_devbox("p", True)          # amun path: reuse, never destroy
    assert not any(c[0] in ("destroy", "new") for c in calls)


def test_parser_reset():
    args = kora.build_parser().parse_args(["reset"])
    assert args.fn is kora.cmd_reset


def test_reset_requires_vm(home):
    with pytest.raises(kora.CliError, match="no VM exists"):
        kora.main(["reset"])


def test_reset_local_uses_backend_reset(home, monkeypatch):
    kora.save_state(dict(OMARCHY_VM, backend="qemu-omarchy"))
    done = {}
    monkeypatch.setattr(kora.QemuOmarchyBackend, "reset",
                        classmethod(lambda cls, vm: done.setdefault("reset", True)))
    monkeypatch.setattr(kora, "save_state", lambda vm: None)
    kora.main(["reset"])
    assert done.get("reset")
