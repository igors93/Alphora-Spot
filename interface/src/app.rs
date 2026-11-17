//! Application shell: menu bar, command bar, workspace tabs, status bar, console,
//! save bar and dialogs. Pages own their data; the shell routes commands and replies.

use std::time::Duration;

use egui::{
    Align, Align2, CornerRadius, Frame, Layout, Margin, Rect, RichText, ScrollArea, Sense, Stroke,
    StrokeKind, Ui, ViewportCommand, pos2, vec2,
};

use crate::bridge::{Bridge, Options, PageId, Status};
use crate::commands::{Command, Workspace};
use crate::dialogs::{self, Confirm};
use crate::icons::{self, Icon};
use crate::logs::LogBuffer;
use crate::pages::Shell;
use crate::pages::backtest::BacktestPage;
use crate::pages::credentials::CredentialsPage;
use crate::pages::intelligence::IntelligencePage;
use crate::pages::observability::ObservabilityPage;
use crate::pages::operation::{OperationPage, mode_label};
use crate::pages::strategy::StrategyPage;
use crate::pages::symbols::SymbolsPage;
use crate::pages::training::{TrainKind, TrainingPage};
use crate::palette::Palette;
use crate::theme::{self, Tone};
use crate::toast::Toasts;
use crate::widgets::{self, Kind, LogAction, LogView, NavEntry};
use crate::workspace::Workspace as Paths;

const CONTENT_MAX_WIDTH: f32 = 1520.0;
const TRANSITION: f64 = 0.16;

#[derive(Clone, Copy, PartialEq, Eq)]
enum ResearchTab {
    Backtest,
    Training,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum LogSource {
    Engine,
    Backtest,
    Training,
}

/// Builds the page context from disjoint fields so pages can be borrowed alongside it.
macro_rules! shell {
    ($app:ident, $saved:ident, $locked:expr) => {
        Shell {
            bridge: &mut $app.bridge,
            toasts: &mut $app.toasts,
            options: &$app.options,
            locked: $locked,
            config_rev: $app.config_rev,
            config_saved: &mut $saved,
        }
    };
}

pub struct AlphoraApp {
    bridge: Bridge,
    profiler: crate::profile::FrameProfiler,
    toasts: Toasts,
    options: Options,
    options_generation: u64,
    config_rev: u64,

    workspace: Workspace,
    switched_at: f64,
    strategy_section: usize,
    research_tab: ResearchTab,
    settings_section: usize,

    operation: OperationPage,
    symbols: SymbolsPage,
    strategy: StrategyPage,
    intelligence: IntelligencePage,
    credentials: CredentialsPage,
    backtest: BacktestPage,
    training: TrainingPage,
    observability: ObservabilityPage,

    palette: Palette,
    console_open: bool,
    console_source: LogSource,
    console_views: [LogView; 3],
    show_shortcuts: bool,
    show_about: bool,
    quit_confirm: Option<Confirm<()>>,
    allow_close: bool,
    running_before: [bool; 3],
}

impl AlphoraApp {
    pub fn new(cc: &eframe::CreationContext<'_>) -> Self {
        theme::apply(&cc.egui_ctx);
        // Zoom is driven by our own commands (menu + shortcuts), not egui's defaults.
        cc.egui_ctx
            .options_mut(|options| options.zoom_with_keyboard = false);
        let bridge = Bridge::start(Paths::discover(), cc.egui_ctx.clone());
        Self {
            bridge,
            toasts: Toasts::default(),
            options: Options::default(),
            options_generation: 0,
            config_rev: 0,
            workspace: Workspace::Dashboard,
            switched_at: 0.0,
            strategy_section: 0,
            research_tab: ResearchTab::Backtest,
            settings_section: 0,
            operation: OperationPage::new(),
            symbols: SymbolsPage::new(),
            strategy: StrategyPage::new(),
            intelligence: IntelligencePage::new(),
            credentials: CredentialsPage::new(),
            backtest: BacktestPage::new(),
            training: TrainingPage::new(),
            observability: ObservabilityPage::new(),
            palette: Palette::default(),
            console_open: false,
            console_source: LogSource::Engine,
            console_views: Default::default(),
            show_shortcuts: false,
            show_about: false,
            quit_confirm: None,
            allow_close: false,
            running_before: [false; 3],
            profiler: crate::profile::FrameProfiler::from_env(),
        }
    }

    fn locked(&self) -> bool {
        self.operation.is_running() || self.operation.is_busy()
    }

    // ------------------------------------------------------------ data flow

    /// Routes core replies and runs per-frame work for every page.
    fn pump(&mut self) {
        let replies = self.bridge.poll();
        if self.bridge.generation() != self.options_generation {
            if let Some(hello) = self.bridge.hello() {
                self.options = hello.options.clone();
            }
            self.options_generation = self.bridge.generation();
            self.config_rev += 1;
        }
        let locked = self.locked();
        let workspace = self.workspace;
        let mut saved = false;
        {
            let mut shell = shell!(self, saved, locked);
            for reply in replies {
                let kind = reply.route.kind;
                let result = reply.result;
                match reply.route.page {
                    PageId::Operation => self.operation.on_reply(kind, result, &mut shell),
                    PageId::Symbols => self.symbols.on_reply(kind, result, &mut shell),
                    PageId::Strategy => self.strategy.on_reply(kind, result, &mut shell),
                    PageId::Intelligence => self.intelligence.on_reply(kind, result, &mut shell),
                    PageId::Credentials => self.credentials.on_reply(kind, result, &mut shell),
                    PageId::Backtest => self.backtest.on_reply(kind, result, &mut shell),
                    PageId::Training => self.training.on_reply(kind, result, &mut shell),
                    PageId::Observability => self.observability.on_reply(kind, result, &mut shell),
                }
            }
            // Every page loads eagerly in the background, so switching screens is instant.
            self.operation.tick(&mut shell, workspace == Workspace::Dashboard);
            self.symbols.tick(&mut shell, workspace == Workspace::Symbols);
            self.strategy.tick(&mut shell);
            self.intelligence.tick(&mut shell);
            self.credentials.tick(&mut shell);
            self.backtest.tick(&mut shell);
            self.training.tick(&mut shell);
            let monitoring = matches!(workspace, Workspace::Monitor | Workspace::Dashboard);
            self.observability
                .tick(&mut shell, monitoring, workspace == Workspace::Monitor);
        }
        if saved {
            self.config_rev += 1;
        }
        // Open the console on the source that just started running.
        let running = [
            self.operation.is_running(),
            self.backtest.is_running(),
            self.training.is_running(),
        ];
        let sources = [LogSource::Engine, LogSource::Backtest, LogSource::Training];
        for ((now, before), source) in running.iter().zip(self.running_before).zip(sources) {
            if *now && !before {
                self.console_open = true;
                self.console_source = source;
            }
        }
        self.running_before = running;
    }

