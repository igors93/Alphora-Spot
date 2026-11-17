//! Bounded log buffer and the engine log-line classifier.

use std::collections::VecDeque;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::Duration;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    Info,
    Warning,
    Error,
}

impl Level {
    pub fn parse(value: &str) -> Self {
        match value.trim().to_ascii_uppercase().as_str() {
            "ERROR" | "CRITICAL" => Self::Error,
            "WARNING" | "WARN" => Self::Warning,
            _ => Self::Info,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Info => "INFO",
            Self::Warning => "AVISO",
            Self::Error => "ERRO",
        }
    }
}

#[derive(Clone, Debug)]
pub struct LogEntry {
    pub time: String,
    pub level: Level,
    pub source: &'static str,
    pub message: String,
    /// Continuation rows of a multi-line message are drawn without time/level badges.
    pub continuation: bool,
}

/// Ring buffer of display rows; multi-line messages are split so rows have fixed height.
pub struct LogBuffer {
    entries: VecDeque<LogEntry>,
    capacity: usize,
    revision: u64,
    warnings: usize,
    errors: usize,
}

impl LogBuffer {
    pub fn new(capacity: usize) -> Self {
        Self {
            entries: VecDeque::with_capacity(capacity.min(4096)),
            capacity,
            revision: 0,
            warnings: 0,
            errors: 0,
        }
    }

    /// Warning and error messages currently held (continuation rows are not counted).
    pub fn counts(&self) -> (usize, usize) {
        (self.warnings, self.errors)
    }

    fn count(&mut self, entry: &LogEntry, delta: isize) {
        if entry.continuation {
            return;
        }
        let slot = match entry.level {
            Level::Warning => &mut self.warnings,
            Level::Error => &mut self.errors,
            Level::Info => return,
        };
        *slot = slot.saturating_add_signed(delta);
    }

    pub fn push(&mut self, level: Level, source: &'static str, message: &str) {
        let time = clock();
        for (index, line) in message.lines().enumerate() {
            if self.entries.len() == self.capacity {
                if let Some(dropped) = self.entries.pop_front() {
                    self.count(&dropped, -1);
                }
            }
            let entry = LogEntry {
                time: time.clone(),
                level,
                source,
                message: line.to_string(),
                continuation: index > 0,
            };
            self.count(&entry, 1);
            self.entries.push_back(entry);
        }
        self.revision += 1;
    }

    pub fn info(&mut self, source: &'static str, message: &str) {
        self.push(Level::Info, source, message);
    }

    pub fn warn(&mut self, source: &'static str, message: &str) {
        self.push(Level::Warning, source, message);
    }

    pub fn error(&mut self, source: &'static str, message: &str) {
        self.push(Level::Error, source, message);
    }

    pub fn clear(&mut self) {
        self.entries.clear();
        self.warnings = 0;
        self.errors = 0;
        self.revision += 1;
    }

    pub fn revision(&self) -> u64 {
        self.revision
    }

    pub fn get(&self, index: usize) -> Option<&LogEntry> {
        self.entries.get(index)
    }

    pub fn iter(&self) -> impl Iterator<Item = &LogEntry> {
        self.entries.iter()
    }

    pub fn to_plain_text(&self) -> String {
        let mut text = String::new();
        for entry in &self.entries {
            if entry.continuation {
                text.push_str("    ");
            } else {
                text.push_str(&format!(
                    "{} [{}] {}: ",
                    entry.time,
                    entry.level.label(),
                    entry.source
                ));
            }
            text.push_str(&entry.message);
            text.push('\n');
        }
        text
    }
}

/// Wall-clock time `HH:MM:SS` in UTC offset by the local zone reported by the OS.
fn clock() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or_default();
    let local = now + local_offset_seconds();
    let seconds = local.rem_euclid(86_400);
    format!(
        "{:02}:{:02}:{:02}",
        seconds / 3600,
        (seconds % 3600) / 60,
        seconds % 60
    )
}

fn local_offset_seconds() -> i64 {
    static OFFSET: std::sync::OnceLock<i64> = std::sync::OnceLock::new();
    *OFFSET.get_or_init(|| {
        std::env::var("ALPHORA_UTC_OFFSET_MINUTES")
            .ok()
            .and_then(|v| v.trim().parse::<i64>().ok())
            .map(|minutes| minutes * 60)
            .unwrap_or_else(system_offset_seconds)
    })
}

