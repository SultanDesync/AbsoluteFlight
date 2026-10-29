# Other game versions and platforms

Absolute Flight is built and tested on **No Man's Sky for Steam on Windows, build 183877**
(Steam build id 26295378). Everything below is outside that: it may work, but there's no guarantee.
Reports in **#feedback** on the Discord (https://discord.gg/jjk8nEMzFK), with a support bundle, are how
these get supported.

## Other game builds and stores (GOG, Microsoft Store, Game Pass)

For now the mod only turns on for the Steam build above. On any other build or store it stays off and
the game plays as normal, so after a game update you can keep playing while a new release catches up.

Support for other builds and stores is planned: the mod will find the game code it needs by its
contents, not its location, and turn on each feature it can confirm.

## Linux and Steam Deck (Proton): unsupported, but working on a Steam Deck

Not officially supported and not guaranteed. A Steam Deck (Proton 11.0) runs 0.2.3-alpha with the Linux
compatibility patch `AbsoluteFlight-Proton.asi` and these launch options:

`PROTON_ENABLE_HIDRAW=1 WINEDLLOVERRIDES="winmm=n,b" %command%`

Full setup, what each part does and what to send if it fails: [LINUX.md](LINUX.md).