    // ------------------------------------------------------------ commands

    fn enabled(&self, command: Command) -> bool {
        let ready = self.bridge.is_ready();
        let locked = self.locked();
        match command {
            Command::Start => ready && self.operation.can_start(),
            Command::Stop | Command::ForceStop => self.operation.is_running(),
            Command::Save => self.dirty(self.workspace),
            Command::RunBacktest | Command::DownloadCandles | Command::FetchRules => {
                ready && !locked && !self.backtest.is_busy()
            }
            Command::TrainAll | Command::TrainGlobal | Command::TrainRefined => {
                ready && !locked && !self.training.is_busy()
            }
            Command::CancelResearch => self.backtest.is_running() || self.training.is_running(),
            Command::ValidateModel | Command::RefreshDiagnostics => ready,
            Command::RotateJournals | Command::RotateIntelligent => ready && !locked,
            Command::RestartCore => !self.operation.is_busy(),
            _ => true,
        }
    }

    fn execute(&mut self, command: Command, ctx: &egui::Context) {
        if !self.enabled(command) {
            return;
        }
        let root = self.bridge.root();
        let open = |name: &str| -> std::path::PathBuf {
            if name.is_empty() {
                root.clone()
            } else {
                root.join(name)
            }
        };
        let path = match command {
            Command::OpenProject => Some(open("")),
            Command::OpenConfig => Some(open("config")),
            Command::OpenData => Some(open("data")),
            Command::OpenReports => Some(open("reports")),
            Command::OpenRuntime => Some(open("runtime")),
            Command::OpenGovernorState => Some(open("runtime").join("governor_state.json")),
            Command::OpenGovernorEvents => Some(open("data").join("governor_events.jsonl")),
            _ => None,
        };
        let locked = self.locked();
        let mut saved = false;
        let mut goto = None;
        {
            let mut shell = shell!(self, saved, locked);
            if let Some(path) = path {
                shell.open_path(&path);
            }
            match command {
                Command::Goto(workspace) => goto = Some(workspace),
                Command::Start => self.operation.start(&mut shell),
                Command::Stop => self.operation.stop(&mut shell),
                Command::ForceStop => self.operation.force_stop(),
                Command::Save => match self.workspace {
                    Workspace::Symbols => self.symbols.save(&mut shell),
                    Workspace::Strategy => {
                        self.strategy.save(&mut shell);
                        self.intelligence.save(&mut shell);
                    }
                    Workspace::Research => {
                        self.backtest.save(&mut shell);
                        self.training.save(&mut shell);
                    }
                    Workspace::Settings => self.credentials.save(&mut shell),
                    _ => {}
                },
                Command::RunBacktest => {
                    self.backtest.run(&mut shell);
                    self.research_tab = ResearchTab::Backtest;
                    goto = Some(Workspace::Research);
                }
                Command::DownloadCandles => {
                    self.backtest.download(&mut shell);
                    self.research_tab = ResearchTab::Backtest;
                    goto = Some(Workspace::Research);
                }
                Command::FetchRules => {
                    self.backtest.fetch_rules(&mut shell);
                    self.research_tab = ResearchTab::Backtest;
                    goto = Some(Workspace::Research);
                }
                Command::TrainAll | Command::TrainGlobal | Command::TrainRefined => {
                    let kind = match command {
                        Command::TrainGlobal => TrainKind::Global,
                        Command::TrainRefined => TrainKind::Refined,
                        _ => TrainKind::All,
                    };
                    self.training.train(&mut shell, kind);
                    self.research_tab = ResearchTab::Training;
                    goto = Some(Workspace::Research);
                }
                Command::CancelResearch => {
                    self.backtest.cancel();
                    self.training.cancel();
                }
                Command::ValidateModel => {
                    self.intelligence.validate_model(&mut shell);
                    self.strategy_section = 1;
                    goto = Some(Workspace::Strategy);
                }
                Command::RefreshDiagnostics => {
                    self.intelligence.refresh_diagnostics(&mut shell);
                    self.strategy_section = 4;
                    goto = Some(Workspace::Strategy);
                }
                Command::RotateJournals => self.observability.request_rotation(false, &mut shell),
                Command::RotateIntelligent => self.observability.request_rotation(true, &mut shell),
                _ => {}
            }
        }
        if saved {
            self.config_rev += 1;
        }
        match command {
            Command::Reload => {
                if self.dirty(self.workspace) {
                    self.toasts.warn(
                        "Alterações pendentes",
                        "Salve ou descarte as alterações antes de recarregar.",
                    );
                } else {
                    self.config_rev += 1;
                    self.toasts.info("Recarregando", "Configuração relida do disco.");
                }
            }
            Command::Palette => self.palette.toggle(),
            Command::Console => self.console_open = !self.console_open,
            Command::Shortcuts => self.show_shortcuts = true,
            Command::About => self.show_about = true,
            Command::RestartCore => self.bridge.restart(),
            Command::ZoomIn => ctx.set_zoom_factor((ctx.zoom_factor() + 0.1).min(2.0)),
            Command::ZoomOut => ctx.set_zoom_factor((ctx.zoom_factor() - 0.1).max(0.7)),
            Command::ZoomReset => ctx.set_zoom_factor(1.0),
            Command::Quit => ctx.send_viewport_cmd(ViewportCommand::Close),
            _ => {}
        }
        if let Some(workspace) = goto {
            self.goto(workspace, ctx);
        }
    }

    fn goto(&mut self, workspace: Workspace, ctx: &egui::Context) {
        if self.workspace != workspace {
            self.workspace = workspace;
            self.switched_at = ctx.input(|i| i.time);
            ctx.send_viewport_cmd(ViewportCommand::Title(format!("Alphora — {}", workspace.label())));
        }
    }

    fn dirty(&self, workspace: Workspace) -> bool {
        match workspace {
            Workspace::Symbols => self.symbols.dirty(),
            Workspace::Strategy => self.strategy.dirty() || self.intelligence.dirty(),
            Workspace::Research => self.backtest.dirty() || self.training.dirty(),
            Workspace::Settings => self.credentials.has_draft(),
            Workspace::Dashboard | Workspace::Monitor => false,
        }
    }

