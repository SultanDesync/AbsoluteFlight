# Absolute Flight for No Man's Sky (alpha 0.1.0-alpha.3)

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
3. In game, open **Options > Controls > HOTAS Flight Setup** (at the bottom of the Controls page) and
   bind your axes under **Flight Axes** and **Throttle**: select a binding, then move the axis.

To play without the mod, start the game from Steam as usual.

## Reporting a problem

1. In game: **Options > Controls > HOTAS Flight Setup > Save support bundle**.
2. Send the newest zip from `%LOCALAPPDATA%\AbsoluteHOTAS\support` (paste that into Explorer's
   address bar) with a few lines on what you were doing.

Your settings and logs live in `%LOCALAPPDATA%\AbsoluteHOTAS`. Nothing is sent anywhere.

## Uninstall

Delete this folder and `%LOCALAPPDATA%\AbsoluteHOTAS`. The mod never changes game files.
