# How Absolute Flight was made

I'm not a programmer. Absolute Flight's code was written by AI coding agents: Anthropic's Claude and
OpenAI's Codex, plus one fix from Google's Gemini for a problem the other two couldn't solve. I decided
what it should be and how it should feel, directed the research, approved every test that touched the
running game, and flew every feature before it was called done.

This page is drawn from the project's own records: its decision log, task board, commit history, flight
logs and my playtime. They're summarized here rather than quoted, since the detail is mostly bookkeeping.

## What "AI-written" means now

A year ago I built a HOTAS mod for Starfield the same way, with the AI tools of the time, and that project
says so. It was slow and hands-on. I read a lot of the code myself and kept every file under 300 lines,
because past that the AI's edit tools cut files off or the logic got too dense for it to follow. Every
change went through review, and I learned to throw away a session that called its work good enough and
start a fresh one to rewrite it. I also spent hours in the game's memory by hand, running sequences to
watch the bits flip and placing the breakpoints myself.

This project was different. The agents took Absolute Flight from an empty folder to a shippable alpha in
nine days, about 88 hours of active work, with far more features than the Starfield mod had. I didn't set
file limits or read the code. My job was direction, judgment and flying.

So yes, it's AI-written, and anything that patches your game deserves care. If nothing here reassures
you, don't install it. But if the reason is "AI can't write clean code" or "AI can't hold a real project
together", check whether that's still true. A year ago I'd have said the same, and I was working around
it every day. The source is in the download if you want to judge it yourself.

## Who did what

**Me**
- **The idea and the rule it's built on:** your HOTAS read 1:1 and fed straight into the flight model,
  with no controller emulation. That comes from years of flying space sims on a HOTAS and streaming about
  space sim controls as SultanDesync.
- **The foundation.** The project started with the input bus, the part that owns your devices,
  calibration and bindings. Its design comes from the Starfield mod. I also specced the research harness
  the agents used to run scripted tests in the game.
- **Research direction.** When the agents stopped at a partial answer, I sent them back to dig deeper
  into how the game's flight model and menus actually work.
- **Design and feel.** Most of the big decisions came from me, and so did the headline features: the
  Sultan flight model with its parking brake, atmosphere flight, manual landing, ditching and crash
  damage, the ship computer's voice lines. I rejected work that ran but didn't fit, like the wrong font or
  a menu item in the wrong place.
- **Safety calls.** Every first change to the running game waited for my approval, with limits on how
  long and how far it could go.
- **Test pilot.** A feature was done when I flew it on my own sticks and accepted the feel. Bugs I hit
  in flight drove many of the fixes.
- **Running the project:** passing work between the agents, deciding what went into a build, deciding
  when to release.

**The agents**
- All of the code, the tests, and the reverse engineering of No Man's Sky's flight code.
- Nearly all of the writing. Most of it is notes they keep for themselves, because an agent starts each
  session remembering nothing. Very little of it is meant for players.

## What it took

- **About a week back in the game first,** relearning its mechanics and working out what I wanted, on
  top of about 350 hours already played and every achievement.
- **Nine long days.** The agents worked most of the hours I was awake, and I was steering them: reading
  their reports, answering their questions, approving tests, rejecting results.
- **About a day of flight testing,** spread across the week, much of it re-flying a feature after a fix.

## Where your experience comes in

The agents can write the code and dig through a game's memory faster than I could. What they can't do:

- **Know what right feels like.** They can't fly. Every judgment about feel was mine.
- **Know the game.** Hundreds of hours let me tell when a mechanic was wrong, even when the agents'
  numbers said it was fine.
- **Know when an answer is shallow.** Having searched memory by hand before, I knew roughly what was
  possible and when to push for more.
- **Pick the foundation.** Earlier projects taught me which structure lasts. Starting with the right one
  saved a rebuild later.
- **Say no.** The agents will build what you ask, and sometimes more: left alone they grow process and
  paperwork, and I had to rein that in. Something that works but doesn't fit is still wrong.
- **Make the safety calls.** Deciding what is allowed to touch a live game, and for how long, stays with you.

## What this means if you install it

- The mod patches No Man's Sky's code in memory while it runs. It never changes game files, and it stays
  off on any game build it wasn't made for.
- It has been flown by one pilot, on one PC, with one set of hardware. Some ship classes haven't been
  flown at all.
- Nobody has read the code line by line. The agents test a lot, but your support bundles are how the
  rest of the bugs get found.

## No Man's Sky and Hello Games

Absolute Flight is an unofficial fan mod. It isn't made, endorsed or supported by Hello Games, so please
bring problems with it to our Discord, not to them.

Working this deep inside No Man's Sky left me with a lot of respect for how it's built. Much of what the
mod does borrows systems the game already has: the G-force blackout uses the game's own screen effects,
manual landing hands touchdown to the game's Land, and ditching and crashes use its own eject, ship
damage and repair. Each ship class gets its handling from its own tuning values, and the menus took a
new page that looks like it was always there. Working in it, it almost feels like any modification is
possible. I don't think the game started out that way. Hello Games has spent ten years since launch
adding feature after feature, and my guess is that all those updates shaped a game that's easy to keep
building on, which is what made a mod like this possible. Thank you to them.

## Built on

- **NMS.py and pyMHF** by monkeyman192, the framework the mod runs on.
- **OpenTrack** and **Tobii** for head tracking.
- **MBINCompiler** and **HGPAKtool**, used during research.
