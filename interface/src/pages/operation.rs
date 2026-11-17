//! Engine lifecycle (start chain, preflight, graceful stop) and the dashboard view.

use std::collections::VecDeque;
use std::time::{Duration, Instant};

use egui::{RichText, Sense, Ui, vec2};
use serde_json::{Value, json};

use super::observability::Event;
use super::{Shell, bool_of, display, format_duration, list_of, parse_f64, str_of, strings_of};
use crate::bridge::{Failure, PageId};
use crate::commands::{Command, Workspace};
use crate::dialogs::{ConfirmationDialog, ConfirmationRequest, Outcome};
use crate::icons::{self, Icon};
use crate::logs::{Dedupe, Level, LogBuffer, LogTail, classify_engine_line};
use crate::process::{ManagedProcess, ProcEvent, Spawn};
use crate::theme::{self, Tone};
use crate::widgets::{self, Cell, Column, Kind};

const PAGE: PageId = PageId::Operation;
const GRACEFUL_STOP: Duration = Duration::from_secs(15);
const PREFLIGHT_TIMEOUT: Duration = Duration::from_secs(150);
const ROWS_INTERVAL: Duration = Duration::from_secs(3);
const AUDIT_FLUSH: Duration = Duration::from_secs(1);

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Phase {
    Idle,
    Preparing,
    Confirming,
    Preflight,
    Running,
    Stopping,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Stage {
    Operation,
    Preflight,
}

#[derive(Default)]
struct Overview {
    strategy: String,
    strategy_mode: String,
    loop_interval: String,
    symbols: Vec<String>,
    ai_enabled: bool,
    credentials: Value,
}

pub struct OperationPage {
    phase: Phase,
    mode: String,
    environment: String,
    overview: Option<Overview>,
    seen_rev: u64,
    overview_loading: bool,

    queue: VecDeque<ConfirmationRequest>,
    responses: Vec<Value>,
    stage: Stage,
    dialog: Option<ConfirmationDialog>,

    process: Option<ManagedProcess>,
    tail: Option<LogTail>,
    stop_deadline: Option<Instant>,
    dedupe: Dedupe,
    audit_batch: Vec<String>,
    audit_flushed: Instant,

    status: String,
    status_tone: Tone,
    last_error: String,
    logs: LogBuffer,

    rows: Vec<Value>,
    rows_state: Value,
    rows_loaded: bool,
    rows_loading: bool,
    rows_next: Instant,
}

impl OperationPage {
    pub fn new() -> Self {
        let mut logs = LogBuffer::new(4000);
        logs.info("interface", "Interface iniciada.");
        Self {
            phase: Phase::Idle,
            mode: "DRY_RUN".into(),
            environment: "TESTNET".into(),
            overview: None,
            seen_rev: u64::MAX,
            overview_loading: false,
            queue: VecDeque::new(),
            responses: Vec::new(),
            stage: Stage::Operation,
            dialog: None,
            process: None,
            tail: None,
            stop_deadline: None,
            dedupe: Dedupe::new(30),
            audit_batch: Vec::new(),
            audit_flushed: Instant::now(),
            status: "Parado".into(),
            status_tone: Tone::Neutral,
            last_error: String::new(),
            logs,
            rows: Vec::new(),
            rows_state: Value::Null,
            rows_loaded: false,
            rows_loading: false,
            rows_next: Instant::now(),
        }
    }

    // ------------------------------------------------------------ queries

    pub fn is_running(&self) -> bool {
        self.process.is_some()
    }

    pub fn is_busy(&self) -> bool {
        !matches!(self.phase, Phase::Idle | Phase::Running)
    }

    pub fn can_start(&self) -> bool {
        self.phase == Phase::Idle && self.process.is_none()
    }

    pub fn status(&self) -> (&str, Tone) {
        (&self.status, self.status_tone)
    }

    pub fn mode(&self) -> &str {
        &self.mode
    }

    pub fn environment(&self) -> &str {
        &self.environment
    }

    pub fn uptime(&self) -> Option<Duration> {
        self.process.as_ref().map(ManagedProcess::uptime)
    }

    pub fn logs(&self) -> &LogBuffer {
        &self.logs
    }

    pub fn clear_logs(&mut self) {
        self.logs.clear();
        self.dedupe.clear();
    }

    /// Last-resort shutdown when the window closes anyway.
    pub fn kill(&mut self) {
        if let Some(process) = &self.process {
            process.kill();
        }
    }

    // ------------------------------------------------------------ replies

    pub fn on_reply(&mut self, kind: &str, result: Result<Value, Failure>, shell: &mut Shell) {
        match kind {
            "overview" => {
                self.overview_loading = false;
                match result {
                    Ok(value) => self.apply_overview(&value),
                    Err(failure) => shell.toasts.failure(&failure),
                }
            }
            "rows" => {
                self.rows_loading = false;
                if let Ok(value) = result {
                    self.rows = list_of(&value, "rows");
                    self.rows_state = value.get("state").cloned().unwrap_or(Value::Null);
                    self.rows_loaded = true;
                }
            }
            "prepare" => match result {
                Ok(value) => self.on_prepared(&value, shell),
                Err(failure) => self.fail_start(&failure.title, &failure.message, shell),
            },
            "authorize" | "confirm_preflight" => match result {
                Ok(value) => self.on_stage(&value, shell),
                Err(failure) => self.fail_start(&failure.title, &failure.message, shell),
            },
            _ => {}
        }
    }

    fn apply_overview(&mut self, value: &Value) {
        if self.phase == Phase::Idle && self.process.is_none() {
            self.mode = str_of(value, "execution_mode");
            self.environment = str_of(value, "environment");
        }
        self.overview = Some(Overview {
            strategy: str_of(value, "strategy"),
            strategy_mode: str_of(value, "strategy_mode"),
            loop_interval: str_of(value, "loop_interval"),
            symbols: strings_of(value, "symbols"),
            ai_enabled: bool_of(value, "ai_enabled"),
            credentials: value.get("credentials").cloned().unwrap_or(Value::Null),
        });
    }

    fn on_prepared(&mut self, value: &Value, shell: &mut Shell) {
        self.log_messages(value, "operação");
        if str_of(value, "status") == "BLOCKED" {
            self.fail_start(&str_of(value, "title"), &str_of(value, "message"), shell);
            return;
        }
        self.queue = list_of(value, "confirmations")
            .into_iter()
            .filter_map(|item| serde_json::from_value(item).ok())
            .collect();
        self.responses.clear();
        self.stage = Stage::Operation;
        self.advance(shell);
    }

    fn on_stage(&mut self, value: &Value, shell: &mut Shell) {
        self.log_preflight_output(&str_of(value, "output"));
        self.log_messages(value, "preflight");
        if bool_of(value, "profiles_changed") {
            shell.saved();
        }
        match str_of(value, "stage").as_str() {
            "launch" => self.launch(value, shell),
            "confirm" => match value.get("confirmation").cloned().map(serde_json::from_value) {
                Some(Ok(request)) => {
                    self.queue = VecDeque::from([request]);
                    self.responses.clear();
                    self.stage = Stage::Preflight;
                    self.advance(shell);
                }
                _ => self.fail_start("Preflight inválido", "Confirmação do preflight ilegível.", shell),
            },
            _ => self.fail_start(&str_of(value, "title"), &str_of(value, "message"), shell),
        }
    }

    // ------------------------------------------------------------ actions

    pub fn start(&mut self, shell: &mut Shell) {
        if !self.can_start() {
            return;
        }
        if !shell.bridge.is_ready() {
            shell
                .toasts
                .warn("Núcleo indisponível", "Aguarde a conexão com o núcleo Python.");
            return;
        }
        self.last_error.clear();
        self.set_status("Preparando operação", Tone::Warning);
        self.phase = Phase::Preparing;
        self.logs.info(
            "operação",
            &format!("Preparando início em {} / {}.", self.mode, self.environment),
        );
        shell.call(
            PAGE,
            "prepare",
            "operation.prepare",
            json!({"execution_mode": self.mode, "environment": self.environment}),
        );
    }

    pub fn stop(&mut self, shell: &mut Shell) {
        let Some(process) = &self.process else { return };
        if self.phase == Phase::Stopping {
            return;
        }
        let request = shell.root().join("runtime").join("governor_stop.request");
        let written = request
            .parent()
            .map(std::fs::create_dir_all)
            .transpose()
            .and_then(|_| std::fs::write(&request, std::process::id().to_string()));
        process.mark_stop_requested();
        match written {
            Ok(()) => {
                self.phase = Phase::Stopping;
                self.stop_deadline = Some(Instant::now() + GRACEFUL_STOP);
                self.set_status("Parando Alphora", Tone::Warning);
                self.logs.warn(
                    "operação",
                    &format!("Parada graciosa solicitada via {}.", request.display()),
                );
            }
            Err(error) => {
                self.logs.error(
                    "operação",
                    &format!("Falha ao solicitar parada graciosa: {error}. Encerrando o processo."),
                );
                process.kill();
            }
        }
    }

    pub fn force_stop(&mut self) {
        if let Some(process) = &self.process {
            self.logs.warn("operação", "Parada forçada pelo operador.");
            process.kill();
        }
    }

    fn advance(&mut self, shell: &mut Shell) {
        if let Some(request) = self.queue.pop_front() {
            self.phase = Phase::Confirming;
            self.set_status("Aguardando confirmação", Tone::Warning);
            self.dialog = Some(ConfirmationDialog::new(request));
            return;
        }
        self.dialog = None;
        self.phase = Phase::Preflight;
        match self.stage {
            Stage::Operation => {
                self.set_status("Executando preflight", Tone::Warning);
                self.logs
                    .info("preflight", "Executando preflight antes de iniciar o Alphora.");
                let responses = std::mem::take(&mut self.responses);
                shell.call_slow(
                    PAGE,
                    "authorize",
                    "operation.authorize",
                    json!({"responses": responses}),
                    PREFLIGHT_TIMEOUT,
                );
            }
            Stage::Preflight => {
                let response = self.responses.pop().unwrap_or(Value::Null);
                self.responses.clear();
                shell.call(
                    PAGE,
                    "confirm_preflight",
                    "operation.confirm_preflight",
                    json!({"response": response}),
                );
            }
        }
    }

    fn launch(&mut self, value: &Value, shell: &mut Shell) {
        let command = strings_of(value, "command");
        let mut env = shell.bridge.child_env();
        if let Some(plan_env) = value.get("env").and_then(Value::as_object) {
            env.extend(
                plan_env
                    .iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap_or_default().to_string())),
            );
        }
        let summary = str_of(value, "summary");
        if !summary.is_empty() {
            self.logs.info("operação", &summary);
        }
        let Some(spec) = Spawn::from_command(&command, shell.root(), env) else {
            self.fail_start(
                "Plano inválido",
                "O núcleo não informou o comando do engine.",
                shell,
            );
            return;
        };
        self.set_status("Iniciando Alphora", Tone::Warning);
        match ManagedProcess::spawn(&spec, &shell.bridge_ctx()) {
            Ok(process) => {
                let pid = process.pid();
                self.process = Some(process);
                self.phase = Phase::Running;
                self.dedupe.clear();
                self.set_status("Rodando", Tone::Success);
                self.logs
                    .info("processo", &format!("Alphora iniciado com PID {pid}."));
                self.tail = Some(LogTail::start(
                    shell.root(),
                    shell.bridge.workspace().run_id.clone(),
                    shell.bridge_ctx(),
                ));
                shell.call(
                    PAGE,
                    "audit",
                    "audit.task",
                    json!({"phase": "started", "kind": "engine", "command": command, "pid": pid}),
                );
                shell.toasts.success(
                    "Alphora iniciado",
                    format!("Engine em {} / {}.", self.mode, self.environment),
                );
                self.rows_next = Instant::now();
            }
            Err(error) => {
                self.fail_start(
                    "Falha ao iniciar",
                    &format!("Não foi possível iniciar o processo: {error}"),
                    shell,
                );
            }
        }
    }

    fn fail_start(&mut self, title: &str, message: &str, shell: &mut Shell) {
        let title = if title.is_empty() {
            "Início bloqueado"
        } else {
            title
        };
        self.last_error = message.to_string();
        self.logs.error("operação", message);
        shell.toasts.error(title, message);
        self.reset_flow(shell, false);
        self.set_status("Bloqueado", Tone::Danger);
    }

    fn reset_flow(&mut self, shell: &mut Shell, notify_core: bool) {
        self.queue.clear();
        self.responses.clear();
        self.dialog = None;
        self.phase = if self.process.is_some() {
            Phase::Running
        } else {
            Phase::Idle
        };
        if notify_core {
            shell.call(PAGE, "cancel", "operation.cancel", json!({}));
        }
    }

    // ------------------------------------------------------------ per frame

    pub fn tick(&mut self, shell: &mut Shell, visible: bool) {
        if let Some(dialog) = &mut self.dialog {
            match dialog.show(&shell.bridge_ctx()) {
                Some(Outcome::Accepted(response)) => {
                    self.responses.push(response);
                    self.dialog = None;
                    self.advance(shell);
                }
                Some(Outcome::Rejected) => {
                    self.logs.warn("operação", "Início cancelado pelo usuário.");
                    self.reset_flow(shell, true);
                    self.set_status("Parado", Tone::Neutral);
                }
                None => {}
            }
        }

        self.pump_process(shell);

        if let Some(deadline) = self.stop_deadline {
            if self.process.is_some() && Instant::now() >= deadline {
                self.logs.warn(
                    "operação",
                    "Tempo da parada graciosa esgotado; encerrando a árvore do processo.",
                );
                if let Some(process) = &self.process {
                    process.kill();
                }
                self.stop_deadline = None;
            } else {
                shell
                    .bridge_ctx()
                    .request_repaint_after(Duration::from_millis(500));
            }
        }

        if !self.audit_batch.is_empty() && self.audit_flushed.elapsed() >= AUDIT_FLUSH {
            let lines = std::mem::take(&mut self.audit_batch);
            shell.call(PAGE, "audit", "audit.output", json!({"lines": lines}));
            self.audit_flushed = Instant::now();
        }

        if !shell.bridge.is_ready() {
            return;
        }
        // Loaded eagerly (not only when visible) so the dashboard opens with data.
        if self.seen_rev != shell.config_rev && !self.overview_loading && self.phase == Phase::Idle {
            self.seen_rev = shell.config_rev;
            self.overview_loading = true;
            shell.call(PAGE, "overview", "operation.overview", json!({}));
        }
        let watching = visible || self.process.is_some() || !self.rows_loaded;
        if watching && !self.rows_loading && Instant::now() >= self.rows_next {
            self.rows_loading = true;
            self.rows_next = Instant::now() + ROWS_INTERVAL;
            shell.call(PAGE, "rows", "symbols.rows", json!({}));
        }
        if watching {
            shell.bridge_ctx().request_repaint_after(ROWS_INTERVAL);
        }
    }

    fn pump_process(&mut self, shell: &mut Shell) {
        let Some(process) = &mut self.process else { return };
        let mut exit = None;
        for event in process.poll() {
            match event {
                ProcEvent::Line(line) => {
                    if let Some((level, message)) = classify_engine_line(&line) {
                        if self.dedupe.admit(level, &message) {
                            self.logs.push(level, "processo", &message);
                        }
                    }
                    if self.audit_batch.len() < 5000 {
                        self.audit_batch.push(line);
                    }
                }
                ProcEvent::Exited { code, stop_requested } => exit = Some((code, stop_requested)),
            }
        }
        if let Some(tail) = &self.tail {
            for (level, message) in tail.drain() {
                if self.dedupe.admit(level, &message) {
                    self.logs.push(level, "processo", &message);
                }
            }
        }
        let Some((code, stop_requested)) = exit else {
            return;
        };

        self.process = None;
        self.tail = None;
        self.stop_deadline = None;
        self.phase = Phase::Idle;
        let status = if code == 0 {
            self.set_status(
                if stop_requested { "Parado" } else { "Finalizado" },
                Tone::Neutral,
            );
            "FINISHED"
        } else if stop_requested {
            self.set_status("Parado", Tone::Neutral);
            "STOPPED"
        } else {
            self.last_error = format!("O processo terminou com código {code}.");
            self.set_status("Erro no processo", Tone::Danger);
            shell
                .toasts
                .error("Alphora encerrado com erro", self.last_error.clone());
            "ERROR"
        };
        let level = if status == "ERROR" {
            Level::Error
        } else if stop_requested {
            Level::Warning
        } else {
            Level::Info
        };
        self.logs.push(
            level,
            "processo",
            &format!("Processo finalizado com código {code}."),
        );
        let lines = std::mem::take(&mut self.audit_batch);
        if !lines.is_empty() {
            shell.call(PAGE, "audit", "audit.output", json!({"lines": lines}));
        }
        shell.call(
            PAGE,
            "audit",
            "audit.task",
            json!({"phase": "finished", "kind": "engine", "exit_code": code, "status": status}),
        );
        self.seen_rev = u64::MAX;
    }

    fn log_messages(&mut self, value: &Value, source: &'static str) {
        for message in list_of(value, "messages") {
            let text = format!("{}: {}", str_of(&message, "title"), str_of(&message, "message"));
            self.logs
                .push(Level::parse(&str_of(&message, "severity")), source, &text);
        }
    }

    fn log_preflight_output(&mut self, output: &str) {
        let mut hidden = 0;
        for line in output.lines().filter(|line| !line.trim().is_empty()) {
            match classify_engine_line(line) {
                Some((level, message)) => self.logs.push(level, "preflight", &message),
                None => hidden += 1,
            }
        }
        if hidden > 0 {
            self.logs.info(
                "preflight",
                &format!("Preflight concluído; {hidden} linha(s) detalhadas ocultadas."),
            );
        }
    }

    fn set_status(&mut self, status: &str, tone: Tone) {
        self.status = status.to_string();
        self.status_tone = if self.last_error.is_empty() || tone == Tone::Success {
            tone
        } else {
            Tone::Danger
        };
    }

    // ------------------------------------------------------------ command bar

    /// Status pill, execution mode, environment and the start/stop button.
    pub fn controls_ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        self.status_pill(ui);
        ui.add_space(6.0);
        let editable = self.can_start();
        ui.add_enabled_ui(editable, |ui| {
            let options: Vec<(String, String, Tone)> = shell
                .options
                .execution_modes
                .iter()
                .map(|m| (m.clone(), mode_label(m).to_string(), mode_tone(m)))
                .collect();
            if widgets::segmented(ui, &mut self.mode, &options) {
                match self.mode.as_str() {
                    "TESTNET_LIVE" => self.environment = "TESTNET".into(),
                    "REAL_LIVE" => self.environment = "REAL".into(),
                    _ => {}
                }
            }
            let forced = matches!(self.mode.as_str(), "TESTNET_LIVE" | "REAL_LIVE");
            ui.add_enabled_ui(!forced, |ui| {
                let options: Vec<(String, String, Tone)> = shell
                    .options
                    .environments
                    .iter()
                    .map(|e| {
                        (
                            e.clone(),
                            e.clone(),
                            if e == "REAL" { Tone::Danger } else { Tone::Accent },
                        )
                    })
                    .collect();
                widgets::segmented(ui, &mut self.environment, &options);
            })
            .response
            .on_disabled_hover_text("O modo de execução define o ambiente.");
        });
        ui.add_space(4.0);
        self.primary_action(ui, shell);
    }

    fn status_pill(&self, ui: &mut Ui) {
        let running = self.process.is_some();
        let tone = if running { Tone::Success } else { self.status_tone };
        let text = match self.uptime() {
            Some(uptime) if self.phase == Phase::Running => {
                ui.ctx().request_repaint_after(Duration::from_secs(1));
                format!("Rodando · {}", format_duration(uptime))
            }
            _ => self.status.clone(),
        };
        let galley = ui
            .painter()
            .layout_no_wrap(text, theme::semibold(13.0), theme::TEXT);
        let width = (galley.size().x + 42.0).max(150.0);
        let (rect, response) = ui.allocate_exact_size(vec2(width, theme::CONTROL_HEIGHT), Sense::hover());
        ui.painter().rect_filled(rect, 17, tone.soft());
        ui.painter().rect_stroke(
            rect,
            17,
            egui::Stroke::new(1.0, theme::alpha(tone.color(), 70)),
            egui::StrokeKind::Inside,
        );
        widgets::status_dot(ui, rect.left_center() + vec2(17.0, 0.0), tone, running);
        ui.painter().galley(
            rect.left_center() + vec2(30.0, -galley.size().y / 2.0),
            galley,
            theme::TEXT,
        );
        if !self.last_error.is_empty() && !running {
            response.on_hover_text(&self.last_error);
        }
    }

    /// Start, stop or force-stop depending on the phase.
    fn primary_action(&mut self, ui: &mut Ui, shell: &mut Shell) {
        match self.phase {
            Phase::Running => {
                if widgets::button(ui, Kind::Danger, Some(Icon::Stop), "Parar")
                    .on_hover_text("Parada graciosa (Shift+F9)")
                    .clicked()
                {
                    self.stop(shell);
                }
            }
            Phase::Stopping => {
                if widgets::button(ui, Kind::Danger, Some(Icon::Power), "Forçar parada").clicked() {
                    self.force_stop();
                }
            }
            Phase::Idle => {
                let ready = shell.bridge.is_ready();
                let response = ui
                    .add_enabled_ui(ready, |ui| {
                        widgets::button(ui, Kind::Primary, Some(Icon::Play), "Iniciar")
                    })
                    .inner
                    .on_hover_text("Iniciar o Alphora (F9)");
                if response.clicked() {
                    self.start(shell);
                }
            }
            _ => widgets::spinner(ui, 22.0),
        }
    }

    // ------------------------------------------------------------ dashboard

    /// Hero, readiness checklist, KPIs, positions and recent events.
    pub fn dashboard_ui(&mut self, ui: &mut Ui, shell: &mut Shell, recent: &[Event]) -> Option<Command> {
        let mut command = None;
        widgets::split(ui, 0.62, |ui, side| {
            if side == 0 {
                self.hero(ui, shell);
            } else {
                self.readiness(ui, shell);
            }
        });
        ui.add_space(16.0);
        self.kpis(ui, recent);
        ui.add_space(16.0);
        let rows_loaded = self.rows_loaded;
        let subtitle = rows_subtitle(&self.rows_state);
        let rows = &self.rows;
        widgets::split(ui, 0.62, |ui, side| {
            if side == 0 {
                widgets::card(ui, |ui| {
                    widgets::card_header(ui, Icon::Coins, "Posições e símbolos", &subtitle, |_| {});
                    if rows_loaded {
                        symbol_table(ui, rows);
                    } else {
                        widgets::skeleton_block(ui, 4);
                    }
                });
            } else {
                widgets::card(ui, |ui| {
                    widgets::card_header(
                        ui,
                        Icon::Eye,
                        "Eventos recentes",
                        "Fluxo inteligente do Governor",
                        |ui| {
                            if widgets::button(ui, Kind::Ghost, None, "Ver todos").clicked() {
                                command = Some(Command::Goto(Workspace::Monitor));
                            }
                        },
                    );
                    recent_events(ui, recent);
                });
            }
        });
        command
    }

    fn hero(&mut self, ui: &mut Ui, shell: &mut Shell) {
        widgets::card(ui, |ui| {
            ui.set_min_height(292.0);
            let running = self.process.is_some();
            let tone = if running { Tone::Success } else { self.status_tone };
            ui.horizontal(|ui| {
                let (rect, _) = ui.allocate_exact_size(vec2(64.0, 64.0), Sense::hover());
                ui.painter().circle_filled(rect.center(), 32.0, tone.soft());
                ui.painter()
                    .circle_filled(rect.center(), 22.0, theme::alpha(tone.color(), 40));
                icons::paint(
                    ui.painter(),
                    rect.shrink(20.0),
                    if running { Icon::Pulse } else { Icon::Power },
                    tone.color(),
                );
                ui.add_space(6.0);
                ui.vertical(|ui| {
                    ui.spacing_mut().item_spacing.y = 3.0;
                    ui.label(
                        RichText::new("ENGINE")
                            .font(theme::semibold(11.0))
                            .color(theme::TEXT_FAINT),
                    );
                    ui.label(
                        RichText::new(&self.status)
                            .font(theme::semibold(28.0))
                            .color(theme::TEXT),
                    );
                    let detail = match &self.process {
                        Some(process) => {
                            ui.ctx().request_repaint_after(Duration::from_secs(1));
                            format!(
                                "PID {} · em execução há {}",
                                process.pid(),
                                format_duration(process.uptime())
                            )
                        }
                        None if !self.last_error.is_empty() => self.last_error.clone(),
                        None => "Pronto para iniciar quando você quiser.".into(),
                    };
                    let color = if self.process.is_none() && !self.last_error.is_empty() {
                        theme::DANGER
                    } else {
                        theme::TEXT_MUTED
                    };
                    ui.add(egui::Label::new(RichText::new(detail).size(13.0).color(color)).truncate());
                });
            });
            ui.add_space(14.0);
            ui.horizontal(|ui| {
                widgets::badge(ui, mode_label(&self.mode), mode_tone(&self.mode));
                let env_tone = if self.environment == "REAL" {
                    Tone::Danger
                } else {
                    Tone::Accent
                };
                widgets::badge(ui, &self.environment, env_tone);
                if let Some(overview) = &self.overview {
                    widgets::badge(ui, &format!("Loop {} s", overview.loop_interval), Tone::Muted);
                }
            });
            ui.add_space(16.0);
            ui.horizontal(|ui| {
                self.primary_action(ui, shell);
                ui.label(
                    RichText::new(match self.phase {
                        Phase::Idle => "Modo e ambiente ficam na barra superior · F9 inicia",
                        Phase::Running => "Shift+F9 solicita parada graciosa",
                        _ => "Aguarde a etapa atual",
                    })
                    .size(12.0)
                    .color(theme::TEXT_FAINT),
                );
            });
            if self.mode == "REAL_LIVE" {
                ui.add_space(12.0);
                widgets::banner(
                    ui,
                    Tone::Danger,
                    Icon::Warning,
                    "REAL_LIVE envia ordens reais. O início exige checklist e confirmações digitadas.",
                );
            }
        });
    }

    fn readiness(&mut self, ui: &mut Ui, shell: &mut Shell) {
        widgets::card(ui, |ui| {
            ui.set_min_height(292.0);
            widgets::card_header(
                ui,
                Icon::Check,
                "Prontidão",
                "Verificações antes de iniciar",
                |_| {},
            );
            let Some(overview) = &self.overview else {
                widgets::skeleton_block(ui, 5);
                return;
            };
            ui.spacing_mut().item_spacing.y = 6.0;
            let core = shell.bridge.is_ready();
            widgets::check_item(
                ui,
                Some(core),
                "Núcleo Python",
                if core { "Conectado" } else { "Desconectado" },
            );
            let credentials = overview
                .credentials
                .get(&self.environment)
                .and_then(Value::as_bool)
                .unwrap_or(false);
            widgets::check_item(
                ui,
                Some(credentials),
                &format!("Credenciais {}", self.environment),
                if credentials {
                    "Configuradas"
                } else {
                    "Ausentes — configure em Configurações"
                },
            );
            let symbols = &overview.symbols;
            let symbol_detail = if symbols.is_empty() {
                "Nenhum símbolo habilitado".to_string()
            } else {
                symbols.join(", ")
            };
            widgets::check_item(
                ui,
                Some(!symbols.is_empty()),
                "Símbolos habilitados",
                &symbol_detail,
            );
            let has_strategy = !overview.strategy.is_empty() && overview.strategy != "-";
            let strategy = if overview.strategy_mode == "PROBABILISTIC_AI" {
                format!("{} · IA", overview.strategy)
            } else {
                overview.strategy.clone()
            };
            widgets::check_item(ui, Some(has_strategy), "Estratégia", &strategy);
            widgets::check_item(
                ui,
                None,
                "IA probabilística",
                if overview.ai_enabled {
                    "Ativada"
                } else {
                    "Desativada"
                },
            );
        });
    }

    fn kpis(&self, ui: &mut Ui, recent: &[Event]) {
        let open = self
            .rows
            .iter()
            .filter(|row| !matches!(str_of(row, "position").as_str(), "" | "NONE" | "0"))
            .count();
        let pnl: f64 = self
            .rows
            .iter()
            .filter_map(|row| parse_f64(&str_of(row, "unrealized_pnl")))
            .sum();
        let active = self
            .rows
            .iter()
            .filter(|row| !matches!(str_of(row, "status").as_str(), "" | "-" | "DISABLED"))
            .count();
        let pnl_tone = if pnl > 0.0 {
            Tone::Success
        } else if pnl < 0.0 {
            Tone::Danger
        } else {
            Tone::Neutral
        };
        let last_event = recent
            .first()
            .map(|e| e.title.clone())
            .unwrap_or_else(|| "—".into());
        let columns = widgets::columns_for(ui, 220.0, 4);
        widgets::grid(ui, columns, 4, |ui, index| match index {
            0 => widgets::stat_tile(
                ui,
                Icon::Coins,
                "Posições abertas",
                &open.to_string(),
                Tone::Neutral,
            ),
            1 => widgets::stat_tile(
                ui,
                Icon::Pulse,
                "PnL não realizado (USDT)",
                &format!("{pnl:+.2}"),
                pnl_tone,
            ),
            2 => widgets::stat_tile(
                ui,
                Icon::Server,
                "Ativos no runtime",
                &active.to_string(),
                Tone::Accent,
            ),
            _ => widgets::stat_tile(ui, Icon::Eye, "Último evento", &last_event, Tone::Neutral),
        });
    }
}

