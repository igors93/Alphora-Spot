//! Opt-in frame profiler for diagnosing interface responsiveness.
//!
//! Set `ALPHORA_UI_PROFILE` to a file path and every five seconds the interface
//! appends one line with the frame count, the CPU time spent building frames and
//! the code locations that requested repaints. Disabled, it costs one branch.

use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::time::{Duration, Instant};

const WINDOW: Duration = Duration::from_secs(5);

pub struct FrameProfiler {
    output: Option<PathBuf>,
    window_start: Instant,
    frames: u32,
    busy: Duration,
    worst: Duration,
    causes: HashMap<String, u32>,
}

impl FrameProfiler {
    pub fn from_env() -> Self {
        Self {
            output: std::env::var_os("ALPHORA_UI_PROFILE").map(PathBuf::from),
            window_start: Instant::now(),
            frames: 0,
            busy: Duration::ZERO,
            worst: Duration::ZERO,
            causes: HashMap::new(),
        }
    }

    /// Records one frame that took `cost` to build.
    pub fn record(&mut self, ctx: &egui::Context, cost: Duration) {
        let Some(output) = &self.output else {
            return;
        };
        self.frames += 1;
        self.busy += cost;
        self.worst = self.worst.max(cost);
        for cause in ctx.repaint_causes() {
            *self
                .causes
                .entry(format!("{}:{}", cause.file, cause.line))
                .or_default() += 1;
        }
        let elapsed = self.window_start.elapsed();
        if elapsed < WINDOW {
            return;
        }
        let mut causes: Vec<_> = self.causes.drain().collect();
        causes.sort_by_key(|entry| std::cmp::Reverse(entry.1));
        let causes: Vec<String> = causes
            .into_iter()
            .take(6)
            .map(|(place, count)| format!("{place}={count}"))
            .collect();
        let line = format!(
            "fps={:.1} build_avg_ms={:.2} build_max_ms={:.2} build_cpu={:.1}% causes=[{}]\n",
            f64::from(self.frames) / elapsed.as_secs_f64(),
            self.busy.as_secs_f64() * 1000.0 / f64::from(self.frames.max(1)),
            self.worst.as_secs_f64() * 1000.0,
            self.busy.as_secs_f64() * 100.0 / elapsed.as_secs_f64(),
            causes.join(" "),
        );
        if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(output) {
            let _ = file.write_all(line.as_bytes());
        }
        self.window_start = Instant::now();
        self.frames = 0;
        self.busy = Duration::ZERO;
        self.worst = Duration::ZERO;
    }
}
