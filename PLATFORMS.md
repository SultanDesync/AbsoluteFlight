# Other game versions and platforms

Absolute Flight is built and tested on **No Man's Sky for Steam on Windows, build 180836**
(Steam build id 25732212). Everything below is outside that: it may work, but there's no guarantee.
Reports in **#feedback** on the Discord (https://discord.gg/KtrhfPBpr), with a support bundle, are how
these get supported.

## Other game builds and stores (GOG, Microsoft Store, Game Pass)

For now the mod only turns on for the Steam build above. On any other build or store it stays off and
the game plays as normal, so after a game update you can keep playing while a new release catches up.

Support for other builds and stores is planned: the mod will find the game code it needs by its
contents, not its location, and turn on each feature it can confirm.

## Linux and Steam Deck (Proton): unsupported, but likely to work

Absolute Flight isn't tested on Linux. Players have run the same author's Starfield HOTAS mod under
Proton without much trouble.

- Install as the README says (the zip's two folders into the game folder).
- In Steam, right-click No Man's Sky > **Properties** > **Launch Options**, and enter:

  `WINEDLLOVERRIDES="winmm=n,b" %command%`

  Without it, Proton uses its own winmm.dll, so the mod doesn't load and the game plays as normal.
- If an axis or buttons are missing when you bind them, add `PROTON_ENABLE_HIDRAW=1` before `%command%`.
  It lets the game read your stick and throttle directly.
- Bindings made on Windows won't carry over (devices get new IDs under Proton): bind again in the menu.
- Don't bind the stick or throttle to game controls through Steam Input.
