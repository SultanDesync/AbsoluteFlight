//! The poll thread and non-blocking publication.
//!
//! The poller writes each frame into the back one of two slots with `try_lock` and then flips an
//! atomic index. Readers lock the front slot only long enough to copy it. The poller never waits on a
//! reader: if a slow reader still holds the back slot, the front slot is refreshed in place instead, and
//! only if both are held is that publication skipped (counted in `Stats::publish_skips`). No edges are
//! lost by a skip, because press/release counters are cumulative.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use crate::core::{BusCore, Calibration, Frame};
use crate::source::{InputSource, SourceFactory};

#[derive(Clone, Debug)]
pub struct BusConfig {
    pub poll_hz: u32,
    /// Hot-plug housekeeping interval.
    pub rescan_interval: Duration,
    pub calibration: Calibration,
}

impl Default for BusConfig {
    fn default() -> Self {
        Self { poll_hz: 250, rescan_interval: Duration::from_secs(1), calibration: Calibration::default() }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Stats {
    pub polls: u64,
    pub publishes: u64,
    pub publish_skips: u64,
    pub rebuilds: u64,
    /// Mean poll-loop period over the last second, microseconds (0 until measured).
    pub mean_period_us: u64,
    /// Mean time spent polling all sources per tick, microseconds.
    pub mean_poll_us: u64,
}

struct Shared {
    stop: AtomicBool,
    pending_sources: Mutex<Vec<SourceFactory>>,
    minimum_poll_hz: AtomicU32,
    wake: Condvar,
    slots: [Mutex<Frame>; 2],
    front: AtomicUsize,
    published: AtomicBool,
    polls: AtomicU64,
    publishes: AtomicU64,
    publish_skips: AtomicU64,
    rebuilds: AtomicU64,
    mean_period_us: AtomicU64,
    mean_poll_us: AtomicU64,
}

pub struct Bus {
    shared: Arc<Shared>,
    thread: Option<JoinHandle<()>>,
}

impl Bus {
    /// Starts the poll thread. Sources are constructed on that thread, so DirectInput COM objects never cross threads.
    pub fn start(config: BusConfig, factories: Vec<SourceFactory>) -> std::io::Result<Bus> {
        let shared = Arc::new(Shared {
            stop: AtomicBool::new(false),
            pending_sources: Mutex::new(Vec::new()),
            minimum_poll_hz: AtomicU32::new(1),
            wake: Condvar::new(),
            slots: [Mutex::new(Frame::default()), Mutex::new(Frame::default())],
            front: AtomicUsize::new(0),
            published: AtomicBool::new(false),
            polls: AtomicU64::new(0),
            publishes: AtomicU64::new(0),
            publish_skips: AtomicU64::new(0),
            rebuilds: AtomicU64::new(0),
            mean_period_us: AtomicU64::new(0),
            mean_poll_us: AtomicU64::new(0),
        });
        let thread_shared = shared.clone();
        let thread = std::thread::Builder::new()
            .name("absolute-input-bus-poll".into())
            .spawn(move || run(thread_shared, config, factories))?;
        Ok(Bus { shared, thread: Some(thread) })
    }

    /// True once the first frame has been published.
    pub fn ready(&self) -> bool {
        self.shared.published.load(Ordering::Acquire)
    }

    /// Adds a source on the poll thread without restarting existing hardware. A timed source can
    /// require a minimum poll rate for its watchdog; adding it wakes even a slow sleeping poller.
    pub fn add_source(&self, factory: SourceFactory, minimum_poll_hz: u32) {
        let mut pending = self.shared.pending_sources.lock().unwrap_or_else(|p| p.into_inner());
        pending.push(factory);
        self.shared.minimum_poll_hz.fetch_max(minimum_poll_hz.clamp(1, 2000), Ordering::Relaxed);
        self.shared.wake.notify_one();
    }

    /// Blocks up to `timeout` for the first published frame.
    pub fn wait_ready(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while !self.ready() {
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        true
    }

    /// Runs `f` on the latest published frame without cloning it.
    pub fn with_frame<R>(&self, f: impl FnOnce(&Frame) -> R) -> R {
        let index = self.shared.front.load(Ordering::Acquire);
        let guard = self.shared.slots[index].lock().unwrap_or_else(|p| p.into_inner());
        f(&guard)
    }

    pub fn frame(&self) -> Frame {
        self.with_frame(Frame::clone)
    }

    pub fn stats(&self) -> Stats {
        let s = &self.shared;
        Stats {
            polls: s.polls.load(Ordering::Relaxed),
            publishes: s.publishes.load(Ordering::Relaxed),
            publish_skips: s.publish_skips.load(Ordering::Relaxed),
            rebuilds: s.rebuilds.load(Ordering::Relaxed),
            mean_period_us: s.mean_period_us.load(Ordering::Relaxed),
            mean_poll_us: s.mean_poll_us.load(Ordering::Relaxed),
        }
    }

    /// Stops and joins the poll thread. Sources are dropped on that thread.
    pub fn stop(mut self) {
        self.stop_inner();
    }

    fn stop_inner(&mut self) {
        {
            let _pending = self.shared.pending_sources.lock().unwrap_or_else(|p| p.into_inner());
            self.shared.stop.store(true, Ordering::Release);
            self.shared.wake.notify_one();
        }
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

impl Drop for Bus {
    fn drop(&mut self) {
        self.stop_inner();
    }
}

fn publish(shared: &Shared, core: &BusCore) {
    let front = shared.front.load(Ordering::Acquire);
    let back = 1 - front;
    // Prefer the back slot and flip. If a slow reader still holds it, refresh the front slot in place
    // (readers lock whole slots, so they always see a consistent frame). Skip only if both are held.
    let target = if let Ok(mut slot) = shared.slots[back].try_lock() {
        core.write_frame(&mut slot);
        Some(back)
    } else if let Ok(mut slot) = shared.slots[front].try_lock() {
        core.write_frame(&mut slot);
        Some(front)
    } else {
        None
    };
    match target {
        Some(index) => {
            shared.front.store(index, Ordering::Release);
            shared.published.store(true, Ordering::Release);
            shared.publishes.fetch_add(1, Ordering::Relaxed);
        }
        None => {
            shared.publish_skips.fetch_add(1, Ordering::Relaxed);
        }
    }
}

fn run(shared: Arc<Shared>, config: BusConfig, factories: Vec<SourceFactory>) {
    let mut sources: Vec<Box<dyn InputSource>> = factories.into_iter().map(|f| f()).collect();
    let mut core = BusCore::new();
    core.calibration = config.calibration;
    for source in &mut sources {
        source.maintain();
    }
    core.rebuild(&sources);
    shared.rebuilds.fetch_add(1, Ordering::Relaxed);

    let mut next_tick = Instant::now();
    let mut next_rescan = Instant::now() + config.rescan_interval;
    let mut window_start = Instant::now();
    let mut window_ticks = 0u64;
    let mut window_poll = Duration::ZERO;

    while !shared.stop.load(Ordering::Acquire) {
        let added: Vec<_> = shared.pending_sources.lock().unwrap_or_else(|p| p.into_inner()).drain(..).collect();
        if !added.is_empty() {
            for factory in added {
                let mut source = factory();
                source.maintain();
                sources.push(source);
            }
            core.rebuild(&sources);
            shared.rebuilds.fetch_add(1, Ordering::Relaxed);
            next_tick = Instant::now();
        }
        let hz = config.poll_hz.max(shared.minimum_poll_hz.load(Ordering::Relaxed)).clamp(1, 2000);
        let period = Duration::from_secs_f64(1.0 / hz as f64);
        let now = Instant::now();
        if now >= next_rescan {
            let mut changed = false;
            for source in &mut sources {
                changed |= source.maintain();
            }
            if changed {
                core.rebuild(&sources);
                shared.rebuilds.fetch_add(1, Ordering::Relaxed);
            }
            next_rescan = now + config.rescan_interval;
        }

        let poll_start = Instant::now();
        core.poll(&mut sources);
        window_poll += poll_start.elapsed();
        shared.polls.fetch_add(1, Ordering::Relaxed);
        publish(&shared, &core);

        window_ticks += 1;
        let window = window_start.elapsed();
        if window >= Duration::from_secs(1) {
            shared.mean_period_us.store((window.as_micros() as u64) / window_ticks, Ordering::Relaxed);
            shared.mean_poll_us.store((window_poll.as_micros() as u64) / window_ticks, Ordering::Relaxed);
            window_start = Instant::now();
            window_ticks = 0;
            window_poll = Duration::ZERO;
        }

        next_tick += period;
        let now = Instant::now();
        if next_tick > now {
            let pending = shared.pending_sources.lock().unwrap_or_else(|p| p.into_inner());
            if pending.is_empty() && !shared.stop.load(Ordering::Acquire) {
                let remaining = next_tick - now;
                if remaining > Duration::from_millis(10) {
                    drop(shared.wake.wait_timeout(pending, remaining).unwrap_or_else(|p| p.into_inner()));
                } else {
                    // Windows condition-variable timeouts can round a 1 ms poll to a coarse system
                    // timer tick. Preserve the existing high-rate sleep path; source additions and
                    // shutdown then wait at most this short interval.
                    drop(pending);
                    std::thread::sleep(remaining);
                }
            }
        } else if now - next_tick > period * 4 {
            // Fell far behind (e.g. a slow re-enumeration); don't try to catch up with a burst.
            next_tick = now;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::virtual_source::VirtualSource;

    #[test]
    fn thread_publishes_virtual_device_and_stops() {
        let (factory, handle) = VirtualSource::factory("agent-0").unwrap();
        let bus = Bus::start(BusConfig { poll_hz: 500, ..Default::default() }, vec![factory]).unwrap();
        assert!(bus.wait_ready(Duration::from_secs(2)));
        handle.set_button(7, true);
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let pressed = bus.with_frame(|f| f.snapshots.first().map_or(0, |s| s.press_count[6]));
            if pressed == 1 {
                break;
            }
            assert!(Instant::now() < deadline, "press never published");
            std::thread::sleep(Duration::from_millis(2));
        }
        let frame = bus.frame();
        assert_eq!(frame.index_of("virtual:agent-0"), Some(0));
        assert!(frame.snapshots[0].is_down(6));
        let stats = bus.stats();
        assert!(stats.polls >= 1 && stats.publishes >= 1);
        bus.stop();
    }

    #[test]
    fn slow_reader_never_blocks_poller() {
        let (factory, _handle) = VirtualSource::factory("agent-0").unwrap();
        let bus = Bus::start(BusConfig { poll_hz: 1000, ..Default::default() }, vec![factory]).unwrap();
        assert!(bus.wait_ready(Duration::from_secs(2)));
        let before = bus.stats().polls;
        // Hold the front slot for 100 ms; the poller must keep polling into the back slot.
        bus.with_frame(|_| std::thread::sleep(Duration::from_millis(100)));
        let after = bus.stats().polls;
        assert!(after - before >= 20, "poller stalled behind a reader: {before} -> {after}");
        let published = bus.stats().publishes;
        std::thread::sleep(Duration::from_millis(20));
        assert!(bus.stats().publishes > published, "publication resumed");
        bus.stop();
    }

    #[test]
    fn adding_timed_source_wakes_slow_bus_and_expires_without_host_calls() {
        let bus = Bus::start(BusConfig { poll_hz: 1, ..Default::default() }, vec![]).unwrap();
        assert!(bus.wait_ready(Duration::from_secs(2)));
        let (factory, handle) = VirtualSource::factory("agent-0").unwrap();
        handle.enable_lease(Duration::from_millis(100));
        bus.add_source(factory, 100);
        let added = Instant::now();
        loop {
            if bus.with_frame(|f| f.index_of("virtual:agent-0").is_some()) { break; }
            assert!(added.elapsed() < Duration::from_millis(500), "slow poller did not wake for added source");
            std::thread::sleep(Duration::from_millis(2));
        }
        assert!(handle.update_leased(|state| { state.axes[0] = 65535; state.buttons[0] = true; }));
        std::thread::sleep(Duration::from_millis(250));
        let snapshot = bus.frame().snapshots[0];
        assert_eq!(snapshot.connected, 0);
        assert_eq!(snapshot.normalized_axes, [0.0; 8]);
        assert_eq!(snapshot.digital_down, [0; 3]);
        assert_eq!(handle.state(), crate::source::RawState::neutral());
        bus.stop();
    }
}
