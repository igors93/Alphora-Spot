//! Client for `python -m desktop.bridge` (JSON lines over stdio).
//!
//! Requests are fire-and-forget from the UI thread; replies are drained once per frame
//! and routed back to the page that asked, tagged by [`Route`]. Every request has a
//! deadline, and a crashed core is restarted automatically with back-off.

use std::collections::{HashMap, VecDeque};
use std::io::Write;
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::{Value, json};

use crate::process::{MAX_LINE_BYTES, MAX_PROTOCOL_LINE_BYTES, ProcessTree, Spawn, for_each_line};
use crate::workspace::Workspace;

const DEFAULT_TIMEOUT: Duration = Duration::from_secs(45);
const STDERR_TAIL: usize = 300;
const MAX_AUTO_RESTARTS: u32 = 3;
const BRIDGE_PROTOCOL_VERSION: u32 = 1;

fn protocol_mismatch_error(actual: u32) -> Option<String> {
    (actual != BRIDGE_PROTOCOL_VERSION).then(|| {
        format!(
            "Versão incompatível da ponte: interface {}, núcleo Python {}. Atualize ambos para a mesma versão.",
            BRIDGE_PROTOCOL_VERSION, actual
        )
    })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum PageId {
    Operation,
    Symbols,
    Strategy,
    Intelligence,
    Training,
    Backtest,
    Observability,
    Credentials,
}

#[derive(Clone, Copy, Debug)]
pub struct Route {
    pub page: PageId,
    pub kind: &'static str,
}

pub const fn route(page: PageId, kind: &'static str) -> Route {
    Route { page, kind }
}

#[derive(Clone, Debug)]
pub struct Failure {
    pub title: String,
    pub message: String,
}

impl Failure {
    pub fn new(title: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            title: title.into(),
            message: message.into(),
        }
    }
}

