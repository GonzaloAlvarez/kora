# kora

One VM at a time, local or cloud. kora creates, connects to and disposes of a
single virtual machine per state home — on this machine via tart or QEMU, or
on a persistent AWS devbox (via [clouddevbox]) when the guest needs real
`/dev/kvm` or an x86_64 host.

## Usage

```sh
kora new debian                        # local VM (tart on macOS, QEMU+KVM on Linux)
kora new sequoia --ram 8G --cpu 4      # macOS guest (Apple Silicon Mac only)
kora new debian --cloud                # QEMU+KVM guest on the 'kvm' devbox (billable)
kora new omarchy --cloud               # Omarchy (Arch+Hyprland) desktop, cloud-only
kora new arch --force                  # replace whatever VM exists
kora list                              # catalog: which OS runs where
kora status                            # the active VM; 'kora status cloud' = the devbox
kora cmd 'uname -a && id'              # run a command (single arg = shell string)
kora ssh                               # interactive shell
kora copy report.txt vm:/tmp/          # scp either direction (one side vm:<path>)
kora tun 8080 80                       # localhost:8080 -> VM's 127.0.0.1:80
kora vnc                               # print (and open) the display URL
kora stop && kora start                # halt (disk kept) / resume
kora reset                             # wipe back to clean (cloud: resets the nested
                                       # guest, never stops the devbox)
kora rm                                # delete disk + state
```

## What it does

1. **One VM per state home.** `kora new` refuses to create a second VM
   (`--force` replaces). All verbs auto-target the active VM wherever it
   lives. `KORA_HOME` overrides the state home
   (`~/Library/Application Support/kora` on macOS, `~/.local/share/kora` on
   Linux) — test harnesses point it at a scratch dir for isolated runs.
   Image caches and the tart store stay in the default home on purpose, so
   isolated runs never re-download multi-GB images.
2. **Local backends.** macOS (Apple Silicon): cirruslabs tart images for
   tahoe/sequoia/debian/ubuntu, and QEMU+hvf with the archboot unattended
   installer for arch. Linux: official cloud images (Debian 13, Ubuntu 24.04,
   Arch) as qcow2 backing files + a cloud-init NoCloud seed, under QEMU+KVM.
   QEMU lifecycles are PID-file managed; SSH is a per-VM ed25519 key.
3. **Cloud mode.** `kora new <os> --cloud` ensures a clouddevbox named `kvm`
   exists for the AWS profile (created `--kvm` with the amun `qemu` plugin,
   ~$0.10/h while running, autostop re-asserted to 12h), installs this exact
   kora file onto it, and proxies verbs there over `clouddevbox ssh`. The
   box-side state is the single source of truth: SSH into the box and run
   `kora status` — same machine. Tunnels and VNC compose through
   `clouddevbox tun`. `kora rm` removes the VM but keeps the box.
4. **Profile selection is delegated.** Without `--profile`, kora runs
   `clouddevbox profile` (bullet picker on a tty) and persists the answer in
   the VM state so every later call reuses it.
5. **Omarchy (cloud-only).** `kora new omarchy --cloud` boots the Omarchy ISO
   with an unattended cidata drive (user `gonzalo`, kora's per-VM key, **no
   LUKS**) and installs to disk on the `kvm` devbox at **m7i.xlarge**. Because
   Omarchy needs a known size and a pristine host, an existing `kvm` box is
   destroyed and recreated (confirmed unless `--yes`). The first install
   (~15-40 min) runs detached on the box and is snapshotted to a cached base;
   later `kora new omarchy` / `kora reset` are instant COW overlays off it.
   `kora vnc` tunnels to the Hyprland desktop.
6. **reset.** `kora reset` returns the VM to clean. Local: stop, wipe, restart.
   Cloud: resets the nested guest **on the box** — the EC2 devbox keeps
   running (never stopped). Fast for omarchy/cloud-image guests (base overlay);
   arch reinstalls.

## Supported platforms

