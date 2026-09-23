# Telegram moderator on NixOS

This module deploys the bot source in this Git checkout. It uses a system service,
requires no interactive login or user linger, and preserves runtime state separately
from the immutable source. Nix is not installed on the preparation machine: the
module has NOT been evaluated, built or boot-tested on NixOS. The four direct Python
dependency versions match requirements.txt; transitive dependencies and Python come
from your nixpkgs revision. Use a recent nixpkgs and pin it with your host's flake.lock.

## Install

1. Clone this repository to /etc/nixos/telegram-moderator.
2. Create the credential file OUTSIDE the Nix source tree:

   sudo install -m 600 -o root -g root /etc/nixos/telegram-moderator/.env.example /etc/telegram-moderator.env
   sudoedit /etc/telegram-moderator.env

   Fill in Telegram/model credentials, group IDs, proxies, and moderation policy.
   For migration use your existing .env instead of the example, keeping mode 0600.
   Do not commit the real file or use builtins.readFile on it in Nix: LoadCredential
   reads the runtime path and supplies it privately to the service. The application
   still parses dotenv itself, so its original quoting and literal dollar handling
   are preserved. DATA_DIR is set by the service and overrides a value in .env.

3. Add this entry to the existing imports list in configuration.nix:

   ./telegram-moderator/telegram-moderator.nix

   If your NixOS flake is Git-backed, git add the new module, python-env.nix and
   bot source files so the flake can see them. Do not add credentials.

4. Build first:

   sudo nixos-rebuild build

   Add your usual --flake path#host argument if applicable. Stop any old bot
   instance and transfer data if necessary BEFORE activating the new service.

5. Activate:

   sudo nixos-rebuild switch
   systemctl status telegram-moderator
   sudo journalctl -u telegram-moderator -n 100 -f

   switch starts the wanted system service; no systemctl enable or linger is needed.
   Check the bot's ready-group log, not only systemd's active state.

## Existing data

Stop the old instance before copying its data, including telegram.session and
moderation.sqlite3 and any associated -wal / -shm files. Copy the CONTENTS of its
data directory into /var/lib/telegram-moderator before the first activation.
StateDirectory creates and assigns the directory to the service. With DynamicUser,
systemd may manage the real directory under /var/lib/private and expose the public
path through a symlink. Do not run two instances using the same bot/session.
For an impermanent root filesystem, explicitly persist the real state directory
according to your impermanence setup; ordinary StateDirectory does not override
filesystem resets.

For the initial migration, after stopping both instances and copying the data,
normalize ownership and modes before starting the new service:

```sh
sudo chown -R root:root /var/lib/telegram-moderator/
sudo find -H /var/lib/telegram-moderator -type d -exec chmod 0700 {} +
sudo find -H /var/lib/telegram-moderator -type f -exec chmod 0600 {} +
```

Systemd assigns ownership for the service at startup. Keep the top-level directory
root-owned before the migration's first start so existing copied files are included
in ownership adjustment. Do not change ownership while the service is running.
The credential source stays root:root 0600; LoadCredential handles service access.

## Operations

sudo systemctl restart telegram-moderator  # after editing the runtime .env
sudo systemctl stop telegram-moderator
sudo systemctl start telegram-moderator
sudo journalctl -u telegram-moderator -b

Source/module changes take effect through nixos-rebuild switch. Removing the import
and rebuilding removes the service. Journald retention across boots follows the
host's journald configuration. If desired, configure services.journald.storage =
"persistent" in the host configuration and set retention limits there.

No inbound port is required. Outbound Telegram/model access is required. Proxy
addresses copied from a desktop .env must refer to a proxy reachable on this host.
A proxy available only in a logged-in desktop session will not provide unattended
boot-time access.

This service supervises process uptime only; it does not add a moderation circuit
breaker or change the bot's current ban policy.

## Telethon 1.45.0 packaging fix

The upstream sdist declares a custom Hatch hook but omits hatch_build.py. The
Telethon expression now installs the official 1.45.0 py3-none-any wheel, with its
verified source digest, instead of trying to rebuild that broken sdist. Only the
Telethon derivation changed; keep the service module, credentials and data intact.
Replace python-env.nix in your NixOS configuration and rebuild. This does not
require downgrading Python or deleting any session/database files.
