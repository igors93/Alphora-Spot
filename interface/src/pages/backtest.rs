//! Historical candle download, backtest execution and report.

use std::time::Duration;

use egui::{Align2, Pos2, RichText, Sense, Shape, Stroke, Ui, pos2, vec2};
use serde_json::{Value, json};

use super::{Form, Shell, display, format_duration, split_lines, str_of, strings_of};
use crate::bridge::{Failure, PageId};
use crate::icons::Icon;
use crate::logs::LogBuffer;
use crate::process::{ManagedProcess, ProcEvent, Spawn};
use crate::theme::{self, Tone};
use crate::widgets::{self, Kind, PathDialog};

const PAGE: PageId = PageId::Backtest;
const PREPARE_TIMEOUT: Duration = Duration::from_secs(90);

const RULES: [(&str, &str); 4] = [
    ("backtest_quantity_step", "Quantity step"),
    ("backtest_min_quantity", "Quantidade mínima"),
    ("backtest_min_notional", "Notional mínimo"),
    ("backtest_price_tick", "Price tick"),
];
const COSTS: [(&str, &str); 6] = [
    ("backtest_initial_balance", "Saldo inicial (USDT)"),
    ("maker_fee_pct", "Taxa maker"),
    ("taker_fee_pct", "Taxa taker"),
    ("slippage_bps", "Slippage (bps)"),
    ("spread_bps", "Spread (bps)"),
    ("default_funding_rate", "Funding padrão"),
];
const ARTIFACTS: [(&str, &str); 6] = [
    ("summaryJson", "Resumo"),
    ("tradesCsv", "Trades"),
    ("equityCsv", "Equity"),
    ("eventsJsonl", "Decisões"),
    ("rejectedJsonl", "Rejeitados"),
    ("manifestJson", "Manifesto"),
];

#[derive(Clone, Copy, PartialEq, Eq)]
enum TaskKind {
    Download,
    Backtest,
}

impl TaskKind {
    fn label(self) -> &'static str {
        match self {
            Self::Download => "download",
            Self::Backtest => "backtest",
        }
    }
}

struct Task {
    kind: TaskKind,
    process: ManagedProcess,
    output: Vec<String>,
}

struct Report {
    summary: Value,
    lines: Vec<String>,
    artifacts: Value,
    equity: Vec<f64>,
}

pub struct BacktestPage {
    form: Form,
    baseline: Form,
    strategies: Vec<String>,
    strategy: String,
    strategy_baseline: String,
    task: Option<Task>,
    pending: Option<&'static str>,
    status: (String, Tone),
    last_line: String,
    report: Option<Report>,
    logs: LogBuffer,
    seen_rev: u64,
    loaded: bool,
    loading: bool,
}

impl BacktestPage {
    pub fn new() -> Self {
        Self {
            form: Form::default(),
            baseline: Form::default(),
            strategies: Vec::new(),
            strategy: String::new(),
            strategy_baseline: String::new(),
            task: None,
            pending: None,
            status: ("Pronto".into(), Tone::Neutral),
            last_line: String::new(),
            report: None,
            logs: LogBuffer::new(5000),
            seen_rev: u64::MAX,
            loaded: false,
            loading: false,
        }
    }

    pub fn is_busy(&self) -> bool {
        self.task.is_some() || self.pending.is_some()
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
        self.form != self.baseline || self.strategy != self.strategy_baseline
    }

    pub fn discard(&mut self) {
        self.form = self.baseline.clone();
        self.strategy = self.strategy_baseline.clone();
    }

    fn params(&self) -> Value {
        json!({"values": self.form.to_value(), "strategy": self.strategy})
    }

    fn can_start(&self, shell: &Shell) -> bool {
        !self.is_busy() && !shell.locked && shell.bridge.is_ready() && self.loaded
    }

    pub fn run(&mut self, shell: &mut Shell) {
        if self.guard(shell) {
            self.status = ("Preparando backtest".into(), Tone::Warning);
            self.request(shell, "run", "backtest.run");
        }
    }

