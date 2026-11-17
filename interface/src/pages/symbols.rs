//! Symbols workspace: master-detail profile editor with live validation, plus the
//! portfolio risk limits.

use std::time::{Duration, Instant};

use egui::{Rect, RichText, Sense, Stroke, StrokeKind, Ui, pos2, vec2};
use serde_json::{Value, json};

use super::{Form, Shell, bool_of, list_of, parse_f64, str_of, strings_of};
use crate::bridge::{Failure, PageId};
use crate::dialogs::Confirm;
use crate::icons::Icon;
use crate::theme::{self, Tone};
use crate::widgets::{self, Kind, PathDialog};

const PAGE: PageId = PageId::Symbols;
const ROWS_INTERVAL: Duration = Duration::from_secs(3);
const CHECK_DEBOUNCE: Duration = Duration::from_millis(450);

const GENERAL: [(&str, &str); 4] = [
    ("symbol", "Símbolo"),
    ("leverage", "Alavancagem"),
    ("position_notional_usdt", "Notional (USDT)"),
    ("max_exposure_usdt", "Exposição máxima (USDT)"),
];
const EXIT: [(&str, &str); 2] = [
    ("stop_loss_pct", "Stop loss (fração)"),
    ("take_profit_pct", "Take profit (fração)"),
];
const ATR: [(&str, &str); 3] = [
    ("atr_period", "Período ATR"),
    ("atr_stop_multiplier", "Multiplicador stop"),
    ("atr_take_multiplier", "Multiplicador take"),
];
const PROTECTION: [(&str, &str); 3] = [
    ("protection_policy", "Política de proteção"),
    ("protection_working_type", "Preço de referência"),
    ("recovery_action", "Ação de recuperação"),
];
const LIMITS: [(&str, &str); 5] = [
    ("max_daily_loss_usdt", "Perda diária máx. (USDT)"),
    ("max_daily_trades", "Trades por dia"),
    ("max_consecutive_losses", "Perdas consecutivas"),
    ("cooldown_after_loss_seconds", "Cooldown após perda (s)"),
    ("max_position_hold_seconds", "Tempo máx. de posição (s)"),
];
const PORTFOLIO_GROUPS: [(&str, &[&str]); 3] = [
    (
        "Posições",
        &[
            "max_open_positions",
            "max_new_positions_per_cycle",
            "max_correlated_positions",
            "governor_max_symbols_per_cycle",
        ],
    ),
    (
        "Exposição (USDT)",
        &[
            "max_total_exposure_usdt",
            "max_long_exposure_usdt",
            "max_short_exposure_usdt",
            "max_symbol_exposure_usdt",
            "max_group_exposure_usdt",
        ],
    ),
    (
        "Resultado diário (USDT)",
        &["max_daily_loss_usdt", "stop_after_daily_profit_usdt"],
    ),
];

#[derive(Clone, Copy, PartialEq, Eq)]
enum Tab {
    Profiles,
    Portfolio,
}

pub struct SymbolsPage {
    profiles: Vec<Value>,
    baseline_profiles: Vec<Value>,
    portfolio: Form,
    baseline_portfolio: Form,
    rows: Vec<Value>,
    state: Value,
    selected: Option<usize>,
    editor: Form,
    editor_index: Option<usize>,
    errors: Vec<String>,
    check_due: Option<Instant>,
    checking: bool,
    filter: String,
    tab: Tab,
    confirm_remove: Option<Confirm<usize>>,
    seen_rev: u64,
    loaded: bool,
    loading: bool,
    saving: bool,
    rows_loading: bool,
    rows_next: Instant,
}

impl SymbolsPage {
    pub fn new() -> Self {
        Self {
            profiles: Vec::new(),
            baseline_profiles: Vec::new(),
            portfolio: Form::default(),
            baseline_portfolio: Form::default(),
            rows: Vec::new(),
            state: Value::Null,
            selected: None,
            editor: Form::default(),
            editor_index: None,
            errors: Vec::new(),
            check_due: None,
            checking: false,
            filter: String::new(),
            tab: Tab::Profiles,
            confirm_remove: None,
            seen_rev: u64::MAX,
            loaded: false,
            loading: false,
            saving: false,
            rows_loading: false,
            rows_next: Instant::now(),
        }
    }

