"""Throttle/speed flight telemetry for the release runtime, build 180836 / Steam 25732212.

Read-only. Samples what the installed caves already copy out every flight frame (the six-axis
receipt and the MAV snapshot), the ship fields named in E5/D10, and what we published, at a
fixed rate while piloting. One CSV per game session in config/logs. No hooks, no writes.

Receipt offsets (six-axis frame copy, tools/native_six_axis.py): +56 conflict, +144 native u
read at the throttle site, +148 u after substitution, +200 applied mask (8 = throttle, 4 = roll).
Ship (E5): target = [+0x6678] + ([+0x6674] - [+0x6678]) * u, smooth-damp state +0x6700/+0x6704.
MAV snapshot (native_mav_observer.decode_snapshot): forward_speed and native_u are the values
the native force stage used in that invocation.
"""
import csv,math,struct,time

# Engine block fields per research/18 (T53 static map): BoostMaxSpeed, Falloff, OverspeedBrake, ThrustForce.
# 183791 offsets (each +0x10 from 180836); column names keep their 180836 offsets for continuity.
SHIP_FIELDS=(('boost_max_speed',0x6650,'<f'),('falloff',0x6660,'<f'),('overspeed_brake',0x6690,'<f'),('thrust_force',0x66A4,'<f'),
             ('max_endpoint',0x6684,'<f'),('min_endpoint',0x6688,'<f'),('smooth_velocity',0x6710,'<f'),
             ('smooth_position',0x6714,'<f'),('shared_demand',0x76A8,'<f'),('gate_674c',0x675C,'<I'),('gate_6720',0x6730,'<B'))
CHANNELS=('yaw','pitch','roll','throttle','strafe-lateral','strafe-vertical')
NEWTONIAN_COLUMNS=(['flight_model','newtonian_admitted','newtonian_reason','command_state','command_generation','command_age_ms',
                    'newtonian_native_reason','newtonian_invocation','newtonian_speed','newtonian_forward_speed',
                    'newtonian_target_speed','parking_brake','speed_cap_active','newtonian_avoidance_flag',
                    'bleeding_overspeed','boost_heat','boost_active','boost_overheated','boost_multiplier']
                   +['acceleration_body_'+axis for axis in 'xyz']
                   +['newtonian_'+name+'_'+axis for name in ('final_before','final_after','outer_velocity','outer_velocity_after_integrator') for axis in 'xyz'])
COLUMNS=(['t','piloting','reason','style','hosas_mode','lever_unit','throttle_armed','throttle_reason','rate_target']
         +[f'out_{c}' for c in CHANNELS]
         +[name for name,_,_ in SHIP_FIELDS]+['native_target']
         +['receipt_seq','conflict','u_native','u_applied','applied_mask','throttle_applied']
         +['mav_seq','forward_speed','mav_u','k_prop','modifier','k_min','weight',
           'perp_x','perp_y','perp_z','perp_speed','speed_estimate',
           'mav_reason','mav_applied','force_before_x','force_before_y','force_before_z',
           'force_after_x','force_after_y','force_after_z','combat_active','frontend_state','newtonian_requalifications']
         +NEWTONIAN_COLUMNS+['parking_brake_requested']
         +['landing_gear','ground_clearance','ground_tilt','touchdown','landing_state'])  # T101


def decode_receipt(raw):
    conflict=struct.unpack_from('<I',raw,56)[0];u_native,u_applied=struct.unpack_from('<2f',raw,144)
    mask=struct.unpack_from('<I',raw,200)[0]
    return {'conflict':conflict,'u_native':u_native,'u_applied':u_applied,'applied_mask':mask,'throttle_applied':int(bool(mask&8))}


def build_row(t,state,ship,receipt,mav):
    """One CSV row. state: runtime-side dict; ship: SHIP_FIELDS values; receipt: (seq, raw) or None;
    mav: MavPatch.sample() dict or None. Missing sources leave their columns blank."""
    row=dict.fromkeys(COLUMNS,'');row['t']=f'{t:.4f}';row.update(state)
    row.update(ship)
    if receipt:
        seq,raw=receipt;row['receipt_seq']=seq;row.update(decode_receipt(raw))
        if 'max_endpoint' in ship:
            row['native_target']=ship['min_endpoint']+(ship['max_endpoint']-ship['min_endpoint'])*row['u_applied']
    if mav:
        perp=mav['perpendicular_velocity'][:3];perp_speed=math.sqrt(sum(v*v for v in perp))
        row.update(mav_seq=mav['sequence'],forward_speed=mav['forward_speed'],mav_u=mav['native_u'],k_prop=mav['k_prop'],
                   modifier=mav['modifier'],k_min=mav['k_min'],weight=mav['weight'],perp_x=perp[0],perp_y=perp[1],perp_z=perp[2],
                   perp_speed=perp_speed,speed_estimate=math.hypot(mav['forward_speed'],perp_speed),
                   mav_reason=mav['reason'],mav_applied=mav['applied_count'])
        for name,vector in (('before',mav['before']),('after',mav['after'])):
            for axis,value in zip('xyz',vector):row[f'force_{name}_{axis}']=value
    return {k:(f'{v:.6g}' if isinstance(v,float) else v) for k,v in row.items()}


class TelemetryWriter:
    """Rate-limited CSV writer; opens its file on the first row so idle sessions leave nothing."""
    def __init__(self,path,hz=50.,now=time.monotonic):
        self.path=path;self.period=1./hz;self.now=now;self.next=0.;self.file=None;self.writer=None;self.rows=0;self.last_flush=0.

    def due(self):
        t=self.now()
        if t<self.next:return False
        # Keep cadence when on time; after a stall restart from now instead of bursting to catch up.
        self.next=self.next+self.period if t-self.next<self.period else t+self.period
        return True

    def write(self,row):
        if self.file is None:
            self.file=open(self.path,'w',newline='',encoding='utf-8')
            self.writer=csv.DictWriter(self.file,fieldnames=COLUMNS);self.writer.writeheader()
        self.writer.writerow(row);self.rows+=1
        t=self.now()
        if t-self.last_flush>=1.:self.file.flush();self.last_flush=t

    def close(self):
        if self.file is not None:self.file.close();self.file=None
