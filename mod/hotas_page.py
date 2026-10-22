"""Native NMS options rows for the HOTAS menu (D15/D18), build 180836 / Steam 25732212.

Entry: the native Controls page gets an "ABSOLUTE HOTAS" header and a "HOTAS Flight Setup"
button, inserted before FinalizePage as proven in F1/U3. Each HOTAS screen is another native
Controls page pushed with RequestOptionsPage(2, True) (the navigation proven in F0), so native
Back pops between our screens with no interception. While one of our depths is on screen the
runtime suppresses the native Controls rows and this module emits ours.

Row mapping uses only row types with live evidence: Header, Button and Float (percent or plain,
native arrows/wheel). Switches and choices are Float rows too, stepping by one through their values,
so the row always shows the current value and the arrows pick the previous or next one.
api is a dict of native callables, so everything here is tested with a fake.
"""
import struct,threading,time
from hotas_menu import ROOT,MenuController,build_page
from hotas_settings import BY_ID,Control,profile_defaults

CONTROLS_PAGE=2
ENTRY_HEADER='ABSOLUTE HOTAS'
ENTRY_BUTTON='HOTAS Flight Setup'
ENTRY_HELP='Bind and tune your HOTAS or HOSAS flight controls.'
FOCUS_SECONDS=3.
FORMATS={'percent':(100.,' %','%.0f%s'),'multiplier':(1.,'x','%.2f%s'),'per-second':(1.,' /s','%.2f%s'),'speed':(1.,' u/s','%.0f%s'),
         'seconds':(1.,' s','%.2f%s'),'jerk':(1.,' u/s³','%.0f%s'),'acceleration':(1.,' u/s²','%.2f%s'),'pixels':(1.,' px/s','%.0f%s'),'degrees':(1.,' deg','%.1f%s'),'g':(1.,' g','%.1f%s'),
         'number':(1.,'','%.2f%s'),'distance':(1.,' u','%.0f%s')}
# The native Boolean row shows the state a click switches to, not the current one ("Enable" while
# off, then "Off" while on; user, 2026-10-16/17). Switches and choices therefore use the Float row:
# it shows the current value, and its arrows step to the previous/next one.
BOOL_TEXTS=('Off','On')


class Strings:
    """Stable C strings for native calls; never hand NMS a pointer that can move or be freed."""
    def __init__(self,factory):
        self.factory=factory;self.buffers={}

    def __call__(self,text):
        if text is None:return 0
        buf=self.buffers.get(text)
        if buf is None:
            if len(self.buffers)>4000:raise RuntimeError('string pool overflow')
            buf=self.buffers[text]=self.factory(text)
        return buf.address