    pub fn dirty(&self) -> bool {
        self.profiles != self.baseline_profiles || self.portfolio != self.baseline_portfolio
    }

    pub fn save(&mut self, shell: &mut Shell) {
        if self.saving || !self.dirty() {
            return;
        }
        self.saving = true;
        let portfolio = self.portfolio.to_value();
        shell.call(
            PAGE,
            "save",
            "symbols.save",
            json!({"profiles": self.profiles, "portfolio": portfolio}),
        );
    }

    pub fn discard(&mut self) {
        self.profiles = self.baseline_profiles.clone();
        self.portfolio = self.baseline_portfolio.clone();
        self.select(self.selected.filter(|&i| i < self.profiles.len()).or(Some(0)));
        self.rows_next = Instant::now();
    }

    pub fn on_reply(&mut self, kind: &str, result: Result<Value, Failure>, shell: &mut Shell) {
        match (kind, result) {
            ("load", Ok(value)) => {
                self.loading = false;
                self.loaded = true;
                self.apply_loaded(&value);
            }
            ("save", Ok(value)) => {
                self.saving = false;
                self.apply_loaded(&value);
                shell.saved();
                shell
                    .toasts
                    .success("Símbolos salvos", "Perfis e risco do portfólio foram salvos.");
            }
            ("rows", Ok(value)) => {
                self.rows_loading = false;
                self.rows = list_of(&value, "rows");
                self.state = value.get("state").cloned().unwrap_or(Value::Null);
            }
            ("new" | "duplicate", Ok(value)) => {
                self.profiles.push(value["profile"].clone());
                self.select(Some(self.profiles.len() - 1));
                self.tab = Tab::Profiles;
                self.rows_next = Instant::now();
            }
            ("check", Ok(value)) => {
                self.checking = false;
                self.errors = strings_of(&value, "errors");
            }
            ("files_state", Ok(value)) => shell.open_path(std::path::Path::new(&str_of(&value, "state"))),
            ("files_journal", Ok(value)) => shell.open_path(std::path::Path::new(&str_of(&value, "journal"))),
            (kind, Err(failure)) => {
                match kind {
                    "load" => self.loading = false,
                    "save" => self.saving = false,
                    "rows" => self.rows_loading = false,
                    "check" => self.checking = false,
                    _ => {}
                }
                if kind != "rows" && kind != "check" {
                    shell.toasts.failure(&failure);
                }
            }
            _ => {}
        }
    }

    fn apply_loaded(&mut self, value: &Value) {
        self.profiles = list_of(value, "profiles");
        self.baseline_profiles = self.profiles.clone();
        self.portfolio = Form::from_value(value.get("portfolio").unwrap_or(&Value::Null));
        self.baseline_portfolio = self.portfolio.clone();
        let selected = self
            .selected
            .unwrap_or(0)
            .min(self.profiles.len().saturating_sub(1));
        self.select((!self.profiles.is_empty()).then_some(selected));
        self.rows_next = Instant::now();
    }

    fn select(&mut self, index: Option<usize>) {
        self.selected = index.filter(|&i| i < self.profiles.len());
        self.editor_index = self.selected;
        self.editor = self
            .selected
            .map(|i| Form::from_value(&self.profiles[i]))
            .unwrap_or_default();
        self.errors.clear();
        self.check_due = None;
    }