    fn discard(&mut self) {
        match self.workspace {
            Workspace::Symbols => self.symbols.discard(),
            Workspace::Strategy => {
                self.strategy.discard();
                self.intelligence.discard();
            }
            Workspace::Research => {
                self.backtest.discard();
                self.training.discard();
            }
            Workspace::Settings => self.credentials.discard(),
            _ => {}
        }
    }

    fn handle_shortcuts(&mut self, ctx: &egui::Context) {
        let palette_open = self.palette.is_open();
        let triggered: Vec<Command> = ctx.input_mut(|input| {
            Command::with_shortcuts()
                .filter(|(command, _)| !palette_open || *command == Command::Palette)
                .filter(|(_, shortcut)| input.consume_shortcut(shortcut))
                .map(|(command, _)| command)
                .collect()
        });
        for command in triggered {
            self.execute(command, ctx);
        }
    }

    /// Refuses to close while the engine runs; asks before killing research tasks.
    fn guard_close(&mut self, ctx: &egui::Context) {
        if self.allow_close || !ctx.input(|i| i.viewport().close_requested()) {
            return;
        }
        if self.locked() {
            ctx.send_viewport_cmd(ViewportCommand::CancelClose);
            let message = if self.operation.is_running() {
                "Pare o Alphora antes de fechar a interface."
            } else {
                "Aguarde a operação em andamento terminar antes de fechar a interface."
            };
            self.toasts.warn("Não é possível fechar agora", message);
            self.goto(Workspace::Dashboard, ctx);
        } else if self.backtest.is_busy() || self.training.is_busy() {
            ctx.send_viewport_cmd(ViewportCommand::CancelClose);
            self.quit_confirm = Some(Confirm::new(
                "Encerrar tarefas em andamento?",
                "Há um backtest, download ou treinamento em execução. Sair agora encerra o processo e descarta o resultado parcial.",
                "Encerrar e sair",
                (),
            ));
        }
    }

    // ------------------------------------------------------------ chrome

    fn menu_bar(&mut self, ui: &mut Ui) {
        let mut picked: Option<Command> = None;
        ui.horizontal_centered(|ui| {
            egui::MenuBar::new().ui(ui, |ui| {
                let menus: [(&str, &[Option<Command>]); 6] = [
                    (
                        "Arquivo",
                        &[
                            Some(Command::Save),
                            Some(Command::Reload),
                            None,
                            Some(Command::OpenProject),
                            Some(Command::OpenConfig),
                            Some(Command::OpenData),
                            Some(Command::OpenReports),
                            Some(Command::OpenRuntime),
                            None,
                            Some(Command::Quit),
                        ],
                    ),
                    (
                        "Operação",
                        &[
                            Some(Command::Start),
                            Some(Command::Stop),
                            Some(Command::ForceStop),
                            None,
                            Some(Command::OpenGovernorState),
                            Some(Command::OpenGovernorEvents),
                        ],
                    ),
                    (
                        "Exibir",
                        &[
                            Some(Command::Goto(Workspace::Dashboard)),
                            Some(Command::Goto(Workspace::Symbols)),
                            Some(Command::Goto(Workspace::Strategy)),
                            Some(Command::Goto(Workspace::Research)),
                            Some(Command::Goto(Workspace::Monitor)),
                            Some(Command::Goto(Workspace::Settings)),
                            None,
                            Some(Command::Console),
                            Some(Command::Palette),
                            None,
                            Some(Command::ZoomIn),
                            Some(Command::ZoomOut),
                            Some(Command::ZoomReset),
                        ],
                    ),
                    (
                        "Pesquisa",
                        &[
                            Some(Command::RunBacktest),
                            Some(Command::DownloadCandles),
                            Some(Command::FetchRules),
                            None,
                            Some(Command::TrainAll),
                            Some(Command::TrainGlobal),
                            Some(Command::TrainRefined),
                            None,
                            Some(Command::CancelResearch),
                        ],
                    ),
                    (
                        "Ferramentas",
                        &[
                            Some(Command::ValidateModel),
                            Some(Command::RefreshDiagnostics),
                            None,
                            Some(Command::RotateJournals),
                            Some(Command::RotateIntelligent),
                            None,
                            Some(Command::RestartCore),
                        ],
                    ),
                    ("Ajuda", &[Some(Command::Shortcuts), Some(Command::About)]),
                ];
                for (title, items) in menus {
                    ui.menu_button(title, |ui| {
                        ui.set_min_width(260.0);
                        for item in items {
                            match item {
                                None => {
                                    ui.separator();
                                }
                                Some(command) => {
                                    let mut button = egui::Button::new(command.label());
                                    if let Some(shortcut) = command.shortcut() {
                                        button = button.shortcut_text(ui.ctx().format_shortcut(&shortcut));
                                    }
                                    if ui.add_enabled(self.enabled(*command), button).clicked() {
                                        picked = Some(*command);
                                    }
                                }
                            }
                        }
                    });
                }
            });
        });
        if let Some(command) = picked {
            self.execute(command, &ui.ctx().clone());
        }
    }

    fn command_bar(&mut self, ui: &mut Ui) {
        let locked = self.locked();
        let mut saved = false;
        let mut open_palette = false;
        ui.horizontal_centered(|ui| {
            {
                let mut shell = shell!(self, saved, locked);
                self.operation.controls_ui(ui, &mut shell);
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                open_palette = palette_trigger(ui);
            });
        });
        if saved {
            self.config_rev += 1;
        }
        if open_palette {
            self.palette.toggle();
        }
    }

    fn tab_strip(&mut self, ui: &mut Ui) {
        let mut clicked = None;
        let mut selected_rect = None;
        ui.horizontal_centered(|ui| {
            ui.spacing_mut().item_spacing.x = 2.0;
            for workspace in Workspace::ALL {
                let marker = if self.dirty(workspace) {
                    Some(Tone::Warning)
                } else {
                    match workspace {
                        Workspace::Dashboard if self.operation.is_running() => Some(Tone::Success),
                        Workspace::Research if self.backtest.is_running() || self.training.is_running() => {
                            Some(Tone::Success)
                        }
                        _ => None,
                    }
                };
                let selected = workspace == self.workspace;
                let response = workspace_tab(ui, workspace, selected, marker);
                if selected {
                    selected_rect = Some(response.rect);
                }
                if response.clicked() {
                    clicked = Some(workspace);
                }
            }
        });
        // Sliding underline under the active tab.
        if let Some(rect) = selected_rect {
            let ctx = ui.ctx();
            let left =
                ctx.animate_value_with_time(egui::Id::unique("tab-underline-left"), rect.left() + 12.0, 0.18);
            let right = ctx.animate_value_with_time(
                egui::Id::unique("tab-underline-right"),
                rect.right() - 12.0,
                0.18,
            );
            let y = ui.max_rect().bottom() - 1.5;
            ui.painter()
                .line_segment([pos2(left, y), pos2(right, y)], Stroke::new(3.0, theme::ACCENT));
        }
        if let Some(workspace) = clicked {
            self.goto(workspace, &ui.ctx().clone());
        }
    }

