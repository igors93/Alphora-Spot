//! Training of the global and per-symbol probabilistic AI models.

use std::time::Duration;

use egui::{RichText, Ui};
use serde_json::{Value, json};

use super::{Form, Shell, format_duration, horizon_label, split_lines, str_of, strings_of};
use crate::bridge::{Failure, PageId};
use crate::icons::Icon;
use crate::logs::LogBuffer;
use crate::process::{ManagedProcess, ProcEvent, Spawn};
use crate::theme::{self, Tone};
use crate::widgets::{self, Kind, PathDialog};

const PAGE: PageId = PageId::Training;
const PREPARE_TIMEOUT: Duration = Duration::from_secs(90);

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum TrainKind {
    All,
    Global,
    Refined,
}

impl TrainKind {
    fn code(self) -> &'static str {
        match self {
            Self::All => "ALL",
            Self::Global => "GLOBAL",
            Self::Refined => "REFINED",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::All => "global e refinados",
            Self::Global => "global",
            Self::Refined => "refinado",
        }
    }
}

struct Task {
    kind: TrainKind,
    process: ManagedProcess,
    output: Vec<String>,
}

pub struct TrainingPage {
    form: Form,
    baseline: Form,
    model: Value,
    task: Option<Task>,
    pending: Option<TrainKind>,
    saving: bool,
    status: (String, Tone),
    step: String,
    result_lines: Vec<String>,
    generated: Vec<(String, String)>,
    logs: LogBuffer,
    seen_rev: u64,
    loaded: bool,
    loading: bool,
}

impl TrainingPage {
    pub fn new() -> Self {
        Self {
            form: Form::default(),
            baseline: Form::default(),
            model: Value::Null,
            task: None,
            pending: None,
            saving: false,
            status: ("Pronto".into(), Tone::Neutral),
            step: "Nenhuma tarefa em execução".into(),
            result_lines: Vec::new(),
            generated: Vec::new(),
            logs: LogBuffer::new(5000),
            seen_rev: u64::MAX,
            loaded: false,
            loading: false,
        }
    }

    pub fn is_busy(&self) -> bool {
        self.task.is_some() || self.pending.is_some() || self.saving
    }

    pub fn is_running(&self) -> bool {
        self.task.is_some()
    }

    pub fn elapsed(&self) -> Option<Duration> {
        self.task.as_ref().map(|task| task.process.uptime())
    }

    pub fn logs(&self) -> &LogBuffer {
        &self.logs
    }

    pub fn clear_logs(&mut self) {
        self.logs.clear();
    }

    pub fn kill(&mut self) {
        if let Some(task) = &self.task {
            task.process.kill();
        }
    }

    pub fn dirty(&self) -> bool {
        self.form != self.baseline
    }

    pub fn discard(&mut self) {
        self.form = self.baseline.clone();
    }

    pub fn save(&mut self, shell: &mut Shell) {
        if self.dirty() && !self.is_busy() {
            self.saving = true;
            shell.call(
                PAGE,
                "save",
                "training.save",
                json!({"values": self.form.to_value()}),
            );
        }
    }

    pub fn train(&mut self, shell: &mut Shell, kind: TrainKind) {
        if shell.locked {
            shell.toasts.warn(
                "Treinamento bloqueado",
                "Pare o Alphora antes de treinar modelos.",
            );
            return;
        }
        if self.is_busy() || !shell.bridge.is_ready() {
            return;
        }
        self.pending = Some(kind);
        self.status = ("Preparando treinamento".into(), Tone::Warning);
        self.step = "Validando dados e configuração".into();
        shell.call_slow(
            PAGE,
            "start",
            "training.start",
            json!({"kind": kind.code(), "values": self.form.to_value()}),
            PREPARE_TIMEOUT,
        );
    }

    pub fn cancel(&mut self) {
        if let Some(task) = &self.task {
            self.status = ("Cancelando treinamento".into(), Tone::Warning);
            self.step = "Encerrando o subprocesso com segurança".into();
            task.process.kill();
        }
    }

