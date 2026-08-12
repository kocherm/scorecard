# Scorecard TV appliance (Raspberry Pi kiosk)

A dedicated Raspberry Pi that boots straight into the Scorecard board on a TV.
No desktop, no login, no browser chrome, no keyboard. Power it on and it shows
the board; power it off and on and it shows the board again.

This directory is the **build kit** for producing those units. It is the
authoritative definition of the appliance - `user-data.example` provisions
everything. Do not hand-edit units on a running device and expect the change to
survive re-provisioning; change it here.

## The design in one idea

**Everything a customer must configure lives on the FAT boot partition.**

That partition mounts on any Windows, macOS, or Linux machine with an SD reader.
So a non-technical buyer configures a unit by editing two text files - no SSH,
no keyboard, no HDMI, no terminal. It also means a unit that has gone dark in
the field can be recovered by mailing a card, or by talking someone through a
file edit on their laptop.

| File on the boot partition | Sets |
| --- | --- |
| `wifi-credentials` | Wi-Fi SSID and passphrase |
| `scorecard-kiosk.conf` | Which board this unit displays (`KIOSK_URL`) |
| `network-config` | cloud-init's own network setup (first boot) |
| `meta-data` | `instance-id` - the re-provisioning lever |
| `user-data` | The whole appliance definition |

The second reason this matters is durability. The root filesystem is ext4, whose
journal protects *metadata, not file contents*. An unclean power loss - which is
every power loss, for a device with no shutdown button - can leave
NetworkManager's connection files zero-length. The unit then boots perfectly,
joins nothing, and displays `127.0.0.1`. Because it is headless and remote
access needs the network, that state is unrecoverable without physically
extracting the SD card. The boot partition is written to almost never, so it
survives; `wifi-selfheal.service` restores the profile from it on every boot.

This is not hypothetical - it is the failure that produced this kit.

## Bill of materials

| Part | Requirement | Why |
| --- | --- | --- |
| Raspberry Pi 4 (2 GB+) | 4 GB recommended | Pi 4 has the GLES3 the WPE renderer needs |
| Power supply | **5V / 3A USB-C**, its own wall outlet | See below - this is the most common field fault |
| SD card | A0/A1 rated, 16 GB+ | Cheap cards fail early under a 24/7 write load |
| micro-HDMI cable | 4K60-rated if driving 4K | Marginal cables flicker only at 4K |
| Case | Passive heatsink case | Fanless, since it sits behind a TV |

**Do not power a unit from the TV's USB port.** A Pi 4 is specified at 5V/3A; TV
USB ports supply 0.5A (USB 2.0) to 0.9A (USB 3.0). Units run this way appear
stable for weeks and then fail in ways that look like software: Wi-Fi dropping,
SD corruption, peripherals misbehaving while the CPU seems fine. It also ties
the Pi's power state to the TV's. Ship every unit with its own adapter.

**Mount for service.** Do not sandwich the Pi between the TV and the wall
bracket. Run a USB extension to a reachable point, and keep the SD slot
accessible. A unit whose ports require dismounting a wall-mounted TV converts a
two-minute fix into an hour with a ladder.

## Building a golden image (one-time)

1. Flash Raspberry Pi OS (Bookworm or later, 64-bit, **Lite** - no desktop) with
   Raspberry Pi Imager.
2. Mount the resulting boot partition and copy in the templates:

   ```bash
   cp user-data.example                /Volumes/bootfs/user-data
   cp network-config.example           /Volumes/bootfs/network-config
   cp meta-data.example                /Volumes/bootfs/meta-data
   cp wifi-credentials.example         /Volumes/bootfs/wifi-credentials
   cp scorecard-kiosk.conf.example     /Volumes/bootfs/scorecard-kiosk.conf
   ```

3. Replace the placeholders in `user-data`: `KIOSK_USER`, the `passwd` hash
   (`openssl passwd -6`), and your support SSH public key.