    fn status_bar(&mut self, ui: &mut Ui) {
        let mut toggle_console = false;
        ui.horizontal_centered(|ui| {
            ui.spacing_mut().item_spacing.x = 14.0;
            let (label, tone) = match self.bridge.status() {
                Status::Ready => {
                    let version = self
                        .bridge
                        .hello()
                        .map(|h| h.python_version.clone())
                        .unwrap_or_default();
                    (format!("Núcleo Python {version}"), Tone::Success)
                }
                Status::Starting => ("Conectando ao núcleo…".to_string(), Tone::Warning),
                Status::Down(_) => ("Núcleo desconectado".to_string(), Tone::Danger),
            };
            status_item(ui, tone, &label);
            if let Some(hello) = self.bridge.hello() {
                ui.label(
                    RichText::new(format!("run {}", hello.run_id))
                        .font(theme::mono(11.0))
                        .color(theme::TEXT_FAINT),
                );
                let root = widgets::elided(ui, &hello.root, theme::font(11.5), theme::TEXT_FAINT, 360.0);
                let (rect, response) = ui.allocate_exact_size(root.size(), Sense::hover());
                ui.painter().galley(rect.min, root, theme::TEXT_FAINT);
                response.on_hover_text(format!("{}\n{}", hello.root, hello.python));
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.spacing_mut().item_spacing.x = 14.0;
                let (warnings, errors) = self.log_counts();
                let label = if self.console_open {
                    "Ocultar console"
                } else {
                    "Console"
                };
                if console_toggle(ui, label, warnings, errors).clicked() {
                    toggle_console = true;
                }
                if let Some(elapsed) = self.training.elapsed() {
                    status_item(
                        ui,
                        Tone::Warning,
                        &format!("Treinamento {}", super_duration(elapsed)),
                    );
                }
                if let Some(elapsed) = self.backtest.elapsed() {
                    status_item(
                        ui,
                        Tone::Warning,
                        &format!("Backtest {}", super_duration(elapsed)),
                    );
                }
                let (engine, tone) = match self.operation.uptime() {
                    Some(uptime) => (
                        format!("Engine rodando {}", super_duration(uptime)),
                        Tone::Success,
                    ),
                    None => {
                        let (status, tone) = self.operation.status();
                        (format!("Engine: {status}"), tone)
                    }
                };
                status_item(ui, tone, &engine);
                ui.label(
                    RichText::new(format!(
                        "{} · {}",
                        mode_label(self.operation.mode()),
                        self.operation.environment()
                    ))
                    .size(11.5)
                    .color(theme::TEXT_MUTED),
                );
            });
        });
        if toggle_console {
            self.console_open = !self.console_open;
        }
    }

    fn log_counts(&self) -> (usize, usize) {
        [self.operation.logs(), self.backtest.logs(), self.training.logs()]
            .iter()
            .map(|logs| logs.counts())
            .fold((0, 0), |(w, e), (lw, le)| (w + lw, e + le))
    }

    fn console(&mut self, ui: &mut Ui) {
        let mut close = false;
        ui.horizontal(|ui| {
            ui.label(
                RichText::new("Console")
                    .font(theme::semibold(13.5))
                    .color(theme::TEXT),
            );
            ui.add_space(8.0);
            let sources = [
                (LogSource::Engine, "engine", "Engine", self.operation.logs()),
                (LogSource::Backtest, "backtest", "Backtest", self.backtest.logs()),
                (
                    LogSource::Training,
                    "training",
                    "Treinamento",
                    self.training.logs(),
                ),
            ];
            let mut current = match self.console_source {
                LogSource::Engine => "engine",
                LogSource::Backtest => "backtest",
                LogSource::Training => "training",
            }
            .to_string();
            let options: Vec<(String, String, Tone)> = sources
                .iter()
                .map(|(_, key, label, logs)| {
                    let (_, errors) = logs.counts();
                    let tone = if errors > 0 { Tone::Danger } else { Tone::Accent };
                    (key.to_string(), label.to_string(), tone)
                })
                .collect();
            if widgets::segmented(ui, &mut current, &options) {
                self.console_source = sources
                    .iter()
                    .find(|(_, key, _, _)| *key == current)
                    .map(|(source, ..)| *source)
                    .unwrap_or(LogSource::Engine);
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if widgets::icon_button(ui, Icon::Close, "Fechar console (Ctrl+J)").clicked() {
                    close = true;
                }
            });
        });
        ui.add_space(6.0);
        let height = (ui.available_height() - 48.0).max(80.0);
        let (logs, view, id): (&LogBuffer, &mut LogView, &str) = match self.console_source {
            LogSource::Engine => (
                self.operation.logs(),
                &mut self.console_views[0],
                "console-engine",
            ),
            LogSource::Backtest => (
                self.backtest.logs(),
                &mut self.console_views[1],
                "console-backtest",
            ),
            LogSource::Training => (
                self.training.logs(),
                &mut self.console_views[2],
                "console-training",
            ),
        };
        if let LogAction::Clear = widgets::log_view(ui, id, logs, view, height) {
            match self.console_source {
                LogSource::Engine => self.operation.clear_logs(),
                LogSource::Backtest => self.backtest.clear_logs(),
                LogSource::Training => self.training.clear_logs(),
            }
        }
        if close {
            self.console_open = false;
        }
    }

    fn save_bar(&mut self, ctx: &egui::Context, area: Rect) {
        let dirty = self.dirty(self.workspace);
        let shown = ctx.animate_bool_with_time(egui::Id::unique("save-bar"), dirty, 0.18);
        if shown <= 0.0 {
            return;
        }
        let mut action = None;
        let offset = (1.0 - shown) * 24.0;
        egui::Area::new(egui::Id::unique("save-bar-area"))
            .order(egui::Order::Foreground)
            .fixed_pos(pos2(area.center().x, area.bottom() - 18.0 + offset))
            .pivot(Align2::CENTER_BOTTOM)
            .show(ctx, |ui| {
                ui.multiply_opacity(shown);
                Frame::new()
                    .fill(theme::TEXT)
                    .corner_radius(CornerRadius::same(14))
                    .inner_margin(Margin {
                        left: 18,
                        right: 8,
                        top: 8,
                        bottom: 8,
                    })
                    .shadow(theme::shadow(28, 10, 70))
                    .show(ui, |ui| {
                        ui.horizontal(|ui| {
                            ui.painter().circle_filled(
                                ui.cursor().min + vec2(4.0, 17.0),
                                4.0,
                                theme::WARNING,
                            );
                            ui.add_space(14.0);
                            ui.label(
                                RichText::new(format!("Alterações não salvas em {}", self.workspace.label()))
                                    .font(theme::semibold(13.0))
                                    .color(egui::Color32::WHITE),
                            );
                            ui.add_space(12.0);
                            if dark_button(ui, "Descartar").clicked() {
                                action = Some(false);
                            }
                            if widgets::button(ui, Kind::Primary, Some(Icon::Save), "Salvar  Ctrl+S")
                                .clicked()
                            {
                                action = Some(true);
                            }
                        });
                    });
            });
        match action {
            Some(true) => self.execute(Command::Save, ctx),
            Some(false) => self.discard(),
            None => {}
        }
    }

    // ------------------------------------------------------------ workspaces

    fn workspace_ui(&mut self, ui: &mut Ui) {
        let locked = self.locked();
        let mut saved = false;
        let mut pending = None;
        let workspace = self.workspace;
        if locked
            && matches!(
                workspace,
                Workspace::Strategy | Workspace::Research | Workspace::Settings
            )
        {
            widgets::banner(
                ui,
                Tone::Warning,
                Icon::Lock,
                "Operação em andamento: a configuração fica somente leitura até o Alphora parar.",
            );
            ui.add_space(14.0);
        }
        {
            let recent = self.observability.recent().to_vec();
            let strategy_hint = self.strategy.summary();
            let core_hint = self.bridge_status_label().0;
            let mut shell = shell!(self, saved, locked);
            match workspace {
                Workspace::Dashboard => pending = self.operation.dashboard_ui(ui, &mut shell, &recent),
                Workspace::Symbols => self.symbols.ui(ui, &mut shell),
                Workspace::Monitor => self.observability.ui(ui, &mut shell),
                Workspace::Strategy => {
                    let entries = [
                        NavEntry {
                            icon: Icon::Target,
                            label: "Estratégia",
                            hint: &strategy_hint,
                            marker: self.strategy.dirty().then_some(Tone::Warning),
                        },
                        NavEntry {
                            icon: Icon::Chip,
                            label: "IA probabilística",
                            hint: if self.intelligence.ai_enabled() {
                                "Ativada"
                            } else {
                                "Desativada"
                            },
                            marker: self.intelligence.dirty().then_some(Tone::Warning),
                        },
                        NavEntry {
                            icon: Icon::Filter,
                            label: "Ativação e limiares",
                            hint: "Nível e probabilidades",
                            marker: None,
                        },
                        NavEntry {
                            icon: Icon::Network,
                            label: "Módulos",
                            hint: "Saída, sizing, execução…",
                            marker: None,
                        },
                        NavEntry {
                            icon: Icon::Eye,
                            label: "Diagnóstico",
                            hint: "Leitura das camadas",
                            marker: None,
                        },
                    ];
                    let section = self.strategy_section;
                    let ratio = 250.0 / ui.available_width().max(250.0);
                    let mut chosen = None;
                    widgets::split(ui, ratio, |ui, side| {
                        if side == 0 {
                            widgets::card(ui, |ui| {
                                chosen = widgets::section_nav(ui, "strategy-nav", &entries, section);
                            });
                        } else {
                            ui.add_enabled_ui(!locked || section == 4, |ui| match section {
                                0 => self.strategy.ui(ui, &mut shell),
                                1 => self.intelligence.ai_ui(ui, &mut shell),
                                2 => self.intelligence.thresholds_ui(ui, &mut shell),
                                3 => self.intelligence.modules_ui(ui, &mut shell),
                                _ => self.intelligence.diagnostics_ui(ui, &mut shell),
                            });
                        }
                    });
                    if let Some(index) = chosen {
                        self.strategy_section = index;
                    }
                }
                Workspace::Research => {
                    ui.horizontal(|ui| {
                        let mut tab = if self.research_tab == ResearchTab::Backtest {
                            "backtest"
                        } else {
                            "training"
                        }
                        .to_string();
                        let tabs = [
                            ("backtest".to_string(), "Backtest".to_string(), Tone::Accent),
                            (
                                "training".to_string(),
                                "Treinamento de IA".to_string(),
                                Tone::Accent,
                            ),
                        ];
                        if widgets::segmented(ui, &mut tab, &tabs) {
                            self.research_tab = if tab == "backtest" {
                                ResearchTab::Backtest
                            } else {
                                ResearchTab::Training
                            };
                        }
                        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                            if self.training.is_running() {
                                widgets::badge(ui, "Treinamento em execução", Tone::Warning);
                            }
                            if self.backtest.is_running() {
                                widgets::badge(ui, "Backtest em execução", Tone::Warning);
                            }
                        });
                    });
                    ui.add_space(14.0);
                    match self.research_tab {
                        ResearchTab::Backtest => self.backtest.ui(ui, &mut shell),
                        ResearchTab::Training => self.training.ui(ui, &mut shell),
                    }
                }
                Workspace::Settings => {
                    let credentials_hint = if self.credentials.all_configured() {
                        "Testnet e real configuradas"
                    } else {
                        "Chaves da Binance"
                    };
                    let entries = [
                        NavEntry {
                            icon: Icon::Key,
                            label: "Credenciais",
                            hint: credentials_hint,
                            marker: self.credentials.has_draft().then_some(Tone::Warning),
                        },
                        NavEntry {
                            icon: Icon::Server,
                            label: "Núcleo Python",
                            hint: &core_hint,
                            marker: None,
                        },
                        NavEntry {
                            icon: Icon::Info,
                            label: "Sobre",
                            hint: "Versão e atalhos",
                            marker: None,
                        },
                    ];
                    let section = self.settings_section;
                    let ratio = 250.0 / ui.available_width().max(250.0);
                    let mut chosen = None;
                    let mut restart = false;
                    let mut show_shortcuts = false;
                    let status = shell.bridge.status().clone();
                    let hello = shell.bridge.hello().cloned();
                    let stderr = shell.bridge.stderr_tail();
                    widgets::split(ui, ratio, |ui, side| {
                        if side == 0 {
                            widgets::card(ui, |ui| {
                                chosen = widgets::section_nav(ui, "settings-nav", &entries, section);
                            });
                        } else {
                            match section {
                                0 => {
                                    ui.add_enabled_ui(!locked, |ui| self.credentials.ui(ui, &mut shell))
                                        .inner
                                }
                                1 => restart = core_section(ui, &status, hello.as_ref(), &stderr),
                                _ => show_shortcuts = about_section(ui, hello.as_ref()),
                            }
                        }
                    });
                    if let Some(index) = chosen {
                        self.settings_section = index;
                    }
                    if restart {
                        shell.bridge.restart();
                    }
                    if show_shortcuts {
                        pending = Some(Command::Shortcuts);
                    }
                }
            }
        }
        if saved {
            self.config_rev += 1;
        }
        if let Some(command) = pending {
            self.execute(command, &ui.ctx().clone());
        }
    }

    fn bridge_status_label(&self) -> (String, Tone) {
        match self.bridge.status() {
            Status::Ready => ("Conectado".into(), Tone::Success),
            Status::Starting => ("Conectando…".into(), Tone::Warning),
            Status::Down(_) => ("Desconectado".into(), Tone::Danger),
        }
    }

    fn core_banner(&mut self, ui: &mut Ui) {
        if let Status::Down(reason) = self.bridge.status().clone() {
            widgets::card(ui, |ui| {
                ui.horizontal(|ui| {
                    let (rect, _) = ui.allocate_exact_size(vec2(22.0, 22.0), Sense::hover());
                    icons::paint(ui.painter(), rect, Icon::Server, theme::DANGER);
                    ui.add(egui::Label::new(RichText::new(&reason).color(theme::TEXT)).wrap());
                });
                ui.add_space(8.0);
                ui.horizontal(|ui| {
                    if widgets::button(ui, Kind::Primary, Some(Icon::Refresh), "Reiniciar núcleo").clicked()
                    {
                        self.bridge.restart();
                    }
                    if widgets::button(ui, Kind::Ghost, Some(Icon::File), "Ver diagnóstico").clicked() {
                        self.workspace = Workspace::Settings;
                        self.settings_section = 1;
                    }
                });
            });
            ui.add_space(14.0);
        }
    }

    // ------------------------------------------------------------ dialogs

    fn dialogs(&mut self, ctx: &egui::Context) {
        let enabled: Vec<(Command, bool)> = Command::PALETTE
            .iter()
            .map(|command| (*command, self.enabled(*command)))
            .collect();
        let is_enabled = |command: Command| enabled.iter().any(|(c, on)| *c == command && *on);
        if let Some(command) = self.palette.show(ctx, is_enabled) {
            self.execute(command, ctx);
        }
        if self.show_shortcuts {
            let modal = egui::Modal::new(egui::Id::unique("shortcuts"))
                .backdrop_color(theme::BACKDROP)
                .frame(dialogs::modal_frame())
                .show(ctx, |ui| {
                    ui.set_width(520.0);
                    dialogs::dialog_title(ui, Icon::Bolt, Tone::Accent, "Atalhos de teclado");
                    for (command, shortcut) in Command::with_shortcuts() {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(command.label()).size(13.0).color(theme::TEXT));
                            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                                kbd(ui, &ctx.format_shortcut(&shortcut));
                            });
                        });
                    }
                    ui.add_space(12.0);
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        widgets::button(ui, Kind::Secondary, None, "Fechar").clicked()
                    })
                    .inner
                });
            if modal.inner || modal.should_close() {
                self.show_shortcuts = false;
            }
        }
        if self.show_about {
            let hello = self.bridge.hello().cloned();
            let modal = egui::Modal::new(egui::Id::unique("about"))
                .backdrop_color(theme::BACKDROP)
                .frame(dialogs::modal_frame())
                .show(ctx, |ui| {
                    ui.set_width(460.0);
                    about_section(ui, hello.as_ref());
                    ui.add_space(12.0);
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        widgets::button(ui, Kind::Secondary, None, "Fechar").clicked()
                    })
                    .inner
                });
            if modal.inner || modal.should_close() {
                self.show_about = false;
            }
        }
        if let Some(confirm) = &self.quit_confirm {
            match confirm.show(ctx) {
                Some(true) => {
                    self.backtest.kill();
                    self.training.kill();
                    self.quit_confirm = None;
                    self.allow_close = true;
                    ctx.send_viewport_cmd(ViewportCommand::Close);
                }
                Some(false) => self.quit_confirm = None,
                None => {}
            }
        }
    }
}

