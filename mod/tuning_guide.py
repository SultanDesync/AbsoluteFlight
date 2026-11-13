"""What each Flight Tuning and Atmosphere Flight setting does to the ship's performance (T122; user 2026-11-13:
"settings in the flight tuning menu need per option description of how they affect performance", then "Yes,
Atmosphere next").

These are the settings' help text: the game's own description panel shows paragraph(id) for the highlighted row
(hotas_menu.row_help). A side pane showed them first; in game it covered that panel and repeated it (user
2026-11-13: "they overlap existing descriptions"), so the text moved into the panel. Each entry: (what it is,
((what you do, what the ship does), ...), a note or None). Written from the flight law itself
(newtonian_flight.compute_acceleration, newtonian_runtime's boost heat, additive_rotation, planetary_flight); keep
them in step.
"""

_AUTHORITY_NOTE='Thrust this way = Acceleration scale x this. 1.00 is the full thrust of the engines.'
_RATE_NOTE=("1.00 is the game's own rate. The game's torque limits still apply, so a higher rate can take longer to reach: "
            'raise Turn response with it. Centre the stick after changing.')

GUIDE={
    'tuning-acceleration-scale':(
        "How hard the engines push, in every direction. It multiplies the ship's own thrust, so its class bonus and upgrades still count.",
        (('HIGHER','builds speed sooner, stops sooner and strafes harder.'),
         ('LOWER','a heavier ship: slow to build speed and slow to stop.')),
        "It does not move top speed: that is the Speed cap in space and the Terminal speed in thick air, where drag is matched to the ship's "
        'thrust. The authority settings below split this thrust by direction.'),
    'tuning-forward-ratio':(
        'The share of thrust for pushing forward.',
        (('HIGHER','reaches top speed sooner.'),('LOWER','a longer climb to the same top speed.')),
        _AUTHORITY_NOTE+" Momentum's limits are measured from this one."),
    'tuning-retro-ratio':(
        'The share of thrust for pushing backward: slowing down with the throttle pulled back, and reversing.',
        (('HIGHER','stops in less distance without turning round. At the same value as Forward authority the ship brakes as hard as it accelerates.'),
         ('LOWER','a long slow-down nose first. To stop quickly you turn round and burn.')),
        _AUTHORITY_NOTE+' The parking brake is limited by it too.'),
    'tuning-lateral-ratio':(
        'The share of thrust for strafing left and right.',
        (('HIGHER','sidesteps faster and cancels sideways drift sooner.'),
         ('LOWER','the ship slides: a sideways drift takes longer to stop, and orbiting a target is slower.')),
        _AUTHORITY_NOTE),
    'tuning-up-ratio':(
        'The share of thrust for strafing up.',
        (('HIGHER','climbs faster on the strafe and stops a sink sooner.'),('LOWER','slow to rise; a downward drift takes longer to stop.')),
        _AUTHORITY_NOTE),
    'tuning-down-ratio':(
        'The share of thrust for strafing down.',
        (('HIGHER','drops faster on the strafe and stops a rise sooner.'),('LOWER','slow to descend; an upward drift takes longer to stop.')),
        _AUTHORITY_NOTE),
    'tuning-speed-cap':(
        'The base top speed, as total speed in any direction.',
        (('HIGHER','more top speed, and longer to reach it and to stop from it. In lever Speed mode every lever position asks for more speed.'),
         ('LOWER','less top speed. Acceleration is unchanged, so the ship reaches it sooner.')),
        'Top speed = Speed cap x Class speed multiplier. At top speed, thrust that would add speed is taken away; steering and braking stay. '
        "In atmosphere the air's drag usually limits speed first."),
    'tuning-speed-cap-multiplier':(
        "This class's share of the Speed cap.",
        (('HIGHER','this class is faster flat out.'),('LOWER','this class is slower flat out.')),
        'Top speed = Speed cap x this. Acceleration fade, Brake floor and lever Speed mode all work from that top speed.'),
    'tuning-speed-hold-gain':(
        'Lever Speed mode only: how hard the ship chases the speed the lever asks for. Thrust is the speed still missing times this, '
        'up to the Forward or Retro authority.',
        (('HIGHER','snaps to the set speed and holds it tightly. Small lever moves give sharp thrust.'),
         ('LOWER','eases toward the set speed and takes longer to settle on it.')),
        'No effect in lever Thrust mode or with a centred (HOSAS) throttle.'),
    'tuning-accel-fade-exponent':(
        'How thrust tails off as the ship nears top speed.',
        (('0','off: full thrust up to top speed, then none.'),
         ('1 TO 2','thrust fades from early on: a long, gentle climb over the last part.'),
         ('4 OR MORE','thrust stays strong until close to top speed, then fades quickly.')),
        'With fade on, the ship creeps up to top speed rather than hitting it. Only thrust that adds speed fades; steering does not. '
        'Momentum follows this curve, so Momentum does little while this is 0.'),
    'tuning-brake-floor':(
        'How much braking thrust is left at high speed. Above a quarter of top speed, thrust against your motion is scaled down, '
        'but never below this share.',
        (('100%','braking is the same at any speed.'),
         ('LOWER','weaker braking the faster you go: long stops from high speed, full braking again once slow.')),
        'Values under 25% act as 25%. The parking brake is not affected.'),
    'tuning-momentum':(
        'Makes slowing down and turning at speed cost what it took to build the speed.',
        (('HIGHER','braking fades the way acceleration did, so a flip-and-burn stop takes about as long as the climb. Sideways thrust is '
                   'limited at speed, so the ship holds its line and turns wide.'),
         ('LOWER','full braking and strafing thrust at any speed: agile, less sense of mass. 0 is off.')),
        'Low speed keeps full agility at any setting. Needs Acceleration fade above 0 to do much.'),
    'tuning-overspeed-bleed-time':(
        'What happens when the ship is above top speed, for example after a pulse jump.',
        (('0','off: the ship keeps the extra speed until you brake.'),
         ('SHORTER','a firm automatic slow-down back to top speed.'),
         ('LONGER','a gentle one. Most of the extra speed is gone after about three times this.')),
        'Steering and retro thrust stay yours while it bleeds.'),
    'tuning-jerk-limit':(
        'How fast thrust can change. 0 is off: thrust follows your controls at once.',
        (('LOWER, ABOVE 0','thrust ramps in and out: smoother starts and stops, and the ship answers later.'),
         ('HIGHER','closer to instant.')),
        'Small numbers are a strong effect: at 100, a thrust of 100 u/s2 takes a full second to arrive.'),
    'tuning-boost-enabled':(
        "Sultan boost for this class: the bound boost button and the lever's boost zone.",
        (('ON','boost multiplies thrust in every direction while held, builds heat, and locks out when it overheats.'),
         ('OFF','no Sultan boost for this class.')),
        'In space boost adds thrust, not top speed: the ship gets there sooner. In air it pushes the ship past its Terminal speed.'),
    'tuning-boost-source':(
        'Where the strength of boost comes from.',
        (('NATIVE CLASS RATIO',"the ship's own boost compared with its normal thrust, so the ship and its boost upgrades count."),
         ('FIXED OVERRIDE','the Boost multiplier below, the same for every ship of this class.')),
        None),
    'tuning-boost-multiplier':(
        'How many times normal thrust the ship gets at full boost.',
        (('HIGHER','a harder kick.'),('LOWER','a milder one. 1.00 is no boost.')),
        'A cold boost gives only part of this (Cold boost strength).'),
    'tuning-boost-heat-time':(
        'Seconds of continuous boost from cold to overheat.',
        (('LONGER','long burns, and slower to warm up to full strength.'),
         ('SHORTER','short bursts: full strength comes sooner and so does the overheat.')),
        'Overheating locks boost out until it cools to Boost relight heat.'),
    'tuning-boost-cool-time':(
        'Seconds to cool from full heat to cold. Cooling starts half a second after boost stops.',
        (('SHORTER','boost is ready again sooner.'),('LONGER','boost has to be rationed.')),
        None),
    'tuning-boost-cold-strength':(
        'How much of the boost you get from a cold start. Boost grows as it heats and is full at the hot zone.',
        (('HIGHER','strong boost at once. At 100% boost is flat and heat only limits how long it lasts.'),
         ('LOWER','weak at first: boost pays off when you keep it warm.')),
        None),
    'tuning-boost-hot-zone':(
        'The heat at which boost reaches full strength.',
        (('LOWER','full strength sooner, and a wide band of it before the overheat.'),
         ('HIGHER','full strength only close to overheating: a narrow band to hold.')),
        None),
    'tuning-boost-relight-level':(
        'After an overheat, boost stays locked until heat has fallen to this level.',
        (('HIGHER','a short lockout, but boost comes back hot with little time before it overheats again.'),
         ('LOWER','a long lockout, and boost comes back cool with a long burn ahead.')),
        None),
    'tuning-brake-strength':(
        "How much of the ship's thrust the parking brake may use to stop it.",
        (('HIGHER','stops sooner.'),('LOWER','longer, softer stops.')),
        "It uses each direction's own authority, without boost, so a ship with weak Retro or Lateral authority parks slowly that way."),
    'tuning-brake-override':(
        'How much of your thrust and strafe pushes through the parking brake while you hold its button. The brake keeps '
        'pulling against the ship, so it creeps and stops when you let go.',
        (('HIGHER','the ship creeps faster against the held brake.'),('LOWER','a firmer hold; 0% ignores thrust, as a latched brake does.')),
        "Only a held button gives way. A latched brake and the lever's parking zone always hold fully. Creep speed also grows with Settle time."),
    'tuning-settle-time':(
        'How the parking brake takes off the last of the speed. From high speed it uses all its strength; near rest it slows in '
        'proportion to the speed left, over about this long.',
        (('SHORTER','a crisp stop.'),('LONGER','the ship glides to rest.')),
        None),
    'tuning-hold-threshold':(
        'The speed below which the parking brake stops the ship dead and holds it.',
        (('HIGHER','locks sooner; the last of the stop can feel like a small jolt.'),('LOWER','eases almost to rest before it locks.')),
        None),
    'tuning-pitch-rate':(
        'Top pitch rate for this class at full stick.',
        (('HIGHER','the nose comes round faster: tighter loops, twitchier aim.'),('LOWER','a slower, steadier nose.')),
        _RATE_NOTE),
    'tuning-yaw-rate':(
        'Top yaw rate for this class at full stick or pedal.',
        (('HIGHER','flat turns come round faster.'),('LOWER','slower flat turns; the ship leans on roll and pitch to turn.')),
        _RATE_NOTE),
    'tuning-roll-rate':(
        'Top roll rate for this class at full stick.',
        (('HIGHER','rolls faster: quicker to set up a pitch turn.'),('LOWER','a slower, steadier roll.')),
        _RATE_NOTE),
    'tuning-turn-response':(
        'How quickly pitch, yaw and roll reach their rate, and stop when you let go.',
        (('HIGHER','snappy: the ship follows the stick tightly.'),('LOWER','heavy: a turn builds slowly and trails off after you centre.')),
        "It scales the class engine's turn strength and roll force. 1.00 is the game's own."),
    'tuning-additive-pitch-yaw':(
        'Fighter only. The game shapes pitch and yaw as one turn, so in a diagonal each gets less than its full rate.',
        (('ON','pitch and yaw each get their full rate together: diagonal turns are faster, up to about 1.4 times.'),
         ('OFF',"the game's own shaping.")),
        "Centre both axes after turning it on. The game's torque limits and Auto-bank on yaw still apply."),
    'tuning-rotation-in-combat':(
        "Whether this class's rotation settings stay on while an enemy is engaged.",
        (('ON','rates, turn response and Fighter additive pitch/yaw carry through the fight.'),
         ('OFF',"the game's own rotation takes over for the fight, then yours returns.")),
        "Turn response stays relative to the game's combat engine."),
    'tuning-auto-bank-on-yaw':(
        "The game's assist that rolls the ship into a yaw turn.",
        (('ON','yawing banks the ship like an aircraft.'),('OFF','yaw turns flat; you roll yourself.')),
        None),
}