4. Append `consoleblank=0` to `cmdline.txt` so the console never blanks. Keep
   `cmdline.txt` on **one line** - a stray newline makes the Pi unbootable.
5. Boot it, let cloud-init finish (first boot is slow - it installs `cog`), then
   verify with the checklist below.
6. Power down, image the card back out (`dd`), and keep that as the golden
   image. Per-unit configuration is then only the boot-partition files.

Validate any edit to the templates before imaging - a YAML error in `user-data`
stops cloud-init, which means the unit never configures its network and cannot
be reached:

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('user-data.example')); print('OK')"
```

## Per-unit provisioning

For each unit, on the boot partition:

1. `wifi-credentials` - the customer's SSID and passphrase.
2. `scorecard-kiosk.conf` - `KIOSK_URL=https://their-scorecard.example.com/tv`.
3. `network-config` - the same SSID/passphrase (this is what first boot uses).
4. `meta-data` - a unique `instance-id`, e.g. `scorecard-kiosk-0042-r1`.
5. `cmdline.txt` - update the `ds=nocloud;i=<instance-id>` fragment to match
   `meta-data` exactly.

Steps 4 and 5 must agree. If they don't, cloud-init's behaviour depends on which
source wins and re-provisioning becomes unpredictable.

## Repointing or re-provisioning a unit

**Changing `network-config` or `user-data` on a provisioned unit does nothing on
its own.** cloud-init compares the `instance-id` against what it ran last boot,
and on a match it restores from cache and skips re-applying. This is the single
most confusing behaviour in the whole system, and it is what let one office unit
sit broken through three nightly reboots.

To force a full re-provision, bump the `instance-id` in **both** places:

```
meta-data:    instance-id: scorecard-kiosk-0042-r2
cmdline.txt:  ds=nocloud;i=scorecard-kiosk-0042-r2
```

Re-running is safe: the user, hostname, and SSH keys are idempotent, and host
keys are preserved (`ssh_deletekeys: false` in the base image).

Two changes do **not** need this, because nothing caches them:

- **Repointing the board** - edit `KIOSK_URL` in `scorecard-kiosk.conf` and
  reboot. The unit file reads it fresh at every start.
- **New Wi-Fi password** - edit `wifi-credentials`, delete the stale profile
  (`nmcli connection delete <name>`), and reboot; the self-heal rebuilds it.

## Read-only root (overlayfs)

For units going to customers who will yank power, consider enabling the overlay
filesystem: `raspi-config` → Performance Options → Overlay File System. Root
becomes read-only with writes going to a RAM overlay, so power loss cannot
corrupt or erase anything. The unit boots identically forever.

Understand the two costs before shipping it on by default:

- **Changes stop persisting.** Combined with the nightly reboot, anything
  written at runtime is gone by morning. Updates require toggling the overlay
  off, changing, and toggling back on.
- **It defeats persistent logging.** Journals land in the RAM overlay and vanish
  at reboot, which is precisely the situation that makes a field fault
  undiagnosable. If you ship overlayfs, either accept blind support or write
  logs somewhere durable.

The honest summary: overlayfs makes units nearly unbreakable and nearly
undebuggable. Decide deliberately, per SKU.

## Security notes

Tell customers these, or design around them, before selling units.

- **`/tv` is unauthenticated.** It looks up the display token server-side and
  redirects, so the device stores no credential - but equally, anyone who can
  reach the host can open `/tv` and read the board. Rotating the display token
  does not change this. If a customer's board is sensitive, restrict `/tv` at
  the reverse proxy (source IP allowlist) or keep the instance off the public
  internet.
- **The Wi-Fi passphrase is plain text on the boot partition.** Anyone holding
  the SD card gets the customer's Wi-Fi password. This is inherent to FAT (no
  permissions) and to needing a headless unit to self-heal. Where it matters,
  put units on a guest or IoT VLAN rather than the corporate SSID. Using a
  precomputed PSK instead does not help - a PSK is equally usable as a credential.