pub struct Reply {
    pub route: Route,
    pub result: Result<Value, Failure>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Status {
    Starting,
    Ready,
    Down(String),
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Hello {
    pub protocol: u32,
    pub root: String,
    pub run_id: String,
    pub python: String,
    pub python_version: String,
    pub options: Options,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Options {
    pub execution_modes: Vec<String>,
    pub environments: Vec<String>,
    pub strategy_modes: Vec<String>,
    pub symbol_scopes: Vec<String>,
    pub event_severities: Vec<String>,
    pub credential_environments: Vec<String>,
    pub timeframes: Vec<Timeframe>,
    pub activation_levels: HashMap<String, Vec<String>>,
    pub modules: Vec<ModuleField>,
    pub probability_fields: Vec<Field>,
    pub portfolio_fields: Vec<Field>,
    pub profile_choices: HashMap<String, Vec<String>>,
}

impl Options {
    pub fn choices(&self, key: &str) -> &[String] {
        self.profile_choices.get(key).map(Vec::as_slice).unwrap_or(&[])
    }

    pub fn activation_levels(&self, execution_mode: &str) -> &[String] {
        self.activation_levels
            .get(execution_mode)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    pub fn timeframe_minutes(&self, value: &str) -> Option<u64> {
        self.timeframes
            .iter()
            .find(|t| t.value == value)
            .map(|t| t.minutes)
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Timeframe {
    pub value: String,
    pub label: String,
    pub minutes: u64,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Field {
    pub key: String,
    pub label: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct ModuleField {
    pub key: String,
    pub label: String,
    pub options: Vec<ModuleOption>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct ModuleOption {
    pub value: String,
    pub label: String,
    pub description: String,
}

enum Incoming {
    Message(Value),
    Closed,
}

struct Connection {
    stdin: ChildStdin,
    incoming: Receiver<Incoming>,
    child: Child,
    tree: ProcessTree,
}

impl Drop for Connection {
    fn drop(&mut self) {
        self.tree.kill();
        let _ = self.child.wait();
    }
}

struct Pending {
    route: Route,
    method: String,
    deadline: Instant,
}

pub struct Bridge {
    workspace: Workspace,
    ctx: egui::Context,
    connection: Option<Connection>,
    status: Status,
    hello: Option<Hello>,
    next_id: u64,
    pending: HashMap<u64, Pending>,
    local: VecDeque<Reply>,
    stderr: Arc<Mutex<VecDeque<String>>>,
    restarts: u32,
    restart_at: Option<Instant>,
    /// Incremented each time a fresh core finishes its handshake.
    generation: u64,
}

impl Bridge {
    pub fn start(workspace: Workspace, ctx: egui::Context) -> Self {
        let mut bridge = Self {
            workspace,
            ctx,
            connection: None,
            status: Status::Starting,
            hello: None,
            next_id: 1,
            pending: HashMap::new(),
            local: VecDeque::new(),
            stderr: Arc::new(Mutex::new(VecDeque::new())),
            restarts: 0,
            restart_at: None,
            generation: 0,
        };
        bridge.connect();
        bridge
    }

    pub fn status(&self) -> &Status {
        &self.status
    }

    pub fn is_ready(&self) -> bool {
        self.status == Status::Ready
    }

    pub fn hello(&self) -> Option<&Hello> {
        self.hello.as_ref()
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn ctx(&self) -> egui::Context {
        self.ctx.clone()
    }

    pub fn workspace(&self) -> &Workspace {
        &self.workspace
    }

    /// Workspace root as reported by the core (authoritative once ready).
    pub fn root(&self) -> PathBuf {
        match &self.hello {
            Some(hello) if !hello.root.is_empty() => PathBuf::from(&hello.root),
            _ => self.workspace.working_dir(),
        }
    }

    /// Environment for children of the core: interpreter paths, run id, project root.
    pub fn child_env(&self) -> Vec<(String, String)> {
        self.workspace.base_env(&self.root())
    }

    pub fn stderr_tail(&self) -> Vec<String> {
        self.stderr
            .lock()
            .map(|tail| tail.iter().cloned().collect())
            .unwrap_or_default()
    }

    pub fn call(&mut self, route: Route, method: &str, params: Value) {
        self.call_with_timeout(route, method, params, DEFAULT_TIMEOUT);
    }

    pub fn call_with_timeout(&mut self, route: Route, method: &str, params: Value, timeout: Duration) {
        if self.status != Status::Ready {
            self.local.push_back(Reply {
                route,
                result: Err(Failure::new(
                    "Núcleo indisponível",
                    "O núcleo Python ainda não está pronto. Aguarde a conexão e tente novamente.",
                )),
            });
            self.ctx.request_repaint();
            return;
        }
        let id = self.next_id;
        self.next_id += 1;
        let line = json!({"id": id, "method": method, "params": params}).to_string();
        let written = self.connection.as_mut().is_some_and(|connection| {
            writeln!(connection.stdin, "{line}")
                .and_then(|()| connection.stdin.flush())
                .is_ok()
        });
        if !written {
            self.local.push_back(Reply {
                route,
                result: Err(Failure::new(
                    "Núcleo indisponível",
                    "A conexão com o núcleo Python foi perdida.",
                )),
            });
            self.mark_down("A conexão com o núcleo Python foi perdida.".into());
            return;
        }
        self.pending.insert(
            id,
            Pending {
                route,
                method: method.to_owned(),
                deadline: Instant::now() + timeout,
            },
        );
    }

    /// Drains replies; must be called once per frame on the UI thread.
    pub fn poll(&mut self) -> Vec<Reply> {
        let mut replies: Vec<Reply> = self.local.drain(..).collect();
        let mut closed = false;
        let incoming: Vec<Incoming> = self
            .connection
            .as_ref()
            .map(|connection| connection.incoming.try_iter().collect())
            .unwrap_or_default();
        {
            for incoming in incoming {
                match incoming {
                    Incoming::Closed => closed = true,
                    Incoming::Message(message) => {
                        if let Some(reply) = self.accept(message) {
                            replies.push(reply);
                        }
                    }
                }
            }
        }
        if closed {
            let detail = self
                .stderr_tail()
                .iter()
                .rev()
                .find(|line| !line.trim().is_empty())
                .cloned()
                .unwrap_or_else(|| "o processo terminou inesperadamente".into());
            self.mark_down(format!("O núcleo Python foi encerrado: {detail}"));
            if self.restarts < MAX_AUTO_RESTARTS {
                let delay = Duration::from_millis(800 * u64::from(self.restarts + 1));
                self.restart_at = Some(Instant::now() + delay);
                self.ctx.request_repaint_after(delay);
            }
        }

        let now = Instant::now();
        let expired: Vec<u64> = self
            .pending
            .iter()
            .filter(|(_, pending)| pending.deadline <= now)
            .map(|(id, _)| *id)
            .collect();
        for id in expired {
            if let Some(pending) = self.pending.remove(&id) {
                replies.push(Reply {
                    route: pending.route,
                    result: Err(Failure::new(
                        "Tempo esgotado",
                        format!(
                            "O núcleo Python não respondeu a tempo ({}). A operação foi abandonada.",
                            pending.method
                        ),
                    )),
                });
            }
        }
        if !self.pending.is_empty() {
            self.ctx.request_repaint_after(Duration::from_millis(500));
        }
        if self.restart_at.is_some_and(|at| at <= now) {
            self.restart_at = None;
            self.restarts += 1;
            self.connect();
        }
        replies
    }

    /// Manual restart requested by the user; resets the automatic retry budget.
    pub fn restart(&mut self) {
        self.restarts = 0;
        self.restart_at = None;
        self.connect();
    }

    fn accept(&mut self, message: Value) -> Option<Reply> {
        if message.get("event").and_then(Value::as_str) == Some("ready") {
            match serde_json::from_value::<Hello>(message.get("data").cloned().unwrap_or(Value::Null)) {
                Ok(hello) => {
                    if let Some(error) = protocol_mismatch_error(hello.protocol) {
                        self.mark_down(error);
                        return None;
                    }
                    self.hello = Some(hello);
                    self.status = Status::Ready;
                    self.generation += 1;
                }
                Err(error) => self.mark_down(format!("Handshake inválido do núcleo Python: {error}")),
            }
            return None;
        }
        let id = message.get("id")?.as_u64()?;
        let pending = self.pending.remove(&id)?;
        let ok = message.get("ok").and_then(Value::as_bool).unwrap_or(false);
        let result = if ok {
            Ok(message.get("result").cloned().unwrap_or(Value::Null))
        } else {
            let error = message.get("error");
            let field = |name: &str| {
                error
                    .and_then(|e| e.get(name))
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string()
            };
            Err(Failure::new(field("title"), field("message")))
        };
        Some(Reply {
            route: pending.route,
            result,
        })
    }

    fn mark_down(&mut self, reason: String) {
        self.status = Status::Down(reason);
        self.connection = None;
        for (_, pending) in self.pending.drain() {
            self.local.push_back(Reply {
                route: pending.route,
                result: Err(Failure::new(
                    "Núcleo reiniciado",
                    "A requisição foi interrompida porque o núcleo Python foi encerrado.",
                )),
            });
        }
        self.ctx.request_repaint();
    }

    fn connect(&mut self) {
        self.connection = None;
        self.hello = None;
        self.status = Status::Starting;
        let cwd = self.workspace.working_dir();
        let spawn = Spawn {
            program: self.workspace.python.clone(),
            args: vec!["-u".into(), "-m".into(), "desktop.bridge".into()],
            env: self.workspace.base_env(&cwd),
            cwd,
        };
        match self.spawn(&spawn) {
            Ok(connection) => self.connection = Some(connection),
            Err(error) => {
                self.status = Status::Down(format!(
                    "Não foi possível iniciar o Python ({}): {error}. \
                     Instale as dependências do projeto ou defina ALPHORA_PYTHON.",
                    self.workspace.python.display()
                ));
            }
        }
        self.ctx.request_repaint();
    }

    fn spawn(&self, spec: &Spawn) -> std::io::Result<Connection> {
        let mut command = spec.command();
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn()?;
        let tree = ProcessTree::attach(&child);
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| std::io::Error::other("stdin indisponível"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| std::io::Error::other("stdout indisponível"))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| std::io::Error::other("stderr indisponível"))?;

        let (tx, rx) = mpsc::channel();
        let ctx = self.ctx.clone();
        let protocol_errors = self.stderr.clone();
        thread::Builder::new()
            .name("alphora-bridge-read".into())
            .spawn(move || {
                for_each_line(stdout, MAX_PROTOCOL_LINE_BYTES, |line| {
                    match serde_json::from_str::<Value>(&line) {
                        Ok(message) => {
                            if tx.send(Incoming::Message(message)).is_err() {
                                return false;
                            }
                            ctx.request_repaint();
                        }
                        // Never drop a message silently: its request would only fail
                        // later as a timeout, with no trace of the real cause.
                        Err(error) => {
                            if let Ok(mut tail) = protocol_errors.lock() {
                                if tail.len() >= STDERR_TAIL {
                                    tail.pop_front();
                                }
                                tail.push_back(format!(
                                    "Mensagem do núcleo ignorada ({} bytes): {error}",
                                    line.len()
                                ));
                            }
                        }
                    }
                    true
                });
                let _ = tx.send(Incoming::Closed);
                ctx.request_repaint();
            })?;

        let tail = self.stderr.clone();
        thread::Builder::new()
            .name("alphora-bridge-stderr".into())
            .spawn(move || {
                for_each_line(stderr, MAX_LINE_BYTES, |line| {
                    if let Ok(mut tail) = tail.lock() {
                        if tail.len() >= STDERR_TAIL {
                            tail.pop_front();
                        }
                        tail.push_back(line);
                    }
                    true
                });
            })?;

        Ok(Connection {
            stdin,
            incoming: rx,
            child,
            tree,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::protocol_mismatch_error;

    #[test]
    fn handshake_rejects_missing_or_different_protocol() {
        assert!(protocol_mismatch_error(1).is_none());
        assert!(
            protocol_mismatch_error(0)
                .unwrap()
                .contains("interface 1, núcleo Python 0")
        );
        assert!(
            protocol_mismatch_error(2)
                .unwrap()
                .contains("interface 1, núcleo Python 2")
        );
    }
}