    pub fn tick(&mut self, shell: &mut Shell, visible: bool) {
        if !shell.bridge.is_ready() {
            return;
        }
        if self.seen_rev != shell.config_rev && !self.loading && !self.dirty() {
            self.seen_rev = shell.config_rev;
            self.loading = true;
            shell.call(PAGE, "load", "symbols.load", json!({}));
        }
        if visible && self.loaded && !self.rows_loading && Instant::now() >= self.rows_next {
            self.rows_loading = true;
            self.rows_next = Instant::now() + ROWS_INTERVAL;
            shell.call(PAGE, "rows", "symbols.rows", json!({"profiles": self.profiles}));
        }
        if let (Some(due), Some(index)) = (self.check_due, self.editor_index) {
            if Instant::now() >= due && !self.checking && index < self.profiles.len() {
                self.check_due = None;
                self.checking = true;
                shell.call(
                    PAGE,
                    "check",
                    "symbols.check",
                    json!({"profile": self.profiles[index], "profiles": self.profiles, "exclude_index": index}),
                );
            } else if Instant::now() < due {
                shell.bridge_ctx().request_repaint_after(due - Instant::now());
            }
        }
        if visible {
            shell.bridge_ctx().request_repaint_after(ROWS_INTERVAL);
        }
        if let Some(confirm) = &self.confirm_remove {
            match confirm.show(&shell.bridge_ctx()) {
                Some(true) => {
                    let index = confirm.payload;
                    self.confirm_remove = None;
                    if index < self.profiles.len() && self.profiles.len() > 1 {
                        self.profiles.remove(index);
                        self.select(Some(index.min(self.profiles.len() - 1)));
                        self.rows_next = Instant::now();
                    }
                }
                Some(false) => self.confirm_remove = None,
                None => {}
            }
        }
    }

    // ------------------------------------------------------------ view