#[cfg(windows)]
fn system_offset_seconds() -> i64 {
    #[repr(C)]
    struct SystemTime {
        year: u16,
        month: u16,
        day_of_week: u16,
        day: u16,
        hour: u16,
        minute: u16,
        second: u16,
        millis: u16,
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetSystemTime(out: *mut SystemTime);
        fn GetLocalTime(out: *mut SystemTime);
    }
    let mut utc = SystemTime {
        year: 0,
        month: 0,
        day_of_week: 0,
        day: 0,
        hour: 0,
        minute: 0,
        second: 0,
        millis: 0,
    };
    let mut local = SystemTime {
        year: 0,
        month: 0,
        day_of_week: 0,
        day: 0,
        hour: 0,
        minute: 0,
        second: 0,
        millis: 0,
    };
    // SAFETY: both calls only write into the provided, properly sized structs.
    unsafe {
        GetSystemTime(&mut utc);
        GetLocalTime(&mut local);
    }
    let minutes = |t: &SystemTime| i64::from(t.day) * 1440 + i64::from(t.hour) * 60 + i64::from(t.minute);
    let mut diff = minutes(&local) - minutes(&utc);
    // Month boundaries: the day difference can only be ±1 day.
    if diff > 1440 {
        diff -= i64::from(local.day) * 1440;
    } else if diff < -1440 {
        diff += i64::from(utc.day) * 1440;
    }
    diff * 60
}

#[cfg(unix)]
fn system_offset_seconds() -> i64 {
    // SAFETY: localtime_r writes into the provided tm struct only.
    unsafe {
        let now = libc::time(std::ptr::null_mut());
        let mut tm: libc::tm = std::mem::zeroed();
        if libc::localtime_r(&now, &mut tm).is_null() {
            return 0;
        }
        tm.tm_gmtoff
    }
}

/// Mirrors the engine's visible-log policy: warnings and errors (minus benign retry
/// noise) plus a short allow-list of lifecycle milestones. Returns `None` to hide.
pub fn classify_engine_line(line: &str) -> Option<(Level, String)> {
    let text = line.trim();
    if text.is_empty() {
        return None;
    }
    let severity = extract_severity(text);
    if matches!(severity, "WARNING" | "ERROR" | "CRITICAL") {
        if is_benign_retry_noise(text) {
            return None;
        }
        return Some((Level::parse(severity), compact(text)));
    }
    is_important_info(text).then(|| (Level::Info, compact(text)))
}

fn extract_severity(text: &str) -> &'static str {
    for severity in ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] {
        if text.contains(&format!("| {severity} |")) || text.contains(&format!("[{severity}]")) {
            return severity;
        }
    }
    "INFO"
}

fn is_benign_retry_noise(text: &str) -> bool {
    text.contains("policy=binance_timestamp_resync")
        && text.contains("binance_code=-4046")
        && (text.contains("Retry |") || text.contains("Retry exhausted"))
}

fn is_important_info(text: &str) -> bool {
    const TOKENS: [&str; 15] = [
        "alphora iniciado com pid",
        "iniciando alphora",
        "boot governor efetivo",
        "alphora governor iniciado",
        "websocket publico de mercado iniciado",
        "websocket publico de mercado conectado",
        "websocket de mercado ativo",
        "coalestra_exchange_info_bootstrapped",
        "recovery completed",
        "recovery completed with no open position",
        "margin type already configured",
        "leverage configured",
        "governador avaliando simbolos candidatos",
        "processo finalizado",
        "processo parado",
    ];
    let lowered = text.to_lowercase();
    TOKENS.iter().any(|token| lowered.contains(token))
}

fn compact(text: &str) -> String {
    let parts: Vec<&str> = text.split('|').map(str::trim).collect();
    if parts.len() >= 5 && looks_like_timestamp(parts[0]) {
        return parts[3..].join(" | ");
    }
    if text.chars().count() > 600 {
        let cut: String = text.chars().take(600).collect();
        return format!("{}...", cut.trim_end());
    }
    text.to_string()
}

fn looks_like_timestamp(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() >= 19 && bytes[4] == b'-' && bytes[13] == b':'
}

/// Remembers recently shown messages so stdout and the runtime log do not double up.
pub struct Dedupe {
    recent: VecDeque<String>,
    capacity: usize,
}

impl Dedupe {
    pub fn new(capacity: usize) -> Self {
        Self {
            recent: VecDeque::with_capacity(capacity),
            capacity,
        }
    }

    /// Returns true the first time a (level, message) pair is seen recently.
    pub fn admit(&mut self, level: Level, message: &str) -> bool {
        let key = format!("{}|{message}", level.label());
        if self.recent.contains(&key) {
            return false;
        }
        if self.recent.len() == self.capacity {
            self.recent.pop_front();
        }
        self.recent.push_back(key);
        true
    }

    pub fn clear(&mut self) {
        self.recent.clear();
    }
}

/// Follows the engine's run log (`runtime/runs/<run_id>/logs/alphora.log`) on a
/// background thread, delivering classified lines in bounded batches.
pub struct LogTail {
    rx: Receiver<Vec<(Level, String)>>,
    stop: Arc<AtomicBool>,
}

impl LogTail {
    const MAX_BYTES_PER_POLL: u64 = 512 * 1024;

