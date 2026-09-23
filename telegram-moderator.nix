{ lib, pkgs, ... }:
let
  python = import ./python-env.nix { inherit pkgs; };
  source = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./bot.py
      (lib.fileset.fileFilter (file: file.hasExt "py") ./moderator)
    ];
  };
in
{
  systemd.services.telegram-moderator = {
    description = "Telegram moderation bot";
    wantedBy = [ "multi-user.target" ];
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];
    startLimitIntervalSec = 0;
    environment = {
      DATA_DIR = "/var/lib/telegram-moderator";
      PYTHONDONTWRITEBYTECODE = "1";
    };
    serviceConfig = {
      Type = "simple";
      DynamicUser = true;
      StateDirectory = "telegram-moderator";
      StateDirectoryMode = "0700";
      WorkingDirectory = "/var/lib/telegram-moderator";
      # This is a runtime path string, never a Nix path containing secrets.
      LoadCredential = "env:/etc/telegram-moderator.env";
      ExecStart = "${python}/bin/python -u ${source}/bot.py --env %d/env";
      Restart = "always";
      RestartSec = "15s";
      KillSignal = "SIGINT";
      TimeoutStopSec = "30s";
      UMask = "0077";
      StandardOutput = "journal";
      StandardError = "journal";
      SyslogIdentifier = "telegram-moderator";
    };
  };
}