    pub fn ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        ui.horizontal(|ui| {
            let mut tab = if self.tab == Tab::Profiles {
                "profiles"
            } else {
                "portfolio"
            }
            .to_string();
            let tabs = [
                (
                    "profiles".to_string(),
                    format!("Perfis · {}", self.profiles.len()),
                    Tone::Accent,
                ),
                (
                    "portfolio".to_string(),
                    "Risco do portfólio".to_string(),
                    Tone::Accent,
                ),
            ];
            if widgets::segmented(ui, &mut tab, &tabs) {
                self.tab = if tab == "profiles" {
                    Tab::Profiles
                } else {
                    Tab::Portfolio
                };
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if widgets::icon_button(ui, Icon::Server, "Abrir estado do Governor").clicked() {
                    let path = shell.root().join("runtime").join("governor_state.json");
                    shell.open_path(&path);
                }
                let (label, tone) = governor_state(&self.state);
                widgets::badge(ui, &label, tone);
                let enabled = self.profiles.iter().filter(|p| bool_of(p, "enabled")).count();
                widgets::badge(ui, &format!("{enabled} habilitado(s)"), Tone::Success);
            });
        });
        ui.add_space(14.0);
        if shell.locked {
            widgets::banner(
                ui,
                Tone::Warning,
                Icon::Lock,
                "Operação em andamento: perfis e risco ficam somente leitura.",
            );
            ui.add_space(12.0);
        }
        if !self.loaded {
            widgets::card(ui, |ui| widgets::skeleton_block(ui, 8));
            return;
        }
        match self.tab {
            Tab::Profiles => {
                widgets::split(ui, 0.3, |ui, side| {
                    if side == 0 {
                        self.list(ui, shell);
                    } else {
                        self.detail(ui, shell);
                    }
                });
            }
            Tab::Portfolio => self.portfolio_ui(ui, shell),
        }
    }

    fn list(&mut self, ui: &mut Ui, shell: &mut Shell) {
        widgets::card(ui, |ui| {
            ui.add_enabled_ui(!shell.locked && shell.bridge.is_ready(), |ui| {
                ui.horizontal(|ui| {
                    if widgets::button(ui, Kind::Primary, Some(Icon::Plus), "Novo símbolo").clicked() {
                        shell.call(PAGE, "new", "symbols.new", json!({}));
                    }
                });
            });
            ui.add_space(10.0);
            widgets::text_input(ui, &mut self.filter, "Filtrar símbolos");
            ui.add_space(8.0);
            let needle = self.filter.trim().to_uppercase();
            let mut clicked = None;
            let mut toggled = None;
            egui::ScrollArea::vertical()
                .id_salt("symbols-list")
                .max_height(560.0)
                .show(ui, |ui| {
                    ui.spacing_mut().item_spacing.y = 4.0;
                    for (index, profile) in self.profiles.iter().enumerate() {
                        let symbol = str_of(profile, "symbol");
                        if !needle.is_empty() && !symbol.contains(&needle) {
                            continue;
                        }
                        let row = self.rows.get(index).cloned().unwrap_or(Value::Null);
                        match symbol_row(
                            ui,
                            index,
                            profile,
                            &row,
                            self.selected == Some(index),
                            !shell.locked,
                        ) {
                            RowAction::Select => clicked = Some(index),
                            RowAction::Toggle => toggled = Some(index),
                            RowAction::None => {}
                        }
                    }
                });
            if let Some(index) = toggled {
                let enabled = !bool_of(&self.profiles[index], "enabled");
                self.profiles[index]["enabled"] = Value::Bool(enabled);
                if self.editor_index == Some(index) {
                    *self.editor.flag("enabled") = enabled;
                }
                self.rows_next = Instant::now();
            }
            if let Some(index) = clicked {
                self.select(Some(index));
            }
        });
    }

    fn detail(&mut self, ui: &mut Ui, shell: &mut Shell) {
        let Some(index) = self.selected.filter(|&i| i < self.profiles.len()) else {
            widgets::card(ui, |ui| {
                widgets::empty_state(
                    ui,
                    Icon::Coins,
                    "Nenhum símbolo selecionado",
                    "Escolha um perfil na lista ou crie um novo.",
                );
            });
            return;
        };
        let row = self.rows.get(index).cloned().unwrap_or(Value::Null);
        let symbol = str_of(&self.profiles[index], "symbol");
        let enabled = bool_of(&self.profiles[index], "enabled");

        widgets::card(ui, |ui| {
            ui.horizontal(|ui| {
                ui.vertical(|ui| {
                    ui.spacing_mut().item_spacing.y = 2.0;
                    let title = if symbol.is_empty() {
                        "Novo símbolo"
                    } else {
                        &symbol
                    };
                    ui.label(
                        RichText::new(title)
                            .font(theme::semibold(24.0))
                            .color(theme::TEXT),
                    );
                    ui.horizontal(|ui| {
                        let status = if enabled {
                            str_of(&row, "status")
                        } else {
                            "DISABLED".into()
                        };
                        let tone = if enabled {
                            Tone::parse(&str_of(&row, "tone"))
                        } else {
                            Tone::Muted
                        };
                        widgets::badge(ui, if status.is_empty() { "-" } else { &status }, tone);
                        widgets::badge(
                            ui,
                            if enabled { "Habilitado" } else { "Desabilitado" },
                            if enabled { Tone::Success } else { Tone::Muted },
                        );
                    });
                });
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.add_enabled_ui(!shell.locked, |ui| {
                        if widgets::icon_button(ui, Icon::Trash, "Remover perfil").clicked() {
                            if self.profiles.len() <= 1 {
                                shell.toasts.warn(
                                    "Remoção bloqueada",
                                    "Mantenha ao menos um perfil de símbolo configurado.",
                                );
                            } else {
                                let name = if symbol.is_empty() {
                                    "sem nome".to_string()
                                } else {
                                    symbol.clone()
                                };
                                self.confirm_remove = Some(Confirm::new(
                                    "Remover perfil",
                                    format!("Remover o perfil {name}? A alteração só vale depois de salvar."),
                                    "Remover",
                                    index,
                                ));
                            }
                        }
                        if widgets::icon_button(ui, Icon::Duplicate, "Duplicar perfil").clicked() {
                            shell.call(
                                PAGE,
                                "duplicate",
                                "symbols.duplicate",
                                json!({"profile": self.profiles[index]}),
                            );
                        }
                    });
                    if widgets::icon_button(ui, Icon::File, "Abrir journal do símbolo").clicked() {
                        shell.call(PAGE, "files_journal", "symbols.files", json!({"symbol": symbol}));
                    }
                    if widgets::icon_button(ui, Icon::Open, "Abrir estado do símbolo").clicked() {
                        shell.call(PAGE, "files_state", "symbols.files", json!({"symbol": symbol}));
                    }
                });
            });
            ui.add_space(12.0);
            runtime_strip(ui, &row);
        });
        ui.add_space(14.0);

        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Edit,
                "Configuração do perfil",
                "As mudanças ficam pendentes até salvar (Ctrl+S)",
                |ui| {
                    if self.checking {
                        widgets::spinner(ui, 16.0);
                    } else if self.errors.is_empty() && self.check_due.is_none() && self.dirty() {
                        widgets::badge(ui, "Válido", Tone::Success);
                    }
                },
            );
            ui.add_enabled_ui(!shell.locked, |ui| self.editor_ui(ui, shell));
            if !self.errors.is_empty() {
                ui.add_space(10.0);
                let text = self
                    .errors
                    .iter()
                    .map(|e| format!("• {e}"))
                    .collect::<Vec<_>>()
                    .join("\n");
                widgets::banner(ui, Tone::Danger, Icon::Warning, &text);
            }
        });

        // Write the edited fields back; a change schedules a live validation.
        if let Some(index) = self.editor_index.filter(|&i| i < self.profiles.len()) {
            let mut merged = self.profiles[index].clone();
            if let (Some(target), Value::Object(edits)) = (merged.as_object_mut(), self.editor.to_value()) {
                for (key, value) in edits {
                    let value = if key == "symbol" {
                        Value::String(value.as_str().unwrap_or_default().trim().to_uppercase())
                    } else {
                        value
                    };
                    target.insert(key, value);
                }
            }
            if merged != self.profiles[index] {
                self.profiles[index] = merged;
                self.check_due = Some(Instant::now() + CHECK_DEBOUNCE);
            }
        }
    }

    fn editor_ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        let form = &mut self.editor;
        section_title(ui, "GERAL");
        widgets::toggle(ui, form.flag("enabled"), "Operar este símbolo");
        ui.add_space(6.0);
        text_fields(ui, form, &GENERAL);
        widgets::field(ui, "Tipo de margem", |ui| {
            widgets::combo(
                ui,
                "margin_type",
                form.text("margin_type"),
                shell.options.choices("margin_type"),
            );
        });

        section_title(ui, "SAÍDA");
        let columns = widgets::columns_for(ui, 220.0, 3);
        widgets::grid(ui, columns, 3, |ui, index| match index {
            0 => widgets::field(ui, "Modelo de saída", |ui| {
                widgets::combo(
                    ui,
                    "exit_model",
                    form.text("exit_model"),
                    shell.options.choices("exit_model"),
                );
            }),
            _ => {
                let (key, label) = EXIT[index - 1];
                widgets::field(ui, label, |ui| {
                    widgets::text_input(ui, form.text(key), "0.01");
                });
            }
        });
        let atr = form.get("exit_model") == "ATR";
        ui.add_enabled_ui(atr, |ui| text_fields(ui, form, &ATR))
            .response
            .on_disabled_hover_text("Disponível com o modelo de saída ATR.");

        section_title(ui, "PROTEÇÃO");
        let columns = widgets::columns_for(ui, 220.0, 3);
        widgets::grid(ui, columns, PROTECTION.len(), |ui, index| {
            let (key, label) = PROTECTION[index];
            widgets::field(ui, label, |ui| {
                widgets::combo(ui, key, form.text(key), shell.options.choices(key));
            });
        });

        section_title(ui, "LIMITES (0 = SEM LIMITE)");
        text_fields(ui, form, &LIMITS);

        section_title(ui, "INTELIGÊNCIA ARTIFICIAL");
        widgets::toggle(
            ui,
            form.flag("ai_use_symbol_refined_model"),
            "Usar modelo refinado deste símbolo",
        );
        ui.add_space(6.0);
        widgets::field(ui, "Modelo refinado (vazio = usa o global)", |ui| {
            widgets::path_input(
                ui,
                form.text("ai_refined_model_path"),
                "models/ai/refined/...",
                PathDialog::Model,
            );
        });
    }

    fn portfolio_ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        let labels: Vec<(String, String)> = shell
            .options
            .portfolio_fields
            .iter()
            .map(|f| (f.key.clone(), f.label.clone()))
            .collect();
        let label_of = |key: &str| {
            labels
                .iter()
                .find(|(k, _)| k == key)
                .map(|(_, l)| l.clone())
                .unwrap_or_else(|| key.to_string())
        };
        ui.add_enabled_ui(!shell.locked, |ui| {
            for (title, keys) in PORTFOLIO_GROUPS {
                widgets::card(ui, |ui| {
                    widgets::card_header(ui, Icon::Lock, title, "0 significa sem limite", |_| {});
                    let columns = widgets::columns_for(ui, 240.0, 4);
                    widgets::grid(ui, columns, keys.len(), |ui, index| {
                        let key = keys[index];
                        widgets::field(ui, &label_of(key), |ui| {
                            widgets::text_input(ui, self.portfolio.text(key), "0");
                        });
                    });
                });
                ui.add_space(14.0);
            }
            widgets::card(ui, |ui| {
                widgets::card_header(
                    ui,
                    Icon::Check,
                    "Regras",
                    "Como o Governor combina posições",
                    |_| {},
                );
                for key in [
                    "allow_multiple_same_direction",
                    "allow_multiple_positions_same_symbol",
                ] {
                    widgets::toggle(ui, self.portfolio.flag(key), &label_of(key));
                    ui.add_space(4.0);
                }
            });
        });
    }
}

