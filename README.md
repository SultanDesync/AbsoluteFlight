# Absolute Flight for No Man's Sky (alpha 0.2.0-alpha)

Your HOTAS, read 1:1 and fed straight into the ship's flight model. No vJoy, no Steam Input mapping,
no controller emulation. Optional Sultan flight model with atmosphere flight, manual landing,
ditching and crash damage.

**Public alpha:** open to anyone who wants to fly it. Expect rough edges, and please send a support bundle
with every report. Your feedback is what shapes the next builds.

**Made with AI:** the code was written by AI coding agents under my direction. I designed it, directed
the research and flew every feature. See [HOW-IT-WAS-MADE.md](HOW-IT-WAS-MADE.md) for who did what, and
what that means for you.

**Discord:** https://discord.gg/KtrhfPBpr is where testing happens: bug reports, feedback, shared tunings,
new builds and tester chat. See "Reporting a problem" below.

## What you need

- No Man's Sky on **Steam, build 180836** (Steam build id 25732212). On any other build the mod
  stays off and the game plays as normal.
- Windows 10 or 11, 64-bit.
- Nothing else: Python and everything the mod needs come inside the zip. There's no launcher and nothing to run.

Other builds, stores (GOG, Microsoft Store) and Linux/Steam Deck: see [PLATFORMS.md](PLATFORMS.md).

## Install

1. Open the game folder: in Steam, right-click **No Man's Sky > Manage > Browse local files**. It's the
   folder with `Binaries` and `GAMEDATA` in it.
2. Open the zip and drag both of its folders, **Binaries** and **GAMEDATA**, into the game folder. If
   Windows asks, choose to merge the folders.

That adds one file, `Binaries\winmm.dll`, and one folder, `GAMEDATA\MODS\AbsoluteFlight`. No game file is
changed.

- Multiplayer can stay on. Online play is less tested than single player: if something misbehaves online,
  try it with **Options > Network > Multiplayer: Off** and send a support bundle.
- Steam Input can stay on, but don't bind your stick or throttle to game controls through it.
  If the game also reads the HOTAS, the mod sees a conflict and hands those axes back to the game.

## Play

1. Start No Man's Sky from Steam as usual. The mod loads with the game.
2. Open **Options > Controls > Absolute Flight Setup** (at the bottom of the Controls page) and bind your
   stick axes and throttle under **Flight Axes**: select a binding, then move the axis.

If **Absolute Flight Setup** isn't at the bottom of the Controls page, the mod didn't load: see
"Reporting a problem".

The game's own mod list shows ABSOLUTEFLIGHT. Turning mods off there doesn't turn Absolute Flight off; to
play without it, rename `Binaries\winmm.dll` (for example to `winmm.dll.off`) and rename it back later.

## Update

1. Delete `GAMEDATA\MODS\AbsoluteFlight` in the game folder.
2. Install the new zip the same way. Your settings, bindings and tunings carry over: they live in
   `%LOCALAPPDATA%\AbsoluteHOTAS`, not in the game folder.

**Coming from a Play.bat build (alpha.21 or earlier):** install as above, then delete the folder you unzipped
that build into. Your settings carry over. Don't run the old Play.bat with the new install in place: it would
load the mod twice.

## What's been flown

Sultan's flight model has its own style for each ship class. Fighter, Explorer, Shuttle and Exotic have
been flown and tuned. **Hauler, Living ship, Solar and Sentinel interceptor are untested**: their styles
are first drafts, so your feedback there is the first anyone has flown them. Fly any class back to its shipped
style with **Reset class** on the Flight Tuning page.

**Corvette flight modeling is a work in progress.** It flies a first baseline while its style is still being
decided, so expect it to change between builds. Its autopilot hands control back to the game while engaged.
Manual landing doesn't work on the Corvette yet (its hull rests too high above the ground for touchdown to
trigger): land it the game's way, with **Atmosphere Flight > Manual landing** off.