    pub fn download(&mut self, shell: &mut Shell) {
        if self.guard(shell) {
            self.status = ("Preparando download".into(), Tone::Warning);
            self.request(shell, "download", "backtest.download");
        }
    }

    pub fn fetch_rules(&mut self, shell: &mut Shell) {
        if self.guard(shell) {
            self.status = ("Consultando a Binance".into(), Tone::Warning);
            self.logs.info(
                "backtest",
                &format!("Buscando regras de {}.", self.form.get("symbol")),
            );
            self.request(shell, "rules", "backtest.rules");
        }
    }

    pub fn save(&mut self, shell: &mut Shell) {
        if self.dirty() && self.pending.is_none() {
            self.request(shell, "save", "backtest.save");
        }
    }

    pub fn cancel(&mut self) {
        if let Some(task) = &self.task {
            self.status = ("Cancelando tarefa".into(), Tone::Warning);
            task.process.kill();
        }
    }

    fn guard(&mut self, shell: &mut Shell) -> bool {
        if shell.locked {
            shell.toasts.warn(
                "Pesquisa bloqueada",
                "Pare o Alphora antes de iniciar tarefas de pesquisa.",
            );
            return false;
        }
        if self.is_busy() {
            shell
                .toasts
                .warn("Tarefa em andamento", "Aguarde ou cancele a tarefa atual.");
            return false;
        }
        self.can_start(shell)
    }

    fn request(&mut self, shell: &mut Shell, kind: &'static str, method: &str) {
        self.pending = Some(kind);
        shell.call_slow(PAGE, kind, method, self.params(), PREPARE_TIMEOUT);
    }

    pub fn on_reply(&mut self, kind: &str, result: Result<Value, Failure>, shell: &mut Shell) {
        let value = match result {
            Ok(value) => value,
            Err(failure) => {
                if kind == "load" {
                    self.loading = false;
                } else if kind != "audit" {
                    self.pending = None;
                    self.status = (failure.title.clone(), Tone::Danger);
                    self.logs.error("backtest", &failure.message);
                }
                if kind != "audit" {
                    shell.toasts.failure(&failure);
                }
                return;
            }
        };
        match kind {
            "load" => {
                self.loading = false;
                self.loaded = true;
                self.form = Form::from_value(value.get("values").unwrap_or(&Value::Null));
                self.baseline = self.form.clone();
                self.strategies = strings_of(&value, "strategies");
                self.strategy = str_of(&value, "selected_strategy");
                self.strategy_baseline = self.strategy.clone();
            }
            "save" => {
                self.pending = None;
                self.mark_saved(shell);
                shell.toasts.success("Backtest", "Configuração salva.");
            }
            "download" => {
                self.mark_saved(shell);
                self.spawn(TaskKind::Download, &value, shell);
            }
            "run" => {
                self.mark_saved(shell);
                self.report = None;
                let symbols = strings_of(&value, "symbols");
                self.logs
                    .info("backtest", &format!("Símbolos: {}", symbols.join(", ")));
                self.spawn(TaskKind::Backtest, &value, shell);
            }
            "rules" => {
                self.pending = None;
                self.form.merge(value.get("values").unwrap_or(&Value::Null));
                self.status = ("Regras atualizadas".into(), Tone::Success);
                self.logs.info("backtest", "Regras de mercado atualizadas.");
                shell.toasts.success(
                    "Regras da Binance",
                    "Filtros de quantidade e preço atualizados. Salve para manter.",
                );
            }
            "download_done" => {
                self.pending = None;
                self.form.merge(value.get("values").unwrap_or(&Value::Null));
                self.baseline = self.form.clone();
                shell.saved();
                match value.get("path").and_then(Value::as_str) {
                    Some(path) => {
                        self.status = ("Candles baixados".into(), Tone::Success);
                        shell
                            .toasts
                            .success("Download concluído", format!("Candles salvos em:\n{path}"));
                    }
                    None => {
                        self.status = ("Download sem caminho identificado".into(), Tone::Warning);
                        shell.toasts.warn(
                            "Download concluído",
                            "A tarefa terminou, mas o caminho do CSV não foi identificado na saída.",
                        );
                    }
                }
            }
            "result" => {
                self.pending = None;
                self.status = ("Backtest concluído".into(), Tone::Success);
                self.report = Some(Report {
                    summary: value.get("summary").cloned().unwrap_or(Value::Null),
                    lines: split_lines(&str_of(&value, "text")),
                    artifacts: value.get("artifacts").cloned().unwrap_or(Value::Null),
                    equity: value
                        .get("equity")
                        .and_then(Value::as_array)
                        .map(|items| items.iter().filter_map(Value::as_f64).collect())
                        .unwrap_or_default(),
                });
                shell
                    .toasts
                    .success("Backtest concluído", "Relatório e artefatos atualizados.");
            }
            _ => {}
        }
    }