enum RowAction {
    None,
    Select,
    Toggle,
}

/// One entry of the symbol list: status dot, symbol, position summary and an enable switch.
fn symbol_row(
    ui: &mut Ui,
    index: usize,
    profile: &Value,
    row: &Value,
    selected: bool,
    editable: bool,
) -> RowAction {
    let (rect, response) = ui.allocate_exact_size(vec2(ui.available_width(), 58.0), Sense::click());
    let hover = widgets::hover_amount(ui, &response, true);
    let enabled = bool_of(profile, "enabled");
    let painter = ui.painter();
    if selected {
        painter.rect_filled(rect, 10, theme::alpha(theme::ACCENT, 26));
        painter.rect_stroke(
            rect,
            10,
            Stroke::new(1.0, theme::alpha(theme::ACCENT, 120)),
            StrokeKind::Inside,
        );
    } else {
        painter.rect_filled(rect, 10, widgets::lerp_color(theme::INPUT, theme::HOVER, hover));
    }
    let tone = if enabled {
        Tone::parse(&str_of(row, "tone"))
    } else {
        Tone::Muted
    };
    painter.circle_filled(pos2(rect.left() + 16.0, rect.center().y), 4.5, tone.color());

    let symbol = str_of(profile, "symbol");
    let (title, title_color) = if symbol.is_empty() {
        ("Novo símbolo".to_string(), theme::TEXT_MUTED)
    } else {
        (symbol, if enabled { theme::TEXT } else { theme::TEXT_MUTED })
    };
    let text_width = rect.width() - 90.0;
    let title = widgets::elided(ui, &title, theme::semibold(14.0), title_color, text_width);
    ui.painter()
        .galley(pos2(rect.left() + 30.0, rect.top() + 10.0), title, title_color);

    let position = str_of(row, "position");
    let pnl = str_of(row, "unrealized_pnl");
    let (subtitle, color) = if !enabled {
        ("Desabilitado".to_string(), theme::TEXT_FAINT)
    } else if matches!(position.as_str(), "" | "NONE") {
        (
            format!("Sem posição · {}", non_empty(&str_of(row, "status"))),
            theme::TEXT_MUTED,
        )
    } else {
        let value = parse_f64(&pnl).unwrap_or_default();
        let color = if value > 0.0 {
            theme::SUCCESS
        } else if value < 0.0 {
            theme::DANGER
        } else {
            theme::TEXT_MUTED
        };
        (
            format!("{position} {} · PnL {pnl}", str_of(row, "quantity")),
            color,
        )
    };
    let subtitle = widgets::elided(ui, &subtitle, theme::font(12.0), color, text_width);
    ui.painter()
        .galley(pos2(rect.left() + 30.0, rect.top() + 32.0), subtitle, color);

    // Enable switch on the right, its own click target.
    let switch = Rect::from_center_size(pos2(rect.right() - 30.0, rect.center().y), vec2(34.0, 18.0));
    let switch_response = ui.interact(
        switch.expand(4.0),
        ui.scope_id().with(("symbol-switch", index)),
        Sense::click(),
    );
    let on = ui.ctx().animate_bool_with_time(switch_response.id, enabled, 0.12);
    let fill = widgets::lerp_color(theme::BORDER_STRONG, theme::ACCENT_STRONG, on);
    ui.painter()
        .rect_filled(switch, 9, if editable { fill } else { theme::alpha(fill, 110) });
    let knob = egui::lerp(switch.left() + 9.0..=switch.right() - 9.0, on);
    ui.painter()
        .circle_filled(pos2(knob, switch.center().y), 6.5, egui::Color32::WHITE);

    if editable
        && switch_response
            .on_hover_text(if enabled { "Desabilitar" } else { "Habilitar" })
            .clicked()
    {
        RowAction::Toggle
    } else if response.clicked() {
        RowAction::Select
    } else {
        RowAction::None
    }
}