## Tuning and sharing a flight model

Each ship class has named tunings. In **Absolute Flight Setup > Flight Tuning**, the **Tuning** row lists
**Sultan's** (built in, always there to go back to) and any tuning files for the class you're flying.

- Change values on Flight Tuning or Atmosphere Flight; the tuning shows **(edited)** until you save.
- **Save tuning** writes your changes back to the selected tuning's file. **Save as new tuning** writes a
  new `<Class>_Custom.json` and selects it.
- Tuning files live in `%LOCALAPPDATA%\AbsoluteHOTAS\flight-tunings`, named `<Class>_<Name>.json`, for
  example `Exotic_MyCustom.json`. Rename a file to rename the tuning.
- To share a tuning, post the file in **#tunings** on the Discord. To try someone else's, put their file
  in that folder and pick it on the Tuning row.

Class names for files: Hauler, Fighter, Explorer, Shuttle, Exotic, Living, Solar, Interceptor, Corvette.

## Known issues and coming soon

- **Take-off from your throttle is coming soon.** For now, take off with the game's own take-off control:
  bind a stick or throttle button to that key on the **Buttons** page (add a custom key) and hold it while
  landed. Check **Options > Controls** in game for which key that is.
- **Manual landing is experimental.** With Sultan atmosphere flight, lowering the landing gear switches to
  a hover mode and the mod presses the game's Land for you near the ground. Expect rough touchdowns. Turn it
  off under **Atmosphere Flight > Manual landing** to land the game's way.
- **The game's own boost still works** and takes over from Sultan's flight model while you use it. For
  Sultan's boost, bind **Sultan boost** on the Buttons page (and turn boost on in the class's Flight Tuning).
- **Flight actions only work while you fly.** Sultan boost, parking brake, landing gear, trim and eject
  do nothing on foot, landed, or in menus. Keyboard and mouse bindings work everywhere.
- **Bindings pause in the Absolute Flight menu** and while a binding is being recorded.
- **Corvette flight modeling is a work in progress**, and four ship classes are untested (see "What's been flown").

## Reporting a problem and giving feedback

Join the Discord at https://discord.gg/KtrhfPBpr. Once you have the Tester role:

- **Bugs** go in **#bug-reports**, one post per problem:
  1. In game: **Options > Controls > Absolute Flight Setup > Save support bundle**.
  2. Attach the newest zip from `%LOCALAPPDATA%\AbsoluteHOTAS\support` (paste that into Explorer's
     address bar) with a few lines on what you were doing.
- **Flight feel, ideas and menu thoughts** go in **#feedback**. Say which ship class and where (space,
  atmosphere, landing).
- **The mod didn't load** (no Absolute Flight Setup on the Controls page): post in **#bug-reports** with
  `proxy.log`, `python-boot.log` and the newest `pymhf-*.log` from `%LOCALAPPDATA%\AbsoluteHOTAS\logs`.
  If that folder has no `proxy.log`, the game never loaded `Binaries\winmm.dll`: check it's in the right folder.
- **Questions and general chat** go in **#af-chat**. New builds are posted in **#builds**.

Your settings and logs live in `%LOCALAPPDATA%\AbsoluteHOTAS`. The mod sends nothing anywhere; a
support bundle only leaves your PC when you post it.

## Uninstall

1. In the game folder, delete `Binaries\winmm.dll` and `GAMEDATA\MODS\AbsoluteFlight`.
2. To remove your settings, bindings, tunings and logs too, delete `%LOCALAPPDATA%\AbsoluteHOTAS`.

Deleting `winmm.dll` alone already turns the mod off. Absolute Flight never changes the game's own files.

---

Absolute Flight is MIT licensed (see [LICENSE](LICENSE)). The Python runtime, packages and Rust crates it
ships keep their own licences: see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

Absolute Flight is an unofficial fan mod, not made, endorsed or supported by Hello Games. No Man's Sky
is a trademark of Hello Games.
