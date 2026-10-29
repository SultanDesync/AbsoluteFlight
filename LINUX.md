# Absolute Flight on Linux and Steam Deck (Proton)

Linux isn't officially supported, and there's no guarantee it works on your system. This is the setup
that worked on a Steam Deck (SteamOS, Proton 11.0) with Absolute Flight 0.2.3-alpha: all hooks
installed, the menu worked, and the stick flew the ship. Reports go in **#feedback** on the Discord
(https://discord.gg/jjk8nEMzFK).

## What you need

- No Man's Sky on Steam, the build the release supports (0.2.3-alpha: **183877**).
- The **0.2.3-alpha** release zip: https://github.com/SultanDesync/AbsoluteFlight/releases/tag/v0.2.3-alpha
- The Linux compatibility patch, **`AbsoluteFlight-Proton.asi`**, from this branch:
  https://github.com/SultanDesync/AbsoluteFlight/raw/linux/linux/AbsoluteFlight-Proton.asi
  (SHA256 in [linux/AbsoluteFlight-Proton.asi.sha256](linux/AbsoluteFlight-Proton.asi.sha256)).

## Why the patch

Under Proton the game takes the memory right next to `NMS.exe` early in startup. Without the patch,
Absolute Flight often can't install its hooks there: the HOTAS entry is missing from the menu or does
nothing, and the log shows `MEMORY ALLOC (9) Failed to initialize hook ...`. Even when the hooks get in,
its flight features (game actions, Newtonian, crash damage and the rest) fall back to the game's own.
The patch sets that memory aside for Absolute Flight before the game starts. It does nothing on Windows.
Its source is in [proton_loader/](proton_loader/).

## Install

1. Find the game folder: in Steam, right-click No Man's Sky > **Manage** > **Browse local files**
   (usually `~/.local/share/Steam/steamapps/common/No Man's Sky`).
2. Extract the 0.2.3-alpha zip into that folder, as the README says. You get `Binaries/winmm.dll` and
   `GAMEDATA/MODS/AbsoluteFlight/`.
3. Copy `AbsoluteFlight-Proton.asi` into `Binaries/`, next to `winmm.dll`.
4. In Steam, right-click No Man's Sky > **Properties** > **General** > **Launch Options**, and enter:

   ```
   PROTON_ENABLE_HIDRAW=1 WINEDLLOVERRIDES="winmm=n,b" %command%
   ```

   - `WINEDLLOVERRIDES="winmm=n,b"` makes Proton load the mod's `winmm.dll`. Without it, the mod doesn't
     load and the game plays as normal.
   - `PROTON_ENABLE_HIDRAW=1` lets the game read your stick and throttle directly.
5. Plug in your stick and throttle, then start the game. Open the pause menu > **Options**; Absolute
   Flight's pages are there. Bind your axes and buttons.

## Good to know

- Bindings made on Windows don't carry over (devices get new IDs under Proton): bind again.
- The device list may show **"Controller (XBOX 360 For Windows)"**. That's Steam Input's virtual pad, not
  your stick: don't bind flight axes to it.
- Don't map the stick or throttle to game controls through Steam Input.

## If it doesn't work

Logs and support bundles are in the game's Proton prefix:
`~/.local/share/Steam/steamapps/compatdata/275850/pfx/drive_c/users/steamuser/AppData/Local/AbsoluteHOTAS/logs/`

| File | What a working start shows |
|---|---|
| `proxy.log` | `asi ...AbsoluteFlight-Proton.asi: loaded` |
| `proton-loader.log` | `reserved 64 spare granules below it and 64 above`, `_ctypes.pyd loaded: 1 of 1 imports redirected`, `_cyminhook... 3 of 3 imports redirected` |
| `pymhf-*.log` | `Loaded 3 mods and 36 hooks` |

- **No logs folder at all:** the launch options are missing or mistyped (check the `winmm=n,b` part).
- **No `proton-loader.log`:** the `.asi` isn't in `Binaries/` next to `winmm.dll`.
- **Still `MEMORY ALLOC` errors:** quit, start again, and post the four files above with your Proton
  version and distro (or Steam Deck) in #feedback.

## Removing it

Delete `Binaries/AbsoluteFlight-Proton.asi`. To remove Absolute Flight as well, follow the README and
clear the launch options.