    pub fn start(root: PathBuf, run_id: String, ctx: egui::Context) -> Self {
        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        thread::Builder::new()
            .name("alphora-log-tail".into())
            .spawn(move || {
                let candidates = [
                    root.join("runtime")
                        .join("runs")
                        .join(&run_id)
                        .join("logs")
                        .join("alphora.log"),
                    root.join("logs")
                        .join("runs")
                        .join(&run_id)
                        .join("logs")
                        .join("alphora.log"),
                ];
                let mut path: Option<PathBuf> = None;
                let mut position = 0_u64;
                while !flag.load(Ordering::Relaxed) {
                    if path.is_none() {
                        path = candidates.iter().find(|p| p.is_file()).cloned();
                    }
                    if let Some(current) = &path {
                        match read_increment(current, position, Self::MAX_BYTES_PER_POLL) {
                            Some((next, entries)) => {
                                position = next;
                                if !entries.is_empty() {
                                    if tx.send(entries).is_err() {
                                        return;
                                    }
                                    ctx.request_repaint();
                                }
                            }
                            None => {
                                path = None;
                                position = 0;
                            }
                        }
                    }
                    thread::sleep(Duration::from_millis(900));
                }
            })
            .ok();
        Self { rx, stop }
    }

    pub fn drain(&self) -> Vec<(Level, String)> {
        self.rx.try_iter().flatten().collect()
    }
}

impl Drop for LogTail {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

/// Reads at most `max_bytes` past `position`, stopping at the last complete line.
fn read_increment(path: &Path, position: u64, max_bytes: u64) -> Option<(u64, Vec<(Level, String)>)> {
    let size = std::fs::metadata(path).ok()?.len();
    let position = if size < position { 0 } else { position };
    if size == position {
        return Some((position, Vec::new()));
    }
    let mut file = File::open(path).ok()?;
    file.seek(SeekFrom::Start(position)).ok()?;
    let mut raw = Vec::new();
    file.take(max_bytes).read_to_end(&mut raw).ok()?;
    let mut consumed = raw.len() as u64;
    if position + consumed < size {
        if let Some(cut) = raw.iter().rposition(|&b| b == b'\n') {
            raw.truncate(cut + 1);
            consumed = raw.len() as u64;
        }
    }
    let entries = String::from_utf8_lossy(&raw)
        .lines()
        .filter_map(classify_engine_line)
        .collect();
    Some((position + consumed, entries))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn warnings_are_compacted_and_info_is_filtered() {
        let line = "2026-01-02 10:11:12,123 | engine | WARNING | governor | Saldo baixo";
        assert_eq!(
            classify_engine_line(line),
            Some((Level::Warning, "governor | Saldo baixo".into()))
        );
        assert_eq!(
            classify_engine_line("2026-01-02 | x | INFO | y | detalhe qualquer"),
            None
        );
        assert_eq!(
            classify_engine_line("Alphora Governor iniciado").map(|(level, _)| level),
            Some(Level::Info)
        );
    }

    #[test]
    fn benign_timestamp_retries_are_hidden() {
        let line = "x | WARNING | Retry | policy=binance_timestamp_resync binance_code=-4046";
        assert_eq!(classify_engine_line(line), None);
    }

    #[test]
    fn buffer_splits_multiline_messages_and_respects_capacity() {
        let mut buffer = LogBuffer::new(3);
        buffer.info("ui", "a\nb");
        buffer.warn("ui", "c\nd");
        assert_eq!(buffer.iter().count(), 3);
        assert_eq!(buffer.get(0).unwrap().message, "b");
        assert!(buffer.get(2).unwrap().continuation);
        assert!(buffer.to_plain_text().contains("[AVISO]"));
        assert_eq!(buffer.counts(), (1, 0));
        buffer.error("ui", "x");
        assert_eq!(buffer.counts(), (1, 1));
        buffer.clear();
        assert_eq!(buffer.counts(), (0, 0));
    }

    #[test]
    fn dedupe_admits_once() {
        let mut dedupe = Dedupe::new(2);
        assert!(dedupe.admit(Level::Info, "x"));
        assert!(!dedupe.admit(Level::Info, "x"));
        assert!(dedupe.admit(Level::Error, "x"));
    }

    #[test]
    fn increments_stop_at_last_complete_line() {
        let path = std::env::temp_dir().join(format!("alphora-tail-{}.log", std::process::id()));
        std::fs::write(&path, "a | WARNING | one\nb | ERROR | two\npartial").unwrap();
        let (position, entries) = read_increment(&path, 0, 20).unwrap();
        assert_eq!(position, 18);
        assert_eq!(entries.len(), 1);
        let (_, rest) = read_increment(&path, position, 1024).unwrap();
        assert_eq!(rest[0].0, Level::Error);
        std::fs::remove_file(path).ok();
    }
}