# Atmosphere Flight page (user 2026-11-13: "Yes, Atmosphere next"), from planetary_flight (compute, air_terms,
# wing_support, glide_drag, mav_scale, nose_authority). Planet settings first, then the class's air style.
GUIDE.update({
    'atmosphere-model':(
        'Who flies the ship near a planet.',
        (('NATIVE','the game does, as it always has.'),
         ('SULTAN','Sultan flight carries on down to the landing, with planet gravity, air that thickens toward the ground, drag and, for sleek hulls, lift. '
                   'Each class has its own air style, set below.')),
        "Landing, takeoff and pulse stay the game's own."),
    'air-gravity':(
        'The pull toward the planet near its surface.',
        (('HIGHER','a heavier world: the ship falls faster, needs more thrust or wing to hold height, and climbs slower.'),
         ('LOWER','a floaty one: slow falls and easy climbs. 0 turns gravity off.')),
        'It weakens with distance and ends at the Atmosphere top. The lift thrusters of blocky ships are set in ship weights, so they keep pace with it.'),
    'air-gravity-compensation':(
        'Whether the engines hold the ship up against gravity for you.',
        (('WHILE THRUSTING','held while you thrust or strafe, paid from your thrust. With the lever in coast you glide on the wings, or drop in a ship without them.'),
         ('ALWAYS','held even when coasting: the ship never sinks by itself.'),
         ('OFF','gravity acts in full. Hold height with thrust, the wings or the lift trim buttons.')),
        'Holding the ship up uses thrust, and the wings only carry it while this is not. The parking brake always holds position.'),
    'air-thick-top':(
        'The altitude where the air starts to thin. Below it, drag and engine loss are at full strength.',
        (('HIGHER','thick air reaches further up: a longer climb before the ship speeds up.'),
         ('LOWER','thin air starts closer to the ground: faster flight not far above it.')),
        None),
    'air-auto-level':(
        "The game's own levelling near planets.",
        (('ON','the ship rolls upright when the roll stick is centred, and over water near the surface bank and pitch stop at 45 degrees.'),
         ('OFF','Sultan holds your bank: roll in, centre the stick and pull through the turn, at any angle.')),
        None),
    'air-ground-push':(
        "The game's own ground avoidance.",
        (('ON','the ship is pushed up when terrain comes close, with a kick where the ground rises suddenly.'),
         ('OFF','nothing holds you off the ground. With Crash damage on, hitting it costs hull.')),
        'The sea is not affected.'),
    'air-top':(
        'The altitude where the air ends. Planet gravity ends here too.',
        (('HIGHER','air and gravity reach further out: a longer climb to space, and the air thins more gently.'),
         ('LOWER','space starts sooner.')),
        'Drag and engine loss fade smoothly between the thick air and here; gravity eases out over the last 2000.'),
    'tuning-air-terminal-speed':(
        'The speed full forward thrust settles at in thick air, without boost.',
        (('HIGHER','faster low down, and the air lets go of the ship until later. The wings and the handling effects below need more speed to come in, '
                   'as they scale with this speed.'),
         ('LOWER','slower low down, and the air grips from a lower speed.')),
        "The air drag is matched to the ship's thrust, so a stronger ship gets here sooner, not faster. Boost, a dive or Engines power take the ship "
        'past it, and thinner air raises it.'),
    'tuning-air-engine-loss':(
        'The share of thrust the engines lose in thick air, in every direction.',
        (('HIGHER','sluggish low down: slower to accelerate, climb, strafe and hold against gravity. The ship wakes up as it climbs.'),
         ('LOWER','the same thrust at any height.')),
        'It does not change the Terminal speed.'),
    'tuning-air-boost':(
        'The share of the boost you keep in thick air.',
        (('HIGHER','boost keeps its kick near the ground.'),('LOWER','boost is weak low down and returns as the air thins, so it pays most up high.')),
        None),
    'tuning-air-mav-lift':(
        'For blocky ships: sets the lift thrusters in thick air in ship weights, whatever the engine tier. Strafe and down thrust scale with it.',
        (('HIGHER','strong lift and strafe low down: the ship rises and hovers easily.'),
         ('LOWER','it only just lifts (1.25 barely gets off the ground; under 1 it cannot hover on them), so you point the nose and fly on the mains.'),
         ('0','off: the ship keeps its own strafe and lift thrust.')),
        "The ship's own thrusters return as the air thins."),
    'tuning-air-damping':(
        'How much of the air drag already acts at low speed.',
        (('HIGHER','the air holds the ship from the first u/s: it slows promptly when you cut thrust and builds speed more evenly.'),
         ('LOWER','the ship runs free until near the Terminal speed and coasts further.')),
        'The Terminal speed stays the same either way.'),
    'tuning-air-lift':(
        'How quickly the air turns your flight path toward the nose in thick air.',
        (('HIGHER','the ship goes where it points: bank-and-pull turns carve like a jet, with little slide.'),
         ('LOWER','the nose comes round first and the flight path follows late: turns skid.'),
         ('0','off: only thrust changes your direction.')),
        'It fades below 150 u/s, in thin air and as you slide sideways.'),
    'tuning-air-induced-drag':(
        'The speed the air takes for lift in a turn.',
        (('HIGHER','a hard pull in a bank bleeds a lot of speed: turning is spending energy.'),
         ('LOWER','turns cost little speed. 0 turns it off.')),
        'A plain roll is free. Needs Lift.'),
    'tuning-air-wing-support':(
        "How much of the ship's weight the wings carry when you coast.",
        (('HIGHER','the wings carry the ship at a lower speed, so it glides slower before it sinks.'),
         ('LOWER','it needs more speed to stay up. At 1.00 the wings carry it all from half the Terminal speed.'),
         ('0','no wings: the ship drops without thrust.')),
        'Lets you glide and trade height for speed. The wings lift along the top of the ship, so a banked glide sinks unless you pull.'),
    'tuning-air-glide-ratio':(
        'With the power off, how far the ship glides for each unit of height it loses (4 = 4:1).',
        (('HIGHER','a long, flat glide: speed stays with you when you cut the throttle.'),
         ('LOWER','a steep glide: the ship slows sooner.'),
         ('0','off: full drag at any power.')),
        'The drag along the nose eases as you cut forward thrust and returns as you add it, so full thrust still tops out at the Terminal speed. '
        'Needs Wing support.'),
    'tuning-air-omni':(
        'How much of the lift and wing support also works when the ship slides sideways or backward.',
        (('HIGHER','a strafing ship keeps its slide and still lifts and glides: flies in any direction.'),
         ('LOWER','only flight along the nose makes lift, as with an aircraft.')),
        None),
    'tuning-air-nose-authority':(
        'The share of pitch and yaw rate left at the Terminal speed in thick air.',
        (('LOWER','a heavy nose at speed: the faster and lower you fly, the slower it points. Slow down or climb to turn.'),
         ('1.00','off: speed does not slow the nose.')),
        'Roll is not affected.'),
    'tuning-air-slip-authority':(
        'The share of pitch and yaw rate left when a strafe or yaw has put the nose well off the flight path at speed.',
        (('LOWER','a slide costs you the nose: thrust forward along it to get control back.'),
         ('100%','off: sliding does not slow the nose.')),
        'Full effect with the nose about 35 degrees off the path, from half the Terminal speed.'),
    'tuning-air-side-drag':(
        'Drag on sideways and vertical motion, compared with drag along the nose.',
        (('HIGHER','the hull slices forward and resists sliding: strafes die quickly and the ship tracks its nose.'),
         ('LOWER','the ship slides and strafes freely.')),
        '1.00 is the same drag in every direction.'),
    'tuning-air-weathervane':(
        'How hard the air swings the nose back onto the flight path in a slide.',
        (('HIGHER','the nose is pulled straight firmly: holding it off the path is a fight.'),
         ('LOWER','a light pull. 0 turns it off.')),
        'Sway damping sets how the swing settles.'),
    'tuning-air-sway-damping':(
        'How quickly the weathervane swing settles.',
        (('HIGHER','the nose snaps straight.'),('LOWER','it wallows back and forth before it settles.')),
        'Only with Weathervane above 0.'),
    'tuning-air-buffet':(
        'How hard the nose shakes when you hold it well off the flight path in fast, thick air.',
        (('HIGHER','a bad angle is work to hold.'),('LOWER','a light shake. 0 turns it off.')),
        None),
    'tuning-air-overspeed-penalty':(
        'Above the Terminal speed (boosting or diving), how much harder the air makes turning.',
        (('HIGHER','a stiffer nose, a stronger pull straight, more buffet and more speed bled in a pull the further past it you go.'),
         ('LOWER','overspeed handles much as the Terminal speed does. 0 turns it off.')),
        None),
})

def describe(control_id):
    """(what it is, ((do, effect), ...), note) for a Flight Tuning or Atmosphere Flight setting, or None."""
    return GUIDE.get(control_id)


def paragraph(control_id):
    """The entry as one paragraph for the game's description panel: what it is, then 'Higher: ...' for each way,
    then the note. None for a setting without an entry."""
    entry=GUIDE.get(control_id)
    if entry is None:return None
    what,effects,note=entry
    return ' '.join([what]+[f'{do.capitalize()}: {effect}' for do,effect in effects]+([note] if note else []))
