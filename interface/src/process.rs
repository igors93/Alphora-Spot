//! Child process supervision.
//!
//! Every child runs in its own process tree container: a Windows Job Object with
//! `KILL_ON_JOB_CLOSE`, or a POSIX process group. Stopping kills the whole tree, and if
//! the interface itself dies the operating system closes the job and reaps every child,
//! so no engine, backtest or training process can be orphaned.

use std::io::{self, BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;
use std::time::{Duration, Instant};

/// Longest log line kept from a child's output; protects memory from runaway writers.
pub const MAX_LINE_BYTES: usize = 16 * 1024;

/// Longest bridge protocol message. Replies carry journal tails and diagnostics of
/// hundreds of KB; a truncated message is invalid JSON and its request would hang until
/// it times out, so this limit only guards against a runaway writer.
pub const MAX_PROTOCOL_LINE_BYTES: usize = 64 * 1024 * 1024;

#[derive(Clone, Debug)]
pub struct Spawn {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub env: Vec<(String, String)>,
}

impl Spawn {
    /// Builds a spec from a `[program, args...]` list produced by the Python core.
    pub fn from_command(command: &[String], cwd: PathBuf, env: Vec<(String, String)>) -> Option<Self> {
        let (program, args) = command.split_first()?;
        Some(Self {
            program: PathBuf::from(program),
            args: args.to_vec(),
            cwd,
            env,
        })
    }

    pub fn command(&self) -> Command {
        let mut command = Command::new(&self.program);
        command
            .args(&self.args)
            .current_dir(&self.cwd)
            .envs(self.env.iter().map(|(k, v)| (k.as_str(), v.as_str())));
        hide_console(&mut command);
        isolate_group(&mut command);
        command
    }
}

#[derive(Debug)]
pub enum ProcEvent {
    Line(String),
    Exited { code: i32, stop_requested: bool },
}

/// A supervised child whose stdout and stderr are merged into one ordered line stream.
pub struct ManagedProcess {
    pid: u32,
    events: Receiver<ProcEvent>,
    tree: ProcessTree,
    stop_requested: Arc<AtomicBool>,
    started: Instant,
    finished: bool,
}

impl ManagedProcess {
    pub fn spawn(spec: &Spawn, ctx: &egui::Context) -> io::Result<Self> {
        let (reader, writer) = io::pipe()?;
        let child = {
            let mut command = spec.command();
            command
                .stdin(Stdio::null())
                .stdout(Stdio::from(writer.try_clone()?))
                .stderr(Stdio::from(writer));
            command.spawn()?
            // `command` drops here, closing our copies of the write end so the reader
            // sees EOF once the child tree exits.
        };
        let pid = child.id();
        let tree = ProcessTree::attach(&child);
        let stop_requested = Arc::new(AtomicBool::new(false));
        let (tx, rx) = mpsc::channel();
        start_supervision(child, reader, tx, stop_requested.clone(), ctx.clone());
        Ok(Self {
            pid,
            events: rx,
            tree,
            stop_requested,
            started: Instant::now(),
            finished: false,
        })
    }

    pub fn pid(&self) -> u32 {
        self.pid
    }

    pub fn uptime(&self) -> Duration {
        self.started.elapsed()
    }

    /// Drains pending output and exit notifications without blocking.
    pub fn poll(&mut self) -> Vec<ProcEvent> {
        let events: Vec<ProcEvent> = self.events.try_iter().collect();
        if events.iter().any(|e| matches!(e, ProcEvent::Exited { .. })) {
            self.finished = true;
        }
        events
    }

    /// Marks the exit as user-requested (graceful stop handled by the caller).
    pub fn mark_stop_requested(&self) {
        self.stop_requested.store(true, Ordering::SeqCst);
    }

    /// Kills the whole process tree immediately.
    pub fn kill(&self) {
        self.mark_stop_requested();
        self.tree.kill();
    }
}

impl Drop for ManagedProcess {
    fn drop(&mut self) {
        if !self.finished {
            self.kill();
        }
    }
}

fn start_supervision(
    mut child: Child,
    reader: io::PipeReader,
    tx: Sender<ProcEvent>,
    stop_requested: Arc<AtomicBool>,
    ctx: egui::Context,
) {
    let (done_tx, done_rx) = mpsc::channel::<()>();
    let line_tx = tx.clone();
    let line_ctx = ctx.clone();
    thread::Builder::new()
        .name("alphora-child-output".into())
        .spawn(move || {
            for_each_line(reader, MAX_LINE_BYTES, |line| {
                if line_tx.send(ProcEvent::Line(line)).is_err() {
                    return false;
                }
                line_ctx.request_repaint();
                true
            });
            let _ = done_tx.send(());
        })
        .ok();

    thread::Builder::new()
        .name("alphora-child-wait".into())
        .spawn(move || {
            let code = match child.wait() {
                Ok(status) => status.code().unwrap_or(-1),
                Err(_) => -1,
            };
            // Deliver every output line before the exit event; a grandchild that keeps
            // the pipe open must not hold the exit notification forever.
            let _ = done_rx.recv_timeout(Duration::from_secs(3));
            let _ = tx.send(ProcEvent::Exited {
                code,
                stop_requested: stop_requested.load(Ordering::SeqCst),
            });
            ctx.request_repaint();
        })
        .ok();
}

/// Reads `\n`-terminated lines as lossy UTF-8 until EOF or until `sink` returns false.
/// Lines longer than `max_bytes` are cut to that length.
pub fn for_each_line(source: impl Read, max_bytes: usize, mut sink: impl FnMut(String) -> bool) {
    let mut reader = BufReader::with_capacity(64 * 1024, source);
    let mut buffer = Vec::with_capacity(256);
    loop {
        buffer.clear();
        match reader.read_until(b'\n', &mut buffer) {
            Ok(0) | Err(_) => return,
            Ok(_) => {
                while matches!(buffer.last(), Some(b'\n' | b'\r')) {
                    buffer.pop();
                }
                buffer.truncate(max_bytes);
                if !sink(String::from_utf8_lossy(&buffer).into_owned()) {
                    return;
                }
            }
        }
    }
}

#[cfg(windows)]
fn hide_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_console(_command: &mut Command) {}

#[cfg(unix)]
fn isolate_group(command: &mut Command) {
    use std::os::unix::process::CommandExt;
    command.process_group(0);
}

#[cfg(not(unix))]
fn isolate_group(_command: &mut Command) {}

/// Handle able to kill a child together with everything it spawned.
pub struct ProcessTree {
    pid: u32,
    #[cfg(windows)]
    job: Option<job::JobObject>,
}

impl ProcessTree {
    pub fn attach(child: &Child) -> Self {
        Self {
            pid: child.id(),
            #[cfg(windows)]
            job: job::JobObject::for_child(child),
        }
    }

    #[cfg(windows)]
    pub fn kill(&self) {
        if let Some(job) = &self.job {
            if job.terminate() {
                return;
            }
        }
        let mut command = Command::new("taskkill");
        command
            .args(["/PID", &self.pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        hide_console(&mut command);
        let _ = command.status();
    }

    #[cfg(unix)]
    pub fn kill(&self) {
        // SAFETY: plain syscall; a negative pid targets the process group we created.
        unsafe {
            libc::kill(-(self.pid as i32), libc::SIGKILL);
        }
    }
}

#[cfg(windows)]
mod job {
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;

    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation, SetInformationJobObject,
        TerminateJobObject,
    };

    pub struct JobObject(HANDLE);

    // SAFETY: a job handle is a kernel object reference usable from any thread.
    unsafe impl Send for JobObject {}
    unsafe impl Sync for JobObject {}

    impl JobObject {
        pub fn for_child(child: &Child) -> Option<Self> {
            // SAFETY: all pointers passed are valid for the duration of each call and
            // the handle is owned by the returned value (closed on drop).
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle.is_null() {
                    return None;
                }
                let job = Self(handle);
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let configured = SetInformationJobObject(
                    job.0,
                    JobObjectExtendedLimitInformation,
                    (&raw const info).cast(),
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if configured == 0 {
                    return None;
                }
                let assigned = AssignProcessToJobObject(job.0, child.as_raw_handle() as HANDLE);
                (assigned != 0).then_some(job)
            }
        }

        pub fn terminate(&self) -> bool {
            // SAFETY: the handle is valid while `self` lives.
            unsafe { TerminateJobObject(self.0, 1) != 0 }
        }
    }

    impl Drop for JobObject {
        fn drop(&mut self) {
            // SAFETY: closing our own handle once; KILL_ON_JOB_CLOSE reaps leftovers.
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lines_are_split_trimmed_and_capped() {
        let long = "x".repeat(MAX_LINE_BYTES + 50);
        let input = format!("first\r\nsecond\n{long}\nlast-without-newline");
        let mut lines = Vec::new();
        for_each_line(input.as_bytes(), MAX_LINE_BYTES, |line| {
            lines.push(line);
            true
        });
        assert_eq!(lines[0], "first");
        assert_eq!(lines[1], "second");
        assert_eq!(lines[2].len(), MAX_LINE_BYTES);
        assert_eq!(lines[3], "last-without-newline");
    }

    #[test]
    fn invalid_utf8_is_replaced_not_dropped() {
        let mut lines = Vec::new();
        for_each_line(&b"ok \xff end\n"[..], MAX_LINE_BYTES, |line| {
            lines.push(line);
            true
        });
        assert_eq!(lines, vec!["ok \u{fffd} end".to_string()]);
    }

    #[test]
    fn protocol_messages_larger_than_a_log_line_arrive_intact() {
        // Regression: replies over 16 KB (journal tails, diagnostics) used to be cut to
        // the log-line limit, became invalid JSON and their requests timed out.
        let big = "j".repeat(MAX_LINE_BYTES * 40);
        let message = format!("{{\"id\":7,\"ok\":true,\"result\":{{\"journal_text\":\"{big}\"}}}}");
        let mut lines = Vec::new();
        for_each_line(
            format!("{message}\n").as_bytes(),
            MAX_PROTOCOL_LINE_BYTES,
            |line| {
                lines.push(line);
                true
            },
        );
        assert_eq!(lines, vec![message.clone()]);
        let parsed: serde_json::Value = serde_json::from_str(&lines[0]).expect("valid JSON");
        assert_eq!(
            parsed["result"]["journal_text"].as_str().map(str::len),
            Some(big.len())
        );
    }

    #[test]
    fn managed_process_streams_output_and_exit_code() {
        let ctx = egui::Context::default();
        let (program, args): (&str, Vec<String>) = if cfg!(windows) {
            (
                "cmd",
                vec!["/C".into(), "echo hello& echo boom 1>&2& exit 3".into()],
            )
        } else {
            (
                "sh",
                vec!["-c".into(), "echo hello; echo boom >&2; exit 3".into()],
            )
        };
        let spec = Spawn {
            program: program.into(),
            args,
            cwd: std::env::temp_dir(),
            env: vec![],
        };
        let mut process = ManagedProcess::spawn(&spec, &ctx).expect("spawn");
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut lines = Vec::new();
        let mut exit = None;
        while exit.is_none() && Instant::now() < deadline {
            for event in process.poll() {
                match event {
                    ProcEvent::Line(line) => lines.push(line.trim().to_string()),
                    ProcEvent::Exited { code, stop_requested } => exit = Some((code, stop_requested)),
                }
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(exit, Some((3, false)));
        assert!(lines.contains(&"hello".to_string()));
        assert!(lines.contains(&"boom".to_string()));
    }

    #[test]
    fn kill_terminates_the_tree_and_reports_stop() {
        let ctx = egui::Context::default();
        let (program, args): (&str, Vec<String>) = if cfg!(windows) {
            ("cmd", vec!["/C".into(), "ping -n 30 127.0.0.1 >NUL".into()])
        } else {
            ("sh", vec!["-c".into(), "sleep 30".into()])
        };
        let spec = Spawn {
            program: program.into(),
            args,
            cwd: std::env::temp_dir(),
            env: vec![],
        };
        let mut process = ManagedProcess::spawn(&spec, &ctx).expect("spawn");
        process.kill();
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut stopped = None;
        while stopped.is_none() && Instant::now() < deadline {
            for event in process.poll() {
                if let ProcEvent::Exited { stop_requested, .. } = event {
                    stopped = Some(stop_requested);
                }
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(stopped, Some(true));
    }
}