fn runtime_strip(ui: &mut Ui, row: &Value) {
    let pnl = str_of(row, "unrealized_pnl");
    let pnl_color = match parse_f64(&pnl) {
        Some(v) if v > 0.0 => theme::SUCCESS,
        Some(v) if v < 0.0 => theme::DANGER,
        _ => theme::TEXT,
    };
    let stats = [
        ("Último preço", str_of(row, "last_price"), theme::TEXT),
        ("Posição", str_of(row, "position"), theme::TEXT),
        ("Quantidade", str_of(row, "quantity"), theme::TEXT),
        ("Entrada", str_of(row, "entry_price"), theme::TEXT),
        ("PnL não realizado", pnl, pnl_color),
        ("Score", str_of(row, "score"), theme::TEXT),
    ];
    let columns = widgets::columns_for(ui, 120.0, 6);
    widgets::grid(ui, columns, stats.len(), |ui, index| {
        let (label, value, color) = &stats[index];
        widgets::mini_stat(ui, label, non_empty(value), *color);
    });
    ui.add_space(8.0);
    let error = str_of(row, "error");
    let details = [
        ("Modelo IA", str_of(row, "ai_model")),
        ("Próxima checagem", str_of(row, "next_check")),
        (
            "Motivo",
            if error.is_empty() {
                str_of(row, "reason")
            } else {
                error.clone()
            },
        ),
    ];
    let text = details
        .iter()
        .filter(|(_, value)| !value.is_empty())
        .map(|(label, value)| format!("{label}: {value}"))
        .collect::<Vec<_>>()
        .join("   ·   ");
    if !text.is_empty() {
        let color = if error.is_empty() {
            theme::TEXT_MUTED
        } else {
            theme::DANGER
        };
        ui.add(egui::Label::new(RichText::new(text).size(12.0).color(color)).truncate());
    }
}

fn governor_state(state: &Value) -> (String, Tone) {
    if !str_of(state, "error").is_empty() {
        ("Estado do Governor inválido".into(), Tone::Danger)
    } else if bool_of(state, "available") {
        (
            format!("Governor · {}", str_of(state, "updated_at")),
            Tone::Accent,
        )
    } else {
        ("Governor sem estado".into(), Tone::Muted)
    }
}

fn section_title(ui: &mut Ui, title: &str) {
    ui.add_space(12.0);
    ui.label(
        RichText::new(title)
            .font(theme::semibold(11.0))
            .color(theme::TEXT_FAINT)
            .extra_letter_spacing(0.8),
    );
    ui.add_space(4.0);
}

fn text_fields(ui: &mut Ui, form: &mut Form, fields: &[(&str, &str)]) {
    let columns = widgets::columns_for(ui, 200.0, 3);
    widgets::grid(ui, columns, fields.len(), |ui, index| {
        let (key, label) = fields[index];
        widgets::field(ui, label, |ui| {
            widgets::text_input(ui, form.text(key), "");
        });
    });
}

fn non_empty(value: &str) -> &str {
    if value.trim().is_empty() { "—" } else { value }
}