    pub fn on_reply(&mut self, kind: &str, result: Result<Value, Failure>, shell: &mut Shell) {
        let value = match result {
            Ok(value) => value,
            Err(failure) => {
                if kind == "audit" {
                    return;
                }
                self.loading = false;
                self.saving = false;
                self.pending = None;
                if kind != "load" {
                    self.status = (failure.title.clone(), Tone::Danger);
                    self.step = failure.message.clone();
                }
                shell.toasts.failure(&failure);
                return;
            }
        };
        match kind {
            "load" | "save" => {
                self.loading = false;
                self.saving = false;
                self.loaded = true;
                self.apply_loaded(&value);
                if kind == "save" {
                    shell.saved();
                    shell.toasts.success("Treinamento", "Configuração salva.");
                }
            }
            "start" => {
                self.baseline = self.form.clone();
                shell.saved();
                self.spawn(&value, shell);
            }
            "done" => {
                self.pending = None;
                self.apply_loaded(&value);
                self.result_lines = split_lines(&str_of(&value, "summary"));
                self.generated = value
                    .get("generated")
                    .and_then(Value::as_object)
                    .map(|map| {
                        map.iter()
                            .map(|(k, v)| (k.clone(), v.as_str().unwrap_or_default().to_string()))
                            .collect()
                    })
                    .unwrap_or_default();
                self.status = ("Treinamento concluído".into(), Tone::Success);
                self.step = "Modelos validados e registrados nas configurações".into();
                shell.saved();
                shell.toasts.success(
                    "Treinamento concluído",
                    "Os modelos gerados foram validados e aplicados.",
                );
            }
            _ => {}
        }
    }

    fn apply_loaded(&mut self, value: &Value) {
        self.form = Form::from_value(value.get("values").unwrap_or(&Value::Null));
        self.baseline = self.form.clone();
        self.model = value.get("model_summary").cloned().unwrap_or(Value::Null);
    }

    fn spawn(&mut self, value: &Value, shell: &mut Shell) {
        let kind = self.pending.take().unwrap_or(TrainKind::All);
        let command = strings_of(value, "command");
        let Some(spec) = Spawn::from_command(&command, shell.root(), shell.bridge.child_env()) else {
            self.status = ("Plano inválido".into(), Tone::Danger);
            return;
        };
        match ManagedProcess::spawn(&spec, &shell.bridge_ctx()) {
            Ok(process) => {
                let pid = process.pid();
                self.logs.info(
                    "ia",
                    &format!("Símbolos: {}", strings_of(value, "symbols").join(", ")),
                );
                self.step = format!("Processo iniciado com PID {pid}");
                self.status = (format!("Treinando modelo {}", kind.label()), Tone::Warning);
                self.result_lines.clear();
                self.generated.clear();
                shell.call(
                    PAGE,
                    "audit",
                    "audit.task",
                    json!({"phase": "started", "kind": "ai-training", "command": command, "pid": pid}),
                );
                self.task = Some(Task {
                    kind,
                    process,
                    output: Vec::new(),
                });
            }
            Err(error) => {
                self.status = ("Falha ao iniciar treinamento".into(), Tone::Danger);
                self.step = error.to_string();
                shell
                    .toasts
                    .error("Falha ao iniciar treinamento", error.to_string());
            }
        }
    }