fn rows_subtitle(state: &Value) -> String {
    if !str_of(state, "error").is_empty() {
        return format!("Estado do Governor inválido: {}", str_of(state, "error"));
    }
    if bool_of(state, "available") {
        format!("Governor atualizado em {}", str_of(state, "updated_at"))
    } else {
        "Aguardando estado do Governor".into()
    }
}

pub fn mode_label(mode: &str) -> &str {
    match mode {
        "DRY_RUN" => "Dry run",
        "PAPER_LIVE" => "Paper",
        "TESTNET_LIVE" => "Testnet",
        "REAL_LIVE" => "Real",
        other => other,
    }
}

pub fn mode_tone(mode: &str) -> Tone {
    match mode {
        "REAL_LIVE" => Tone::Danger,
        "TESTNET_LIVE" => Tone::Warning,
        "PAPER_LIVE" => Tone::Success,
        _ => Tone::Accent,
    }
}

fn recent_events(ui: &mut Ui, events: &[Event]) {
    if events.is_empty() {
        widgets::empty_state(
            ui,
            Icon::Eye,
            "Sem eventos ainda",
            "Eventos aparecem quando o Governor opera.",
        );
        return;
    }
    for event in events.iter().take(6) {
        ui.horizontal(|ui| {
            let tone = Tone::from_severity(&event.severity);
            let (rect, _) = ui.allocate_exact_size(vec2(10.0, 34.0), Sense::hover());
            ui.painter().circle_filled(rect.center(), 4.0, tone.color());
            ui.vertical(|ui| {
                ui.spacing_mut().item_spacing.y = 0.0;
                ui.add(
                    egui::Label::new(
                        RichText::new(&event.title)
                            .font(theme::semibold(13.0))
                            .color(theme::TEXT),
                    )
                    .truncate(),
                );
                let meta = [
                    event.symbol.as_str(),
                    event.severity.as_str(),
                    event.timestamp.as_str(),
                ]
                .into_iter()
                .filter(|part| !part.is_empty())
                .collect::<Vec<_>>()
                .join(" · ");
                ui.add(egui::Label::new(RichText::new(meta).size(11.5).color(theme::TEXT_MUTED)).truncate());
            });
        });
    }
}

