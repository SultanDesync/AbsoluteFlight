# Absolute Flight for No Man's Sky (alpha 0.1.0-alpha.9)

Your HOTAS, read 1:1 and fed straight into the ship's flight model. No vJoy, no Steam Input mapping,
no controller emulation. Optional Sultan flight model with atmosphere flight, manual landing,
ditching and crash damage.

**Alpha:** for invited testers. Expect rough edges, and please send a support bundle with every report.

## What you need

- No Man's Sky on **Steam, build 180836** (Steam build id 25732212). On any other build the mod
  stays off and the game plays as normal.
- Windows 10 or 11, 64-bit.
- Nothing else: Python and everything the mod needs are inside this folder.

## Install

1. Unzip this folder anywhere **outside** OneDrive or other synced folders, for example `C:\Games\AbsoluteFlight`.
2. In No Man's Sky: **Options > Network > Multiplayer: Off.**
3. Steam Input can stay on, but don't bind your stick or throttle to game controls through it.
   If the game also reads the HOTAS, the mod sees a conflict and hands those axes back to the game.

## Play

1. Start Steam.
2. Double-click **Play.bat**. A console window opens, Steam starts the game, and the mod loads while
   the game starts. Leave the console open while you play; closing it unloads the mod.
3. In game, open **Options > Controls > Absolute Flight Setup** (at the bottom of the Controls page) and
   bind your axes under **Flight Axes** and **Throttle**: select a binding, then move the axis.

To play without the mod, start the game from Steam as usual.

## What's been flown

Sultan's flight model has its own style for each ship class. Fighter, Explorer, Shuttle and Exotic have
been flown and tuned. **Hauler, Living ship, Solar and Sentinel interceptor are untested**: their styles
are first drafts, so your feedback there is the first anyone has flown them. The Corvette uses the game's
own flight for now. Fly any class back to its shipped style with **Reset class** on the Flight Tuning page.

## Tuning and sharing a flight model

Each ship class has named tunings. In **Absolute Flight Setup > Flight Tuning**, the **Tuning** row lists
**Sultan's** (built in, always there to go back to) and any tuning files for the class you're flying.

- Change values on Flight Tuning or Atmosphere Flight; the tuning shows **(edited)** until you save.
- **Save tuning** writes your changes back to the selected tuning's file. **Save as new tuning** writes a
  new `<Class>_Custom.json` and selects it.
- Tuning files live in `%LOCALAPPDATA%\AbsoluteHOTAS\flight-tunings`, named `<Class>_<Name>.json`, for
  example `Exotic_MyCustom.json`. Rename a file to rename the tuning.
- To share a tuning, send the file. To try someone else's, put their file in that folder and pick it on
  the Tuning row.

Class names for files: Hauler, Fighter, Explorer, Shuttle, Exotic, Living, Solar, Interceptor, Corvette.

## Known issues and coming soon

- **Take-off from your throttle is coming soon.** For now, take off with the game's own take-off control:
  bind a stick or throttle button to that key on the **Buttons** page (add a custom key) and hold it while
  landed. Check **Options > Controls** in game for which key that is.
- **Manual landing is experimental.** With Sultan atmosphere flight, lowering the landing gear switches to
  a hover mode and the mod presses the game's Land for you near the ground. Expect rough touchdowns. Turn it
  off under **Atmosphere Flight > Manual landing** to land the game's way.
- **Flight actions only work while you fly.** Sultan boost, parking brake, landing gear, trim and eject
  do nothing on foot, landed, or in menus. Keyboard and mouse bindings work everywhere.
- **Bindings pause in the Absolute Flight menu** and while a binding is being recorded.
- **Four ship classes are untested** (see "What's been flown"), and the Corvette uses the game's own flight.

## Reporting a problem

1. In game: **Options > Controls > Absolute Flight Setup > Save support bundle**.
2. Send the newest zip from `%LOCALAPPDATA%\AbsoluteHOTAS\support` (paste that into Explorer's
   address bar) with a few lines on what you were doing.

Your settings and logs live in `%LOCALAPPDATA%\AbsoluteHOTAS`. Nothing is sent anywhere.

## Uninstall

Delete this folder and `%LOCALAPPDATA%\AbsoluteHOTAS`. The mod never changes game files.