    pub fn tick(&mut self, shell: &mut Shell) {
        if shell.bridge.is_ready()
            && self.seen_rev != shell.config_rev
            && !self.loading
            && !self.dirty()
            && !self.is_busy()
        {
            self.seen_rev = shell.config_rev;
            self.loading = true;
            shell.call(PAGE, "load", "training.load", json!({}));
        }
        let Some(task) = &mut self.task else { return };
        let mut exit = None;
        for event in task.process.poll() {
            match event {
                ProcEvent::Line(line) => {
                    let event = serde_json::from_str::<serde_json::Value>(&line).ok();
                    let text = event
                        .as_ref()
                        .filter(|event| {
                            event.get("type").and_then(serde_json::Value::as_str) == Some("ai_progress")
                                && event.get("version").and_then(serde_json::Value::as_u64) == Some(1)
                        })
                        .and_then(|event| event.get("message"))
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or(line.trim_end());
                    if !text.is_empty() {
                        self.step = text.to_string();
                        self.logs.info("ia", text);
                    }
                    task.output.push(line);
                }
                ProcEvent::Exited { code, stop_requested } => exit = Some((code, stop_requested)),
            }
        }
        shell.bridge_ctx().request_repaint_after(Duration::from_secs(1));
        let Some((code, stop_requested)) = exit else {
            return;
        };
        let Some(task) = self.task.take() else { return };
        let status = if stop_requested {
            "STOPPED"
        } else if code == 0 {
            "FINISHED"
        } else {
            "ERROR"
        };
        shell.call(PAGE, "audit", "audit.output", json!({"lines": task.output}));
        shell.call(
            PAGE,
            "audit",
            "audit.task",
            json!({"phase": "finished", "kind": "ai-training", "exit_code": code, "status": status}),
        );
        if stop_requested {
            self.status = ("Treinamento cancelado".into(), Tone::Warning);
            self.step = format!("Subprocesso encerrado com código {code}");
        } else if code != 0 {
            self.status = (format!("Falhou com código {code}"), Tone::Danger);
            self.step = "Consulte o console (Ctrl+J) para os detalhes".into();
            shell
                .toasts
                .error("Treinamento concluído com erro", "Consulte o console (Ctrl+J).");
        } else {
            self.pending = Some(task.kind);
            self.step = format!("Validando modelos ({})", task.kind.label());
            shell.call_slow(
                PAGE,
                "done",
                "training.done",
                json!({"output": task.output}),
                PREPARE_TIMEOUT,
            );
        }
    }

    // ------------------------------------------------------------ view