impl eframe::App for AlphoraApp {
    fn ui(&mut self, ui: &mut Ui, _frame: &mut eframe::Frame) {
        let started = std::time::Instant::now();
        let ctx = ui.ctx().clone();
        self.pump();
        self.handle_shortcuts(&ctx);
        self.guard_close(&ctx);

        let bar = |fill| Frame::new().fill(fill).inner_margin(Margin::symmetric(14, 0));
        egui::Panel::top("menu-bar")
            .exact_size(34.0)
            .resizable(false)
            .frame(bar(theme::CARD))
            .show(ui, |ui| self.menu_bar(ui));
        egui::Panel::top("command-bar")
            .exact_size(58.0)
            .resizable(false)
            .frame(bar(theme::CARD))
            .show(ui, |ui| self.command_bar(ui));
        egui::Panel::top("tab-strip")
            .exact_size(46.0)
            .resizable(false)
            .frame(bar(theme::CARD))
            .show(ui, |ui| self.tab_strip(ui));
        egui::Panel::bottom("status-bar")
            .exact_size(28.0)
            .resizable(false)
            .frame(bar(theme::CARD))
            .show(ui, |ui| self.status_bar(ui));
        if self.console_open {
            egui::Panel::bottom("console")
                .resizable(true)
                .default_size(280.0)
                .min_size(150.0)
                .frame(Frame::new().fill(theme::CARD).inner_margin(Margin::same(12)))
                .show(ui, |ui| self.console(ui));
        }

        let now = ctx.input(|i| i.time);
        let progress = ((now - self.switched_at) / TRANSITION).clamp(0.0, 1.0) as f32;
        let eased = 1.0 - (1.0 - progress).powi(3);
        if progress < 1.0 {
            ctx.request_repaint();
        }
        let central = egui::CentralPanel::default()
            .frame(Frame::new().fill(theme::BG))
            .show(ui, |ui| {
                ScrollArea::vertical()
                    .id_salt(("workspace-scroll", self.workspace))
                    .auto_shrink([false, false])
                    .show(ui, |ui| {
                        let width = ui.available_width();
                        let content = (width - 48.0).min(CONTENT_MAX_WIDTH);
                        let side = ((width - content) / 2.0).max(24.0);
                        ui.add_space(20.0 + 10.0 * (1.0 - eased));
                        ui.horizontal_top(|ui| {
                            ui.add_space(side);
                            ui.vertical(|ui| {
                                ui.set_width(content);
                                ui.multiply_opacity(0.25 + 0.75 * eased);
                                self.core_banner(ui);
                                self.workspace_ui(ui);
                                ui.add_space(90.0);
                            });
                        });
                    });
            });
        self.save_bar(&ctx, central.response.rect);
        self.dialogs(&ctx);
        self.toasts.show(&ctx);
        self.profiler.record(&ctx, started.elapsed());
    }