fn symbol_table(ui: &mut Ui, rows: &[Value]) {
    let active: Vec<&Value> = rows.iter().filter(|row| bool_of(row, "enabled")).collect();
    if active.is_empty() {
        widgets::empty_state(
            ui,
            Icon::Coins,
            "Nenhum símbolo habilitado",
            "Habilite perfis no workspace Símbolos.",
        );
        return;
    }
    let columns = [
        Column {
            title: "Símbolo",
            width: 110.0,
        },
        Column {
            title: "Status",
            width: 130.0,
        },
        Column {
            title: "Posição",
            width: 90.0,
        },
        Column {
            title: "Qtd.",
            width: 90.0,
        },
        Column {
            title: "Entrada",
            width: 100.0,
        },
        Column {
            title: "Último",
            width: 100.0,
        },
        Column {
            title: "PnL",
            width: 100.0,
        },
        Column {
            title: "Score",
            width: 70.0,
        },
        Column {
            title: "Motivo",
            width: 240.0,
        },
    ];
    let columns = widgets::fit_columns(&columns, ui.available_width());
    egui::ScrollArea::horizontal()
        .id_salt("dashboard-symbols")
        .show(ui, |ui| {
            ui.spacing_mut().item_spacing.y = 0.0;
            widgets::table_header(ui, &columns);
            for (index, row) in active.iter().enumerate() {
                let pnl = str_of(row, "unrealized_pnl");
                let pnl_color = match parse_f64(&pnl) {
                    Some(v) if v > 0.0 => theme::SUCCESS,
                    Some(v) if v < 0.0 => theme::DANGER,
                    _ => theme::TEXT_MUTED,
                };
                let error = str_of(row, "error");
                let reason = if error.is_empty() {
                    str_of(row, "reason")
                } else {
                    error
                };
                widgets::table_row(
                    ui,
                    &columns,
                    &[
                        Cell::colored(str_of(row, "symbol"), theme::TEXT),
                        Cell::badge(str_of(row, "status"), Tone::parse(&str_of(row, "tone"))),
                        Cell::text(str_of(row, "position")),
                        Cell::text(str_of(row, "quantity")),
                        Cell::text(str_of(row, "entry_price")),
                        Cell::text(str_of(row, "last_price")),
                        Cell::colored(pnl, pnl_color),
                        Cell::muted(display(row.get("score"))),
                        Cell::muted(reason),
                    ],
                    false,
                    index % 2 == 1,
                );
            }
        });
}