| Host | Guests | Backend |
|---|---|---|
| macOS (Apple Silicon) | tahoe (macOS 26), sequoia (macOS 15), debian, ubuntu | tart (Virtualization.framework) |
| macOS (Apple Silicon) | arch | QEMU + hvf (archboot, aarch64) |
| Linux with /dev/kvm | debian, ubuntu, arch | QEMU + KVM (cloud image + cloud-init, x86_64) |
| cloud (`--cloud`) | debian, ubuntu, arch | QEMU + KVM on the `kvm` clouddevbox (m7i.large) |
| cloud only (`--cloud`) | omarchy | QEMU + KVM ISO install on the `kvm` clouddevbox (m7i.xlarge) |

Dependencies are binaries, never pip packages: `tart` + `sshpass`
(`brew install cirruslabs/cli/tart cirruslabs/cli/sshpass`), `qemu`
(`brew install qemu` / `apt-get install qemu-system-x86 genisoimage` / the
amun `qemu` plugin), `clouddevbox` (gear) for cloud mode.

## Testing

- `tests/` — offline unit tests, no VMs or network:
  `python3 -m venv /tmp/kora-test && /tmp/kora-test/bin/pip install -q pytest
  && /tmp/kora-test/bin/pytest -q tests/`
- `./e2e <cloud|local|both> [--yes] [--only <os>] [--profile <p>]
  [--destroy-box]` — boots every OS available on the chosen side and gates
  new/cmd/copy/tun/vnc/stop/start/rm. `local` is free (first runs download
  images; sequoia is tens of GB). `cloud` is **billable** and additionally
  asserts nested `/dev/kvm` inside the guest.

## Known quirks

- **SVE on Apple Silicon Linux guests**: Virtualization.framework advertises
  SVE the CPU doesn't implement; cryptography 47+/OpenSSL 4 SIGILLs on import
  (pyca/cryptography#14733). kora sets `arm64.nosve` and reboots tart Linux
  guests automatically at create.
- **archboot pinning**: the arch installer ISO and its embedded SSH key must
  come from the same release; when `latest` is mid-upload-inconsistent, pin
  `KORA_ARCHBOOT_RELEASE=YYYY.MM`. ISOs are cached per release.
- **tart `--hd`** can only grow the disk, and the guest filesystem may not
  auto-grow to match.
- **Cloud VM lifetimes**: the devbox autostops (12h from each boot). The VM
  disk survives — the next `kora start` boots the box, then the VM.
- **`kora rm` with the box stopped** clears local state only; the remote VM
  files remain until the next `kora new --cloud --force` on that box.
- **clouddevbox venv bootstrap** costs ~10-20 s per call; `python3 -m pip
  install --user boto3 bullet` makes clouddevbox invocations near-instant.
- **VNC**: tart guests get their URL from the tart log (`--vnc-experimental`);
  QEMU guests listen on loopback (`vnc://127.0.0.1:59xx`); cloud guests are
  reached through a held-open tunnel (`kora vnc` blocks until ctrl-c).
- **Omarchy cidata schema** is version-sensitive: `omarchy_user_configuration`
  is byte-matched to Omarchy 4's Configurator output (archinstall + Limine +
  btrfs). On an Omarchy version bump, re-capture it (the ISO's Configurator
  writes the exact JSON, or see `omacom/omarchy-iso`'s integration test); pin
  the ISO with `KORA_OMARCHY_VERSION`.
- **Omarchy shares the `kvm` box** but needs m7i.xlarge, so `kora new omarchy`
  destroys+recreates the box (confirmed unless `--yes`). A following amun
  cloud test finds it at xlarge (works, slightly more $) or recreates it at
  m7i.large — some churn is inherent to one shared box name.
- **Omarchy display**: renders on `virtio-vga` over VNC (Omarchy's own CI uses
  this). Its default is a 2× HiDPI scale; at 1080p over VNC adjust
  `~/.config/hypr/monitors.lua` if it looks zoomed.

## License

GPL-3.0. See LICENSE.

[clouddevbox]: https://github.com/GonzaloAlvarez/cn-cli-devbox