    fn mark_saved(&mut self, shell: &mut Shell) {
        self.baseline = self.form.clone();
        self.strategy_baseline = self.strategy.clone();
        shell.saved();
    }

    fn spawn(&mut self, kind: TaskKind, value: &Value, shell: &mut Shell) {
        self.pending = None;
        let command = strings_of(value, "command");
        let mut env = shell.bridge.child_env();
        if let Some(extra) = value.get("env").and_then(Value::as_object) {
            env.extend(
                extra
                    .iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap_or_default().to_string())),
            );
        }
        let Some(spec) = Spawn::from_command(&command, shell.root(), env) else {
            self.status = ("Plano inválido".into(), Tone::Danger);
            return;
        };
        match ManagedProcess::spawn(&spec, &shell.bridge_ctx()) {
            Ok(process) => {
                let pid = process.pid();
                self.logs
                    .info(kind.label(), &format!("Tarefa iniciada com PID {pid}."));
                self.last_line.clear();
                self.status = (
                    match kind {
                        TaskKind::Download => "Baixando candles",
                        TaskKind::Backtest => "Executando backtest",
                    }
                    .into(),
                    Tone::Warning,
                );
                shell.call(
                    PAGE,
                    "audit",
                    "audit.task",
                    json!({"phase": "started", "kind": kind.label(), "command": command, "pid": pid}),
                );
                self.task = Some(Task {
                    kind,
                    process,
                    output: Vec::new(),
                });
            }
            Err(error) => {
                self.status = ("Falha ao iniciar tarefa".into(), Tone::Danger);
                shell.toasts.error("Falha ao iniciar tarefa", error.to_string());
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
            shell.call(PAGE, "load", "backtest.load", json!({}));
        }
        let Some(task) = &mut self.task else { return };
        let mut exit = None;
        for event in task.process.poll() {
            match event {
                ProcEvent::Line(line) => {
                    self.logs.info(task.kind.label(), &line);
                    if !line.trim().is_empty() {
                        self.last_line = line.trim().to_string();
                    }
                    task.output.push(line);
                }
                ProcEvent::Exited { code, stop_requested } => exit = Some((code, stop_requested)),
            }
        }
        // Keeps the elapsed-time label ticking while the task runs.
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
            json!({"phase": "finished", "kind": task.kind.label(), "exit_code": code, "status": status}),
        );
        if stop_requested {
            self.status = ("Tarefa cancelada".into(), Tone::Warning);
            self.logs
                .warn(task.kind.label(), &format!("Tarefa cancelada com código {code}."));
            return;
        }
        if code != 0 {
            self.status = (format!("Falhou com código {code}"), Tone::Danger);
            shell.toasts.error(
                "Tarefa concluída com erro",
                "Consulte o console (Ctrl+J) para os detalhes.",
            );
            return;
        }
        match task.kind {
            TaskKind::Download => {
                self.pending = Some("download_done");
                let mut params = self.params();
                params["output"] = json!(task.output);
                shell.call(PAGE, "download_done", "backtest.download_done", params);
            }
            TaskKind::Backtest => {
                self.pending = Some("result");
                self.status = ("Lendo relatório".into(), Tone::Warning);
                shell.call(PAGE, "result", "backtest.result", json!({"output": task.output}));
            }
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
                self.run_card(ui, shell);
                ui.add_space(14.0);
                self.report_ui(ui, shell);
            }
        });
    }

    fn config(&mut self, ui: &mut Ui, shell: &mut Shell) {
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Candles,
                "Configuração do backtest",
                "Período, estratégia, dados e custos",
                |_| {},
            );
            section(ui, "DADOS");
            let timeframes: Vec<(String, String)> = shell
                .options
                .timeframes
                .iter()
                .map(|t| (t.value.clone(), format!("{} · {}", t.value, t.label)))
                .collect();
            widgets::grid(ui, 2, 4, |ui, index| match index {
                0 => widgets::field(ui, "Símbolo", |ui| {
                    widgets::text_input(ui, self.form.text("symbol"), "BTCUSDT");
                }),
                1 => widgets::field(ui, "Intervalo", |ui| {
                    widgets::combo_labeled(
                        ui,
                        "backtest-interval",
                        self.form.text("backtest_interval"),
                        &timeframes,
                    );
                }),
                2 => widgets::field(ui, "Data inicial", |ui| {
                    widgets::text_input(ui, self.form.text("backtest_start"), "AAAA-MM-DD");
                }),
                _ => widgets::field(ui, "Data final", |ui| {
                    widgets::text_input(ui, self.form.text("backtest_end"), "AAAA-MM-DD");
                }),
            });

            section(ui, "ESTRATÉGIA");
            let modes: Vec<(String, String, Tone)> = shell
                .options
                .strategy_modes
                .iter()
                .map(|m| {
                    (
                        m.clone(),
                        super::strategy::mode_label(m).to_string(),
                        Tone::Accent,
                    )
                })
                .collect();
            widgets::segmented(ui, self.form.text("strategy_selection_mode"), &modes);
            ui.add_space(6.0);
            let fixed = self.form.get("strategy_selection_mode") != "PROBABILISTIC_AI";
            ui.add_enabled_ui(fixed, |ui| {
                widgets::field(ui, "Estratégia fixa", |ui| {
                    let options: Vec<(String, String)> = self
                        .strategies
                        .iter()
                        .map(|q| (q.clone(), q.rsplit('.').next().unwrap_or(q).to_string()))
                        .collect();
                    widgets::combo_labeled(ui, "backtest-strategy", &mut self.strategy, &options);
                });
            })
            .response
            .on_disabled_hover_text("No modo IA o backtest usa o modelo configurado em Estratégia e IA.");
            ui.add_space(6.0);
            widgets::field(ui, "Símbolos testados", |ui| {
                let scopes: Vec<(String, String, Tone)> = shell
                    .options
                    .symbol_scopes
                    .iter()
                    .map(|s| {
                        let label = if s == "ENABLED" {
                            "Perfis habilitados"
                        } else {
                            "Somente o símbolo acima"
                        };
                        (s.clone(), label.to_string(), Tone::Accent)
                    })
                    .collect();
                widgets::segmented(ui, self.form.text("backtest_symbol_scope"), &scopes);
            });

            section(ui, "ARQUIVOS");
            widgets::field(ui, "CSV principal ({symbol} para vários símbolos)", |ui| {
                widgets::path_input(
                    ui,
                    self.form.text("backtest_csv_path"),
                    "data/market/{symbol}_1m.csv",
                    PathDialog::Csv,
                );
            });
            ui.add_space(6.0);
            widgets::toggle(
                ui,
                self.form.flag("use_secondary_csv"),
                "CSV de timeframe secundário",
            );
            if self.form.is_on("use_secondary_csv") {
                ui.add_space(4.0);
                widgets::path_input(
                    ui,
                    self.form.text("secondary_csv_path"),
                    "data/market/{symbol}_15m.csv",
                    PathDialog::Csv,
                );
            }
            ui.add_space(4.0);
            widgets::toggle(ui, self.form.flag("funding_enabled"), "Considerar funding");

            section(ui, "REGRAS DO MERCADO");
            widgets::grid(ui, 2, RULES.len(), |ui, index| {
                let (key, label) = RULES[index];
                widgets::field(ui, label, |ui| {
                    widgets::text_input(ui, self.form.text(key), "");
                });
            });
            ui.add_space(6.0);
            if widgets::button(ui, Kind::Ghost, Some(Icon::Refresh), "Buscar na Binance").clicked() {
                self.fetch_rules(shell);
            }

            section(ui, "SALDO E CUSTOS");
            widgets::grid(ui, 2, COSTS.len(), |ui, index| {
                let (key, label) = COSTS[index];
                widgets::field(ui, label, |ui| {
                    widgets::text_input(ui, self.form.text(key), "");
                });
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
                    } else {
                        widgets::badge(ui, "Ocioso", self.status.1);
                    }
                });
            });
            ui.add_space(10.0);
            if self.task.is_some() {
                widgets::indeterminate_bar(ui);
                ui.add_space(6.0);
                let line = if self.last_line.is_empty() {
                    "Aguardando saída do processo…"
                } else {
                    &self.last_line
                };
                ui.add(
                    egui::Label::new(
                        RichText::new(line)
                            .font(theme::mono(12.0))
                            .color(theme::TEXT_MUTED),
                    )
                    .truncate(),
                );
                ui.add_space(10.0);
            }
            ui.horizontal(|ui| {
                let can_start = self.can_start(shell);
                ui.add_enabled_ui(can_start, |ui| {
                    if widgets::button(ui, Kind::Primary, Some(Icon::Play), "Executar backtest")
                        .on_hover_text("Ctrl+B")
                        .clicked()
                    {
                        self.run(shell);
                    }
                    if widgets::button(ui, Kind::Secondary, Some(Icon::Download), "Baixar candles").clicked()
                    {
                        self.download(shell);
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

    fn report_ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        let Some(report) = &self.report else {
            widgets::card(ui, |ui| {
                widgets::empty_state(
                    ui,
                    Icon::Candles,
                    "Nenhum resultado ainda",
                    "Execute um backtest para ver métricas, curva de equity e relatório.",
                );
            });
            return;
        };
        let summary = &report.summary;
        let metric = |key: &str| display(summary.get(key));
        let pnl = metric("net_pnl");
        let pnl_tone = match super::parse_f64(&pnl) {
            Some(v) if v > 0.0 => Tone::Success,
            Some(v) if v < 0.0 => Tone::Danger,
            _ => Tone::Neutral,
        };
        let reliable = summary
            .get("reliability_passed")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let tiles: [(Icon, &str, String, Tone); 8] = [
            (Icon::Coins, "PnL líquido", pnl.clone(), pnl_tone),
            (Icon::Target, "Win rate", metric("win_rate"), Tone::Neutral),
            (
                Icon::Bolt,
                "Profit factor",
                metric("profit_factor"),
                Tone::Neutral,
            ),
            (
                Icon::Warning,
                "Max drawdown",
                metric("max_drawdown"),
                Tone::Warning,
            ),
            (Icon::Pulse, "Sharpe", metric("sharpe_ratio"), Tone::Neutral),
            (Icon::Pulse, "Sortino", metric("sortino_ratio"), Tone::Neutral),
            (Icon::Candles, "Trades", metric("total_trades"), Tone::Neutral),
            (
                Icon::Check,
                "Veredito",
                format!(
                    "{} · {}",
                    metric("operational_verdict"),
                    if reliable { "confiável" } else { "não confiável" }
                ),
                if reliable { Tone::Success } else { Tone::Danger },
            ),
        ];
        let columns = widgets::columns_for(ui, 190.0, 4);
        widgets::grid(ui, columns, tiles.len(), |ui, index| {
            let (icon, label, value, tone) = &tiles[index];
            widgets::stat_tile(ui, *icon, label, value, *tone);
        });
        ui.add_space(14.0);

        let mut open = None;
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Pulse,
                "Curva de equity",
                &format!("{} pontos", report.equity.len()),
                |_| {},
            );
            equity_chart(ui, &report.equity, 230.0);
        });
        ui.add_space(14.0);
        widgets::card(ui, |ui| {
            widgets::card_header(ui, Icon::File, "Relatório", "Readiness, razões e avisos", |ui| {
                if widgets::icon_button(ui, Icon::Folder, "Abrir pasta de relatórios").clicked() {
                    open = Some(shell.root().join("reports"));
                }
            });
            widgets::text_lines(ui, "backtest-summary", &report.lines, 240.0);
            ui.add_space(10.0);
            ui.horizontal_wrapped(|ui| {
                for (key, label) in ARTIFACTS {
                    let path = str_of(&report.artifacts, key);
                    ui.add_enabled_ui(!path.is_empty(), |ui| {
                        if widgets::button(ui, Kind::Secondary, Some(Icon::Open), label).clicked() {
                            open = Some(std::path::PathBuf::from(&path));
                        }
                    });
                }
            });
        });
        if let Some(path) = open {
            shell.open_path(&path);
        }
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

/// Line chart with a soft area fill, guides and a hover read-out.
fn equity_chart(ui: &mut Ui, values: &[f64], height: f32) {
    let width = ui.available_width();
    let (rect, response) = ui.allocate_exact_size(vec2(width, height), Sense::hover());
    let painter = ui.painter_at(rect);
    painter.rect_filled(rect, 10, theme::INPUT);
    if values.len() < 2 {
        painter.text(
            rect.center(),
            Align2::CENTER_CENTER,
            "Sem pontos de equity",
            theme::font(13.0),
            theme::TEXT_MUTED,
        );
        return;
    }
    let (min, max) = values
        .iter()
        .fold((f64::MAX, f64::MIN), |(lo, hi), &v| (lo.min(v), hi.max(v)));
    let span = if (max - min).abs() < f64::EPSILON {
        1.0
    } else {
        max - min
    };
    let plot = rect.shrink2(vec2(64.0, 18.0));
    let point = |index: usize, value: f64| -> Pos2 {
        let x = plot.left() + plot.width() * index as f32 / (values.len() - 1) as f32;
        let y = plot.bottom() - plot.height() * ((value - min) / span) as f32;
        pos2(x, y)
    };
    for fraction in [0.0_f32, 0.5, 1.0] {
        let y = plot.bottom() - plot.height() * fraction;
        painter.hline(plot.x_range(), y, Stroke::new(1.0, theme::BORDER));
        let label = min + span * fraction as f64;
        painter.text(
            pos2(rect.left() + 10.0, y),
            Align2::LEFT_CENTER,
            format!("{label:.2}"),
            theme::mono(11.0),
            theme::TEXT_FAINT,
        );
    }
    let up = values.last() >= values.first();
    let line_color = if up { theme::ACCENT } else { theme::DANGER };
    let points: Vec<Pos2> = values.iter().enumerate().map(|(i, &v)| point(i, v)).collect();
    let mut mesh = egui::Mesh::default();
    let top = theme::alpha(line_color, 70);
    let bottom = theme::alpha(line_color, 0);
    for pair in points.windows(2) {
        let base = mesh.vertices.len() as u32;
        mesh.colored_vertex(pair[0], top);
        mesh.colored_vertex(pair[1], top);
        mesh.colored_vertex(pos2(pair[1].x, plot.bottom()), bottom);
        mesh.colored_vertex(pos2(pair[0].x, plot.bottom()), bottom);
        mesh.add_triangle(base, base + 1, base + 2);
        mesh.add_triangle(base, base + 2, base + 3);
    }
    painter.add(Shape::mesh(mesh));
    painter.add(Shape::line(points.clone(), Stroke::new(2.0, line_color)));
    if let Some(pointer) = response.hover_pos() {
        let index = (((pointer.x - plot.left()) / plot.width()) * (values.len() - 1) as f32)
            .round()
            .clamp(0.0, (values.len() - 1) as f32) as usize;
        let p = points[index];
        painter.vline(p.x, plot.y_range(), Stroke::new(1.0, theme::BORDER_STRONG));
        painter.circle_filled(p, 4.0, line_color);
        painter.text(
            p + vec2(8.0, -10.0),
            Align2::LEFT_BOTTOM,
            format!("{:.2}", values[index]),
            theme::semibold(12.0),
            theme::TEXT,
        );
    }
}