- **Your support SSH key is in the image.** Every unit built from one golden
  image trusts the same key. Treat that private key as fleet-wide credential
  material, and consider per-unit keys if the fleet gets large.

## Verification checklist

Run before a unit ships, and after any field repair. Do not skip the reboot -
it is the only thing that proves the configuration actually persists.

```bash
systemctl is-active scorecard-kiosk.service     # active
pgrep -a cog                                    # running, with the right URL
ip -br addr show wlan0                          # UP, with a real address
systemctl is-enabled wifi-selfheal.service      # enabled
journalctl -u wifi-selfheal.service -b          # ran; reports its decision
systemctl list-timers | grep kiosk              # watchdog + nightly reboot armed
journalctl --list-boots                         # MORE THAN ONE = logs persist
```

That last one is the easiest to get wrong. Raspberry Pi OS ships a drop-in
forcing `Storage=volatile`, and journald also needs `journalctl --flush` after
the restart or it keeps writing to `/run`. If only the current boot is listed,
persistent logging is not working, no matter what the config says.

## Field support runbook

| Symptom on screen | Cause | Fix |
| --- | --- | --- |
| Console, `My IP address is 127.0.0.1` | Wi-Fi profile lost (power cut) | Should self-heal at next boot. If not, the credentials file is wrong or missing. |
| Console, stops at `NetworkManager-wait-online` | No network | Wi-Fi credentials, AP down, or out of range. |
| Console, service waiting | Network fine, **server** unreachable | Check `KIOSK_URL` and that the Scorecard host is up. The wait loop is intentional. |
| Black screen, unit responds to ping | Renderer died, or `renderer=gles` missing | Watchdog restarts within 10 min; else `systemctl restart scorecard-kiosk`. |
| Board renders but is stale | Server-side, not the unit | The TV polls every 10s; check the app. |
| Boots to a desktop | `set-default multi-user.target` didn't apply | Re-provision; lightdm is fighting cog for DRM. |

**When a unit is unreachable and you have the SD card**, you can read its
filesystem without mounting it - useful on macOS, which cannot mount ext4:

```bash
brew install e2fsprogs
sudo /opt/homebrew/opt/e2fsprogs/sbin/dumpe2fs -h /dev/rdiskNs2   # health
sudo /opt/homebrew/opt/e2fsprogs/sbin/debugfs -R "cat /var/log/boot.log" /dev/rdiskNs2
sudo /opt/homebrew/opt/e2fsprogs/sbin/debugfs -R "ls -l /etc/netplan" /dev/rdiskNs2
```

Zero-length files under `/etc/netplan` or an empty
`/etc/NetworkManager/system-connections` is the signature of the lost-credential
failure.

> **Do not write to ext4 with `debugfs` if `dumpe2fs` reports `needs_recovery`
> in the feature flags.** The journal has unreplayed transactions and writing
> around it can cause the corruption you came to fix. Everything needed is
> reachable from the FAT partition instead; let the kernel replay the journal on
> the next boot.

## Known gaps before this is a product

Honest list of what selling units would still require:

- **No fleet management.** No inventory, no remote health check, no way to know
  a customer's screen went dark. Today that arrives as a phone call.
- **No update path.** Units never update themselves. Shipping a fix means a
  card, a site visit, or talking a customer through a file edit.
- **The office reference unit predates this kit.** It hard-codes its URL in the
  unit file rather than reading `scorecard-kiosk.conf`. Rebuild it from this kit
  before treating it as representative of what customers receive.
- **First boot needs internet** to install `cog`. A unit that first boots on a
  network without outbound access provisions incompletely. Pre-install `cog` in
  the golden image to remove this.
- **No enclosure, labelling, or serial scheme** beyond the `instance-id`
  convention suggested here.