    pub fn ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        if !self.loaded {
            widgets::card(ui, |ui| widgets::skeleton_block(ui, 8));
            return;
        }
        widgets::split(ui, 0.44, |ui, side| {
            if side == 0 {
                let editable = !self.is_busy() && !shell.locked;
                ui.add_enabled_ui(editable, |ui| self.config(ui, shell));
            } else {
                self.model_card(ui, shell);
                ui.add_space(14.0);
                self.run_card(ui, shell);
                ui.add_space(14.0);
                self.result_card(ui, shell);
            }
        });
    }

    fn config(&mut self, ui: &mut Ui, shell: &mut Shell) {
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Network,
                "Configuração do treinamento",
                "Símbolos, período, alvo e saída",
                |_| {},
            );
            section(ui, "SÍMBOLOS");
            widgets::grid(ui, 2, 2, |ui, index| {
                let (key, label, hint) = if index == 0 {
                    ("ai_training_global_symbols", "Modelo global", "BTCUSDT,ETHUSDT")
                } else {
                    ("ai_training_refined_symbol", "Modelo refinado", "BTCUSDT")
                };
                widgets::field(ui, label, |ui| {
                    if widgets::text_input(ui, self.form.text(key), hint).changed() {
                        let upper = self.form.get(key).to_uppercase();
                        self.form.set(key, upper);
                    }
                });
            });

            section(ui, "PERÍODO E ALVO");
            let timeframes: Vec<(String, String)> = shell
                .options
                .timeframes
                .iter()
                .map(|t| (t.value.clone(), format!("{} · {}", t.value, t.label)))
                .collect();
            widgets::grid(ui, 2, 6, |ui, index| match index {
                0 => widgets::field(ui, "Data inicial", |ui| {
                    widgets::text_input(ui, self.form.text("ai_training_start"), "AAAA-MM-DD");
                }),
                1 => widgets::field(ui, "Data final", |ui| {
                    widgets::text_input(ui, self.form.text("ai_training_end"), "AAAA-MM-DD");
                }),
                2 => widgets::field(ui, "Intervalo dos candles", |ui| {
                    widgets::combo_labeled(
                        ui,
                        "training-interval",
                        self.form.text("ai_training_interval"),
                        &timeframes,
                    );
                }),
                3 => widgets::field(ui, "Horizonte (candles)", |ui| {
                    widgets::text_input(ui, self.form.text("ai_training_horizon_candles"), "8");
                }),
                4 => widgets::field(ui, "Threshold (bps)", |ui| {
                    widgets::text_input(ui, self.form.text("ai_training_threshold_bps"), "40");
                }),
                _ => widgets::field(ui, "Mínimo de linhas", |ui| {
                    widgets::text_input(ui, self.form.text("ai_training_min_training_rows"), "200");
                }),
            });
            let interval = self.form.get("ai_training_interval").to_string();
            let candles = self
                .form
                .get("ai_training_horizon_candles")
                .trim()
                .parse::<u64>()
                .unwrap_or(0);
            if let (Some(minutes), true) = (shell.options.timeframe_minutes(&interval), candles > 0) {
                let label = shell
                    .options
                    .timeframes
                    .iter()
                    .find(|t| t.value == interval)
                    .map(|t| t.label.clone())
                    .unwrap_or_default();
                ui.add_space(4.0);
                ui.label(
                    RichText::new(format!(
                        "Prevê {label} × {candles} candles ≈ {} à frente",
                        horizon_label(minutes, candles)
                    ))
                    .size(12.0)
                    .color(theme::ACCENT),
                );
            }

            section(ui, "DADOS");
            widgets::toggle(
                ui,
                self.form.flag("ai_training_download_if_missing"),
                "Baixar candles quando não houver CSV",
            );
            ui.add_space(6.0);
            widgets::field(ui, "CSV (um símbolo)", |ui| {
                widgets::path_input(
                    ui,
                    self.form.text("ai_training_csv_path"),
                    "data/market/BTCUSDT_15m.csv",
                    PathDialog::Csv,
                );
            });
            ui.add_space(4.0);
            widgets::field(
                ui,
                "Mapa de CSV (SÍMBOLO=caminho, separados por vírgula)",
                |ui| {
                    widgets::text_input(
                        ui,
                        self.form.text("ai_training_csv_map"),
                        "BTCUSDT=data/market/btc.csv",
                    );
                },
            );

            section(ui, "SAÍDA DOS MODELOS");
            let mut open = None;
            for (key, label) in [
                ("ai_training_global_output_dir", "Diretório do modelo global"),
                (
                    "ai_training_refined_output_dir",
                    "Diretório dos modelos refinados",
                ),
            ] {
                widgets::field(ui, label, |ui| {
                    ui.horizontal(|ui| {
                        let width = (ui.available_width() - theme::CONTROL_HEIGHT - 8.0).max(80.0);
                        ui.allocate_ui(egui::vec2(width, theme::CONTROL_HEIGHT), |ui| {
                            widgets::path_input(
                                ui,
                                self.form.text(key),
                                "models/ai/...",
                                PathDialog::Directory,
                            );
                        });
                        if widgets::icon_button(ui, Icon::Open, "Abrir diretório").clicked() {
                            open = Some(self.form.get(key).to_string());
                        }
                    });
                });
                ui.add_space(4.0);
            }
            if let Some(dir) = open {
                let path = shell.root().join(dir);
                let _ = std::fs::create_dir_all(&path);
                shell.open_path(&path);
            }
        });
    }

    fn model_card(&mut self, ui: &mut Ui, shell: &mut Shell) {
        widgets::card(ui, |ui| {
            let tone = Tone::parse(&str_of(&self.model, "tone"));
            widgets::card_header(
                ui,
                Icon::Chip,
                "Modelo global em uso",
                &str_of(&self.model, "details"),
                |ui| {
                    widgets::badge(ui, &str_of(&self.model, "status"), tone);
                },
            );
            let path = str_of(&self.model, "path");
            ui.horizontal(|ui| {
                let text = if path.is_empty() {
                    "Nenhum modelo selecionado"
                } else {
                    &path
                };
                ui.add(
                    egui::Label::new(RichText::new(text).font(theme::mono(12.5)).color(theme::TEXT))
                        .truncate(),
                );
                if !path.is_empty() && widgets::icon_button(ui, Icon::Open, "Abrir modelo").clicked() {
                    let target = shell.root().join(&path);
                    shell.open_path(&target);
                }
            });
        });
    }

    fn run_card(&mut self, ui: &mut Ui, shell: &mut Shell) {
        widgets::card(ui, |ui| {
            ui.horizontal(|ui| {
                ui.vertical(|ui| {
                    ui.spacing_mut().item_spacing.y = 2.0;
                    ui.label(
                        RichText::new("EXECUÇÃO")
                            .font(theme::semibold(11.0))
                            .color(theme::TEXT_FAINT),
                    );
                    ui.label(
                        RichText::new(&self.status.0)
                            .font(theme::semibold(19.0))
                            .color(theme::TEXT),
                    );
                });
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if let Some(task) = &self.task {
                        widgets::badge(ui, &format_duration(task.process.uptime()), Tone::Warning);
                    } else if self.pending.is_some() {
                        widgets::spinner(ui, 20.0);
                    }
                });
            });
            ui.add_space(6.0);
            ui.add(
                egui::Label::new(RichText::new(&self.step).size(12.5).color(theme::TEXT_MUTED)).truncate(),
            );
            if self.task.is_some() {
                ui.add_space(8.0);
                widgets::indeterminate_bar(ui);
            }
            ui.add_space(12.0);
            ui.horizontal_wrapped(|ui| {
                let can_start = !self.is_busy() && !shell.locked && shell.bridge.is_ready();
                ui.add_enabled_ui(can_start, |ui| {
                    if widgets::button(ui, Kind::Primary, Some(Icon::Network), "Treinar tudo").clicked() {
                        self.train(shell, TrainKind::All);
                    }
                    if widgets::button(ui, Kind::Secondary, Some(Icon::Chip), "Só global").clicked() {
                        self.train(shell, TrainKind::Global);
                    }
                    if widgets::button(ui, Kind::Secondary, Some(Icon::Target), "Só refinado").clicked() {
                        self.train(shell, TrainKind::Refined);
                    }
                });
                if self.task.is_some()
                    && widgets::button(ui, Kind::Danger, Some(Icon::Stop), "Cancelar").clicked()
                {
                    self.cancel();
                }
            });
        });
    }

    fn result_card(&mut self, ui: &mut Ui, shell: &mut Shell) {
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Check,
                "Resultado",
                "Modelos gerados e registrados",
                |_| {},
            );
            if self.result_lines.is_empty() {
                widgets::empty_state(
                    ui,
                    Icon::Network,
                    "Nenhum treinamento nesta sessão",
                    "Os modelos gerados aparecem aqui.",
                );
                return;
            }
            widgets::text_lines(ui, "training-result", &self.result_lines, 140.0);
            let mut open = None;
            if !self.generated.is_empty() {
                ui.add_space(8.0);
                for (symbol, path) in &self.generated {
                    ui.horizontal(|ui| {
                        widgets::badge(ui, symbol, Tone::Accent);
                        ui.add(
                            egui::Label::new(RichText::new(path).font(theme::mono(12.0)).color(theme::TEXT))
                                .truncate(),
                        );
                        if widgets::icon_button(ui, Icon::Open, "Abrir").clicked() {
                            open = Some(shell.root().join(path));
                        }
                    });
                }
            }
            if let Some(path) = open {
                shell.open_path(&path);
            }
        });
    }
}

fn section(ui: &mut Ui, title: &str) {
    ui.add_space(14.0);
    ui.label(
        RichText::new(title)
            .font(theme::semibold(11.0))
            .color(theme::TEXT_FAINT)
            .extra_letter_spacing(0.8),
    );
    ui.add_space(4.0);
}