    fn on_exit(&mut self) {
        // Children are reaped by their job objects anyway; this makes it immediate.
        self.operation.kill();
        self.backtest.kill();
        self.training.kill();
    }

    fn clear_color(&self, _visuals: &egui::Visuals) -> [f32; 4] {
        theme::BG.to_normalized_gamma_f32()
    }
}

// ---------------------------------------------------------------- chrome pieces

fn workspace_tab(ui: &mut Ui, workspace: Workspace, selected: bool, marker: Option<Tone>) -> egui::Response {
    let font = if selected {
        theme::semibold(13.5)
    } else {
        theme::font(13.5)
    };
    let galley = ui
        .painter()
        .layout_no_wrap(workspace.label().to_string(), font, theme::TEXT);
    let marker_space = if marker.is_some() { 14.0 } else { 0.0 };
    let width = galley.size().x + 52.0 + marker_space;
    let height = ui.available_height().max(36.0);
    let (rect, response) = ui.allocate_exact_size(vec2(width, height), Sense::click());
    let hover = widgets::hover_amount(ui, &response, true);
    let painter = ui.painter();
    let pill = rect.shrink2(vec2(2.0, 6.0));
    if !selected && hover > 0.0 {
        painter.rect_filled(pill, 8, theme::alpha(theme::RAISED, (255.0 * hover) as u8));
    }
    let color = if selected {
        theme::ACCENT
    } else {
        widgets::lerp_color(theme::TEXT_MUTED, theme::TEXT, hover)
    };
    icons::paint(
        painter,
        icons::icon_rect(pos2(rect.left() + 22.0, rect.center().y), 17.0),
        workspace.icon(),
        color,
    );
    let text_color = if selected { theme::TEXT } else { color };
    let text_pos = pos2(rect.left() + 38.0, rect.center().y - galley.size().y / 2.0);
    painter.galley(text_pos, galley.clone(), text_color);
    if let Some(tone) = marker {
        painter.circle_filled(
            pos2(text_pos.x + galley.size().x + 9.0, rect.center().y - 5.0),
            3.5,
            tone.color(),
        );
    }
    let hint = format!(
        "{} ({})",
        workspace.description(),
        ui.ctx().format_shortcut(&workspace.shortcut())
    );
    response
        .on_hover_text(hint)
        .on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// Search-box look-alike that opens the command palette.
fn palette_trigger(ui: &mut Ui) -> bool {
    let (rect, response) = ui.allocate_exact_size(vec2(300.0, theme::CONTROL_HEIGHT), Sense::click());
    let hover = widgets::hover_amount(ui, &response, true);
    let painter = ui.painter();
    painter.rect_filled(rect, 9, widgets::lerp_color(theme::INPUT, theme::HOVER, hover));
    painter.rect_stroke(
        rect,
        9,
        Stroke::new(
            1.0,
            widgets::lerp_color(theme::BORDER_STRONG, theme::alpha(theme::ACCENT, 150), hover),
        ),
        StrokeKind::Inside,
    );
    icons::paint(
        painter,
        icons::icon_rect(rect.left_center() + vec2(18.0, 0.0), 16.0),
        Icon::Search,
        theme::TEXT_MUTED,
    );
    painter.text(
        rect.left_center() + vec2(34.0, 0.0),
        Align2::LEFT_CENTER,
        "Buscar telas e ações…",
        theme::font(13.0),
        theme::TEXT_FAINT,
    );
    let key = Rect::from_min_size(
        pos2(rect.right() - 62.0, rect.center().y - 11.0),
        vec2(54.0, 22.0),
    );
    painter.rect_filled(key, 6, theme::CARD);
    painter.rect_stroke(key, 6, Stroke::new(1.0, theme::BORDER), StrokeKind::Inside);
    painter.text(
        key.center(),
        Align2::CENTER_CENTER,
        "Ctrl K",
        theme::semibold(11.0),
        theme::TEXT_MUTED,
    );
    response.on_hover_cursor(egui::CursorIcon::Text).clicked()
}

fn status_item(ui: &mut Ui, tone: Tone, text: &str) {
    ui.horizontal(|ui| {
        ui.spacing_mut().item_spacing.x = 6.0;
        let (rect, _) = ui.allocate_exact_size(vec2(8.0, 14.0), Sense::hover());
        ui.painter().circle_filled(rect.center(), 3.5, tone.color());
        ui.label(RichText::new(text).size(11.5).color(theme::TEXT_MUTED));
    });
}

fn console_toggle(ui: &mut Ui, label: &str, warnings: usize, errors: usize) -> egui::Response {
    let font = theme::semibold(11.5);
    let label = ui
        .painter()
        .layout_no_wrap(label.to_string(), font.clone(), theme::TEXT_MUTED);
    let warn_color = if warnings > 0 {
        theme::WARNING
    } else {
        theme::TEXT_FAINT
    };
    let error_color = if errors > 0 {
        theme::DANGER
    } else {
        theme::TEXT_FAINT
    };
    let warn = ui
        .painter()
        .layout_no_wrap(warnings.to_string(), font.clone(), warn_color);
    let error = ui.painter().layout_no_wrap(errors.to_string(), font, error_color);
    let width = 10.0 + label.size().x + 12.0 + 14.0 + warn.size().x + 10.0 + 14.0 + error.size().x + 10.0;
    let (rect, response) = ui.allocate_exact_size(vec2(width, 22.0), Sense::click());
    let hover = widgets::hover_amount(ui, &response, true);
    let painter = ui.painter();
    painter.rect_filled(rect, 6, theme::alpha(theme::RAISED, (255.0 * hover) as u8));
    let y = rect.center().y;
    let mut x = rect.left() + 10.0;
    painter.galley(
        pos2(x, y - label.size().y / 2.0),
        label.clone(),
        theme::TEXT_MUTED,
    );
    x += label.size().x + 12.0;
    icons::paint(
        painter,
        icons::icon_rect(pos2(x + 6.0, y), 12.0),
        Icon::Warning,
        warn_color,
    );
    x += 14.0;
    painter.galley(pos2(x, y - warn.size().y / 2.0), warn.clone(), warn_color);
    x += warn.size().x + 10.0;
    icons::paint(
        painter,
        icons::icon_rect(pos2(x + 6.0, y), 12.0),
        Icon::Close,
        error_color,
    );
    x += 14.0;
    painter.galley(pos2(x, y - error.size().y / 2.0), error, error_color);
    response
        .on_hover_text(format!(
            "{warnings} aviso(s) e {errors} erro(s) nos logs · Ctrl+J abre o console"
        ))
        .on_hover_cursor(egui::CursorIcon::PointingHand)
}

fn dark_button(ui: &mut Ui, label: &str) -> egui::Response {
    let galley = ui
        .painter()
        .layout_no_wrap(label.to_string(), theme::semibold(13.0), egui::Color32::WHITE);
    let (rect, response) = ui.allocate_exact_size(
        vec2(galley.size().x + 26.0, theme::CONTROL_HEIGHT),
        Sense::click(),
    );
    let hover = widgets::hover_amount(ui, &response, true);
    ui.painter().rect_filled(
        rect,
        theme::RADIUS_SMALL,
        egui::Color32::from_white_alpha((20.0 + 30.0 * hover) as u8),
    );
    ui.painter()
        .galley(rect.center() - galley.size() / 2.0, galley, egui::Color32::WHITE);
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

fn kbd(ui: &mut Ui, text: &str) {
    let galley = ui
        .painter()
        .layout_no_wrap(text.to_string(), theme::semibold(11.5), theme::TEXT_MUTED);
    let (rect, _) = ui.allocate_exact_size(vec2(galley.size().x + 16.0, 24.0), Sense::hover());
    ui.painter().rect_filled(rect, 6, theme::RAISED);
    ui.painter()
        .rect_stroke(rect, 6, Stroke::new(1.0, theme::BORDER), StrokeKind::Inside);
    ui.painter()
        .galley(rect.center() - galley.size() / 2.0, galley, theme::TEXT_MUTED);
}

fn super_duration(duration: Duration) -> String {
    crate::pages::format_duration(duration)
}

/// Python core status, interpreter details and the bridge's stderr; returns "restart".
fn core_section(
    ui: &mut Ui,
    status: &Status,
    hello: Option<&crate::bridge::Hello>,
    stderr: &[String],
) -> bool {
    let mut restart = false;
    widgets::card(ui, |ui| {
        let (label, tone) = match status {
            Status::Ready => ("Conectado", Tone::Success),
            Status::Starting => ("Conectando", Tone::Warning),
            Status::Down(_) => ("Desconectado", Tone::Danger),
        };
        widgets::card_header(
            ui,
            Icon::Server,
            "Núcleo Python",
            "Processo que executa os serviços do Alphora",
            |ui| {
                widgets::badge(ui, label, tone);
            },
        );
        if let Status::Down(reason) = status {
            widgets::banner(ui, Tone::Danger, Icon::Warning, reason);
            ui.add_space(8.0);
        }
        if let Some(hello) = hello {
            widgets::key_value(
                ui,
                "Python",
                &format!("{} ({})", hello.python_version, hello.python),
            );
            widgets::key_value(ui, "Workspace", &hello.root);
            widgets::key_value(ui, "Run ID", &hello.run_id);
        }
        ui.add_space(10.0);
        if widgets::button(ui, Kind::Secondary, Some(Icon::Refresh), "Reiniciar núcleo").clicked() {
            restart = true;
        }
    });
    ui.add_space(14.0);
    widgets::card(ui, |ui| {
        widgets::card_header(
            ui,
            Icon::File,
            "Saída de diagnóstico",
            "Últimas linhas de stderr do núcleo",
            |_| {},
        );
        if stderr.is_empty() {
            widgets::empty_state(
                ui,
                Icon::Check,
                "Nenhuma mensagem",
                "O núcleo não reportou avisos.",
            );
        } else {
            widgets::text_lines(ui, "core-stderr", stderr, 320.0);
        }
    });
    restart
}

/// Product identity and versions; returns true when the user asks for the shortcuts.
fn about_section(ui: &mut Ui, hello: Option<&crate::bridge::Hello>) -> bool {
    let mut shortcuts = false;
    widgets::card(ui, |ui| {
        ui.horizontal(|ui| {
            let (rect, _) = ui.allocate_exact_size(vec2(56.0, 56.0), Sense::hover());
            icons::paint_logo(ui.painter(), rect, theme::CARD);
            ui.vertical(|ui| {
                ui.label(
                    RichText::new("Alphora")
                        .font(theme::semibold(24.0))
                        .color(theme::TEXT),
                );
                ui.label(
                    RichText::new(format!(
                        "Interface {} · Futures Engine",
                        env!("CARGO_PKG_VERSION")
                    ))
                    .size(13.0)
                    .color(theme::TEXT_MUTED),
                );
            });
        });
        ui.add_space(12.0);
        ui.label(
            RichText::new(
                "Interface nativa em Rust sobre o núcleo Python do Alphora: Governor, estratégias, IA probabilística, backtest e observabilidade.",
            )
            .size(13.0)
            .color(theme::TEXT),
        );
        if let Some(hello) = hello {
            ui.add_space(8.0);
            widgets::key_value(ui, "Núcleo", &format!("Python {}", hello.python_version));
        }
        ui.add_space(10.0);
        if widgets::button(ui, Kind::Secondary, Some(Icon::Bolt), "Atalhos de teclado").clicked() {
            shortcuts = true;
        }
    });
    shortcuts
}
