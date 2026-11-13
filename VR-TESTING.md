# Absolute Flight in VR: tester checklist

VR support is experimental and has had very little flying. This checklist takes about ten minutes in the
headset, and steps can be skipped. **Stop at any point if you feel unwell**; a partial report is still useful.

What works so far: flying the ship on your stick and throttle (Sultan and Vanilla), the Absolute Flight menu, and
the pulse drive on a button. What is known not to work yet: the mod's own HUD does not draw in VR, and buttons that
send a keyboard key or mouse click do nothing, because the game takes no keyboard or mouse in VR. Firing and
launching from a button did not work in the first tests. This build has two changes for that which nobody has
flown yet: while you pilot, the game is told to take its input as on a flat screen instead of waiting for the VR
hand controllers (Flight Model > Flat-screen input in flight, on by default once Experimental VR is on). That is
what we need you for.

## Before you put the headset on

You can do all of this in flat screen first. Settings are shared between flat and VR.

1. Bind your stick and throttle axes as usual (Absolute Flight > Flight Axes).
2. Absolute Flight > Flight Model > turn **Experimental VR** on.
3. Absolute Flight > Buttons: leave **Button output** on **Game actions**, and bind a button to each of:
   Fire weapon, Launch (take off), Pulse drive, Boost, and Recentre VR view.
4. Park somewhere you can take off from, in your ship or next to it, and save.

## In the headset

Start the game from Steam with **No Man's Sky VR**. Leave the VR hand controllers on the desk once you are seated
in the ship; note it if you had to pick them up for anything.

| Step | Do this | What we want to know |
|---|---|---|
| 1 | Sit in the ship. Press your Launch button. | Did it take off? If not, take off the game's own way and carry on. |
| 2 | Move each axis for a few seconds: pitch, yaw, roll, throttle up and down, strafe if you have it bound. | Does the ship follow the stick and throttle? Anything reversed, dead or fighting you? |
| 3 | Hold Fire weapon for three seconds. | Did the weapon fire? |
| 4 | Press Boost, then Pulse drive (pulse needs open space). Open the quick menu from your button if you have one bound. | Did each one work? What did the quick menu look like, and could you work it from the stick? |
| 5 | Turn your head to one side and hold Recentre VR view until the view jumps back. | Did it recentre? Roughly how long did you hold it? |
| 6 | Pick up the right hand controller, grab the in-world flight stick for five seconds, let go. Then the same with the left hand on the in-world throttle. Put the controllers down again. | Nothing to answer. The log records how the game marks "hands on the controls", which is what its weapons seem to wait for. |
| 7 | Pause, open **Absolute Flight**, choose **Save VR test report**. | Nothing to answer. It writes the zip. |

Then take the headset off.

## What to send

1. The newest `vr-test-....zip` from `%LOCALAPPDATA%\AbsoluteHOTAS\support` (paste that path into Explorer's
   address bar). It holds your settings, this session's logs and flight telemetry, and a short summary of what
   the mod saw. Nothing is sent anywhere by the mod; you choose to share the file.
2. Your answers. Copy this and fill it in:

```
Headset and controllers:
Flight model (Sultan / Vanilla / Vanilla + strafe):
1 Launch button:      worked / did nothing
2 Stick and throttle: fine / problem:
3 Fire:               worked / did nothing
4 Boost:              worked / did nothing      Pulse: worked / did nothing      Quick menu:
5 Recentre:           worked / did nothing      held for about:
6 Grabbed the in-world stick and throttle: yes / no
Hand controllers needed for:
Anything else:
```

Post both in the testers' channel on the Discord.

## If something goes wrong

- The ship does not respond to the stick at all: check Experimental VR is on. The Absolute Flight menu says so at
  the top when it is off.
- Flying worked but something else got worse (a menu, the quick menu, getting out of the ship): turn
  **Flat-screen input in flight** off on the Flight Model page, try again, and tell us which it was.
- Turn Experimental VR off and the game flies exactly as it does without the mod's VR changes.
- The view goes black or stutters badly: take the headset off and quit. Tell us what you were doing.
