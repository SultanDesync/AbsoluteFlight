# Other game versions and platforms

Absolute Flight is built and tested on **No Man's Sky for Steam on Windows, build 185034**
(Steam build id 26540391). Everything below is outside that: it may work, but there's no guarantee.
Reports in **#feedback** on the Discord (https://discord.gg/jjk8nEMzFK), with a support bundle, are how
these get supported.

## Other game builds and stores (GOG, Microsoft Store, Game Pass)

For now the mod only turns on for the Steam build above. On any other build or store it stays off and
the game plays as normal, so after a game update you can keep playing while a new release catches up.

Support for other builds and stores is planned: the mod will find the game code it needs by its
contents, not its location, and turn on each feature it can confirm.

## Linux and Steam Deck (Proton): unsupported, but likely to work

Absolute Flight isn't supported on Linux, and there's no guarantee it works. What follows is a starting
place: one Steam Deck (Proton 11.0, 0.2.3-alpha) loaded the mod and flew pitch and yaw from a VKB stick
this way. Players have also run the same author's Starfield HOTAS mod under Proton without much trouble.

- Install as the README says (the zip's two folders into the game folder).
- In Steam, right-click No Man's Sky > **Properties** > **Launch Options**, and enter:

  `PROTON_ENABLE_HIDRAW=1 WINEDLLOVERRIDES="winmm=n,b" %command%`

  Without the `winmm` part, Proton uses its own winmm.dll, so the mod doesn't load and the game plays as
  normal. `PROTON_ENABLE_HIDRAW=1` lets the game read your stick and throttle directly.
- Copy `AbsoluteFlight-Proton.asi` (the Linux compatibility patch, in the zip at
  `GAMEDATA/MODS/AbsoluteFlight/linux/`) into `No Man's Sky/Binaries/`, next to `winmm.dll`. Under Proton the game takes the memory next to NMS.exe early, and without the patch the mod
  often can't install its hooks or its flight features (they fall back to the game's own). The patch holds
  that room for Absolute Flight. It does nothing on Windows; delete the file to remove it.
- If Absolute Flight is still missing from the Options menu, quit and start the game again, and send the
  `proton-loader.log` from the logs folder below.
- Bindings made on Windows won't carry over (devices get new IDs under Proton): bind again in the menu.
- The device list may show "Controller (XBOX 360 For Windows)". That's Steam Input's virtual pad, not
  your stick: don't bind flight axes to it.
- Don't bind the stick or throttle to game controls through Steam Input.
- Logs and support bundles are in the game's Proton prefix:
  `steamapps/compatdata/275850/pfx/drive_c/users/steamuser/AppData/Local/AbsoluteHOTAS/`.