class NativeMenu:
    def __init__(self,store,strings,options,capture_start=None,capture_cancel=None,now=time.monotonic,on_action=None):
        self.store=store;self.s=strings;self.options=options;self.now=now
        self.lock=threading.RLock()
        self.controller=MenuController(store,self._start_capture if capture_start else None,capture_cancel,on_action=on_action)
        self._capture_start=capture_start
        self.depth_routes={}  # native options-stack depth -> our route
        self.pending=None  # route waiting for its RequestOptionsPage push
        self.last_changed=(None,0.);self._labels=set();self._section='';self._repeats=set()
        self.meters={}  # spacer label -> meter spec, for the drawing pass

    # ----- capture plumbing (runtime thread calls back) -----
    def _start_capture(self,binding_id):
        def done(token):
            with self.lock:
                if token=='timeout':
                    self.controller.capture=None
                elif token=='clear':  # Delete/Backspace during capture
                    if self.controller.clear_capture()=='stale':self.controller.capture=None
                else:self.controller.capture_result(token)
        # Read by the runtime's capture start: which device kind to listen to.
        done.kind=('key' if binding_id=='button-add-key' else 'modifier' if binding_id=='button-modifier'
                   else 'button' if binding_id.startswith('button-') else 'axis')
        self._capture_start(done)

    # ----- navigation -----
    def frame(self,depth):
        """Called at DoOptions entry with the native stack depth; returns our route or None."""
        with self.lock:
            if depth==0:self.pending=None
            for d in sorted((d for d in self.depth_routes if d>depth),reverse=True):
                del self.depth_routes[d]
                # Native Back popped one of our screens: mirror it, cancel any capture, save.
                while self.controller.capture or self.controller.conflict:self.controller.back()
                if len(self.controller.stack)>1:self.controller.back()
                else:self.store.save()
            return self.depth_routes.get(depth)

    def take_pending(self):
        """Route waiting to be pushed (consumed once, on the application thread)."""
        with self.lock:
            route,self.pending=self.pending,None
            return route

    def pushed(self,route,depth):
        """Record a route only after the native push is observed at its new depth."""
        with self.lock:self.depth_routes[depth]=route

    def close_all(self):
        """Stop owning every screen (watchdog / menu left): native rows return, settings saved."""
        with self.lock:
            self.depth_routes.clear();self.pending=None;self.meters={}
            self.controller.capture=None;self.controller.conflict=None;self.controller.stack=[ROOT]
            self.store.save()

    def open_root(self):
        with self.lock:
            if self.pending is None:
                self.controller.stack=[ROOT];self.controller.capture=None;self.controller.conflict=None
                self.pending=ROOT

    def root_pushed(self,depth):
        """Pause-root entry: the runtime already pushed a Controls page at depth; own it as ROOT."""
        with self.lock:
            self.controller.stack=[ROOT];self.controller.capture=None;self.controller.conflict=None
            self.pending=None;self.depth_routes={depth:ROOT}

    @property
    def open(self):return bool(self.depth_routes) or self.pending is not None

    # ----- rows -----
    def entry_rows(self,page,api):
        api['Header'](page,self.s(ENTRY_HEADER),0)
        if api['Button'](page,self.s(ENTRY_BUTTON),self.s(ENTRY_HELP),self.options.button):self.open_root()

    def focused(self):
        control,at=self.last_changed
        return control if self.now()-at<=FOCUS_SECONDS else None

    def page_rows(self,page,api,route,live=None,device_names=None):
        with self.lock:
            self.controller.stack=self.controller.stack[:1] if route==ROOT else [ROOT,route]
            view=self.controller.page(live,self.focused(),device_names)
            self.meters={};self._labels=set();self._section=''
            names=[r.label for r in view.rows if r.kind!='meter']
            self._repeats={n for n in names if names.count(n)>1}|({'Binding'} if names.count('Binding')>1 else set())
            if view.modal:self._modal_rows(page,api,view.modal)
            for row in view.rows:self._row(page,api,row)
            return len(view.rows)

    def _label(self,text):
        """Native rows are identified by label (F1), so every label on a page must be unique.
        Repeats are qualified with their section the way NMS does, e.g. 'Deadzone (Pitch)'."""
        base=text or ' ';label=base
        if (base in self._repeats or label in self._labels) and self._section:label=f'{base} ({self._section})'
        n=2
        while label in self._labels:label=f'{base} ({n})';n+=1
        self._labels.add(label)
        return self.s(label)

    def _modal_rows(self,page,api,modal):
        api['Header'](page,self._label(modal['text'].upper() if modal['kind']=='capture' else 'BINDING CONFLICT'),0)
        if modal['kind']=='capture':
            if modal.get('clearable') and api['Button'](page,self._label('Clear binding'),self.s('Remove this binding.'),self.options.button):
                self.controller.clear_capture();return
            if api['Button'](page,self._label('Cancel binding'),self.s(modal['text']),self.options.button):self.controller.back()
        else:
            if api['Button'](page,self._label('Move binding'),self.s(modal['text']),self.options.button):self.controller.resolve_conflict(True)
            if api['Button'](page,self._label('Keep current binding'),self.s(modal['text']),self.options.button):self.controller.resolve_conflict(False)

    def _row(self,page,api,row):
        s=self.s
        if row.kind=='header':
            if row.label:self._section=row.label.title()
            api['Header'](page,self._label(row.label or ' '),0);return
        if row.kind=='meter':
            label='  '+row.id.upper()  # unique spacer row; the drawing pass covers it
            # Register before the call: NMS reports the row's root (GetRow) while creating it.
            self.meters[label]=row.meter;api['Header'](page,self._label(label),0);return
        if row.kind=='button':
            text=row.label if row.enabled else f"{row.label}  ({row.suffix or 'unavailable'})"
            if api['Button'](page,self._label(text),s(row.help),self.options.button) and row.enabled:self._activate(row.id)
            return
        if row.kind=='binding':
            label=f'{row.label} ({self._section})' if row.label in self._repeats and self._section else row.label
            if api['Button'](page,self._label(f'{label}:  {row.value}'),s(row.help),self.options.button):self._activate(row.id)
            return
        if row.kind=='readout':
            # Ignore edits: each frame displays the measured value. Keep ordinary finite
            # bounds (a zero-width native range could divide by zero inside the widget).
            # Stable labels avoid allocating native C strings for every speed sample.
            unit={' u/s²':'acceleration',' deg':'degrees',' %':'percent','x':'multiplier'}.get(row.suffix,'speed')
            # Readout rows hold display values, while native Float options apply
            # their own scale (e.g. heat 85% is passed as the fraction 0.85).
            value=float(row.value)/FORMATS[unit][0]
            control=Control(row.id,'float',row.label,row.help,value,unit=unit)
            api['Float'](page,self._label(row.label),s('Live measurement; changes here are ignored.'),value,value,-1e6,1e6,self.options.float(control));return
        control=BY_ID[row.id];value=self.store.values[row.id]
        default=profile_defaults(self.store.current_class)[row.id[len('tuning-'):].replace('-','_')] if row.id.startswith('tuning-') else control.default
        if row.kind in ('bool','choice'):
            if row.kind=='bool':keys,texts=(False,True),BOOL_TEXTS
            else:keys=[k for k,_ in control.choices];texts=[t for _,t in control.choices]
            current=keys.index(value)
            new=api['Float'](page,self._label(control.label),s(control.help),float(current),float(keys.index(default)),
                             0.,float(len(keys)-1),self.options.choice(texts[current]))
            new=min(len(keys)-1,max(0,int(round(float(new)))))
            if new!=current:self._set(row.id,keys[new])
        elif row.kind=='float':
            new=float(api['Float'](page,self._label(control.label),s(control.help),float(value),float(default),
                                   float(control.low),float(control.high),self.options.float(control)))
            if abs(new-value)>1e-7:self._set(row.id,new)

    def _set(self,control_id,value):
        self.store.set(control_id,value);self.last_changed=(control_id,self.now())

    def _activate(self,row_id):
        result=self.controller.activate(row_id)
        if result=='opened':self.pending=self.controller.route


class NativeOptions:
    """Native option blocks: Button (1 byte) and Float (<ffQQ), including text-valued choice blocks."""
    def __init__(self,strings,factory_bytes):
        self.s=strings;self.make=factory_bytes;self.cache={}
        self.button=self._block('button',b'\0')

    def _block(self,key,data):
        if key not in self.cache:self.cache[key]=self.make(data)
        return self.cache[key].address

    def choice(self,text):
        """Float block that prints text as the value: step 1, no scale or suffix. The native row
        formats with sprintf(buf, fmt, value, suffix) (0x1B72E2), so a format with no conversions
        prints the text as is."""
        return self._block(('choice',text),struct.pack('<ffQQ',1.,1.,self.s(''),self.s(text.replace('%','%%'))))

    def float(self,control):
        scale,suffix,fmt=FORMATS[control.unit]
        return self._block(('float',control.step,control.unit),struct.pack('<ffQQ',control.step,scale,self.s(suffix),self.s(fmt)))
