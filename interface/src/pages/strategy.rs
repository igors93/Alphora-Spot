//! Global strategy selection and its JSON parameters.

use egui::{RichText, Sense, Ui, vec2};
use serde::Deserialize;
use serde_json::{Value, json};

use super::{Shell, list_of, split_lines, str_of};
use crate::bridge::{Failure, PageId};
use crate::icons::Icon;
use crate::theme::{self, Tone};
use crate::widgets;

const PAGE: PageId = PageId::Strategy;

pub fn mode_label(mode: &str) -> &str {
    match mode {
        "FIXED" => "Estratégia fixa",
        "PROBABILISTIC_AI" => "IA probabilística",
        other => other,
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
struct Descriptor {
    qualified_name: String,
    module_name: String,
    class_name: String,
    valid: bool,
    contract: String,
    details: String,
}

#[derive(Clone, PartialEq, Default)]
struct Draft {
    selected: String,
    mode: String,
    params: String,
    apply_to_profiles: bool,
}

pub struct StrategyPage {
    descriptors: Vec<Descriptor>,
    current: String,
    draft: Draft,
    baseline: Draft,
    filter: String,
    details_lines: Vec<String>,
    params_error: Option<String>,
    seen_rev: u64,
    loaded: bool,
    loading: bool,
    saving: bool,
}

impl StrategyPage {
    pub fn new() -> Self {
        Self {
            descriptors: Vec::new(),
            current: String::new(),
            draft: Draft::default(),
            baseline: Draft::default(),
            filter: String::new(),
            details_lines: Vec::new(),
            params_error: None,
            seen_rev: u64::MAX,
            loaded: false,
            loading: false,
            saving: false,
        }
    }

    pub fn dirty(&self) -> bool {
        self.draft != self.baseline
    }

    pub fn discard(&mut self) {
        self.draft = self.baseline.clone();
        self.refresh_details();
        self.validate_params();
    }

    pub fn save(&mut self, shell: &mut Shell) {
        if !self.dirty() || self.saving {
            return;
        }
        if let Some(error) = &self.params_error {
            shell.toasts.error("Parâmetros inválidos", error.clone());
            return;
        }
        if self.draft.selected.is_empty() {
            shell
                .toasts
                .warn("Estratégia", "Selecione uma estratégia antes de salvar.");
            return;
        }
        self.saving = true;
        shell.call(
            PAGE,
            "save",
            "strategy.save",
            json!({
                "qualified_name": self.draft.selected,
                "selection_mode": self.draft.mode,
                "params_text": self.draft.params,
                "apply_to_profiles": self.draft.apply_to_profiles,
            }),
        );
    }

    pub fn rediscover(&mut self, shell: &mut Shell) {
        if !self.loading {
            self.loading = true;
            shell.call(PAGE, "discover", "strategy.discover", json!({}));
        }
    }

    pub fn summary(&self) -> String {
        if self.current.is_empty() {
            "—".into()
        } else {
            self.current
                .rsplit('.')
                .next()
                .unwrap_or(&self.current)
                .to_string()
        }
    }

    pub fn on_reply(&mut self, kind: &str, result: Result<Value, Failure>, shell: &mut Shell) {
        match (kind, result) {
            ("load", Ok(value)) => {
                self.loading = false;
                self.loaded = true;
                self.set_descriptors(&value);
                self.current = str_of(&value, "current");
                let selected = if self.descriptors.iter().any(|d| d.qualified_name == self.current) {
                    self.current.clone()
                } else {
                    self.descriptors
                        .iter()
                        .find(|d| d.valid)
                        .map(|d| d.qualified_name.clone())
                        .unwrap_or_default()
                };
                self.draft = Draft {
                    selected,
                    mode: str_of(&value, "selection_mode"),
                    params: str_of(&value, "params_text"),
                    apply_to_profiles: false,
                };
                self.baseline = self.draft.clone();
                self.refresh_details();
                self.validate_params();
            }
            ("discover", Ok(value)) => {
                self.loading = false;
                self.set_descriptors(&value);
                self.refresh_details();
                // Strategy files on disk changed: pages that list strategies (backtest)
                // must reload, otherwise a new strategy only shows up after a restart.
                shell.saved();
                shell.toasts.info(
                    "Estratégias recarregadas",
                    format!("{} estratégia(s) encontradas.", self.descriptors.len()),
                );
            }
            ("save", Ok(value)) => {
                self.saving = false;
                self.current = str_of(&value, "current");
                self.draft.params = str_of(&value, "params_text");
                self.draft.apply_to_profiles = false;
                self.baseline = self.draft.clone();
                shell.saved();
                shell
                    .toasts
                    .success("Estratégia salva", str_of(&value, "message"));
            }
            (kind, Err(failure)) => {
                if kind == "save" {
                    self.saving = false;
                } else {
                    self.loading = false;
                }
                shell.toasts.failure(&failure);
            }
            _ => {}
        }
    }

    fn set_descriptors(&mut self, value: &Value) {
        self.descriptors = list_of(value, "descriptors")
            .into_iter()
            .filter_map(|item| serde_json::from_value(item).ok())
            .collect();
    }

    fn refresh_details(&mut self) {
        self.details_lines = self
            .descriptors
            .iter()
            .find(|d| d.qualified_name == self.draft.selected)
            .map(|d| split_lines(&d.details))
            .unwrap_or_else(|| vec!["Selecione uma estratégia na lista.".into()]);
    }

    fn validate_params(&mut self) {
        let text = self.draft.params.trim();
        self.params_error = if text.is_empty() {
            None
        } else {
            match serde_json::from_str::<Value>(text) {
                Ok(Value::Object(_)) => None,
                Ok(_) => Some("Os parâmetros precisam formar um objeto JSON.".into()),
                Err(error) => Some(format!("JSON inválido: {error}")),
            }
        };
    }

    pub fn tick(&mut self, shell: &mut Shell) {
        if shell.bridge.is_ready() && self.seen_rev != shell.config_rev && !self.loading && !self.dirty() {
            self.seen_rev = shell.config_rev;
            self.loading = true;
            shell.call(PAGE, "load", "strategy.load", json!({}));
        }
    }

    pub fn ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        if !self.loaded {
            widgets::card(ui, |ui| widgets::skeleton_block(ui, 8));
            return;
        }
        widgets::split(ui, 0.42, |ui, side| {
            if side == 0 {
                self.list_card(ui, shell);
            } else {
                self.config_card(ui, shell);
            }
        });
    }

    fn list_card(&mut self, ui: &mut Ui, shell: &mut Shell) {
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Target,
                "Estratégias disponíveis",
                &format!("Em uso: {}", self.summary()),
                |ui| {
                    ui.add_enabled_ui(!self.loading && shell.bridge.is_ready(), |ui| {
                        if widgets::icon_button(ui, Icon::Refresh, "Procurar estratégias novamente").clicked()
                        {
                            self.rediscover(shell);
                        }
                    });
                    if self.loading {
                        widgets::spinner(ui, 16.0);
                    }
                },
            );
            widgets::text_input(ui, &mut self.filter, "Filtrar por nome ou módulo");
            ui.add_space(8.0);
            let needle = self.filter.to_lowercase();
            let mut clicked = None;
            egui::ScrollArea::vertical()
                .id_salt("strategy-list")
                .max_height(520.0)
                .show(ui, |ui| {
                    ui.spacing_mut().item_spacing.y = 4.0;
                    for descriptor in &self.descriptors {
                        if !needle.is_empty() && !descriptor.qualified_name.to_lowercase().contains(&needle) {
                            continue;
                        }
                        let selected = descriptor.qualified_name == self.draft.selected;
                        let in_use = descriptor.qualified_name == self.current;
                        let (rect, response) =
                            ui.allocate_exact_size(vec2(ui.available_width(), 54.0), Sense::click());
                        let hover = widgets::hover_amount(ui, &response, !shell.locked);
                        let painter = ui.painter();
                        if selected {
                            painter.rect_filled(rect, 10, theme::alpha(theme::ACCENT, 26));
                            painter.rect_stroke(
                                rect,
                                10,
                                egui::Stroke::new(1.0, theme::alpha(theme::ACCENT, 130)),
                                egui::StrokeKind::Inside,
                            );
                        } else {
                            painter.rect_filled(
                                rect,
                                10,
                                widgets::lerp_color(theme::INPUT, theme::HOVER, hover),
                            );
                        }
                        let tone = if descriptor.valid {
                            Tone::Success
                        } else {
                            Tone::Danger
                        };
                        painter.circle_filled(rect.left_center() + vec2(16.0, 0.0), 4.0, tone.color());
                        let width = rect.width() - 120.0;
                        let name = widgets::elided(
                            ui,
                            &descriptor.class_name,
                            theme::semibold(13.5),
                            theme::TEXT,
                            width,
                        );
                        let module = widgets::elided(
                            ui,
                            &descriptor.module_name,
                            theme::font(11.5),
                            theme::TEXT_MUTED,
                            width,
                        );
                        ui.painter().galley(rect.min + vec2(32.0, 9.0), name, theme::TEXT);
                        ui.painter()
                            .galley(rect.min + vec2(32.0, 30.0), module, theme::TEXT_MUTED);
                        if in_use {
                            let tag = egui::Rect::from_min_size(
                                rect.right_top() + vec2(-78.0, 16.0),
                                vec2(66.0, 22.0),
                            );
                            ui.painter().rect_filled(tag, 11, theme::alpha(theme::ACCENT, 30));
                            ui.painter().text(
                                tag.center(),
                                egui::Align2::CENTER_CENTER,
                                "Em uso",
                                theme::semibold(11.0),
                                theme::ACCENT,
                            );
                        }
                        if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() && !shell.locked
                        {
                            clicked = Some(descriptor.qualified_name.clone());
                        }
                    }
                });
            if let Some(name) = clicked {
                self.draft.selected = name;
                self.refresh_details();
            }
        });
    }

    fn config_card(&mut self, ui: &mut Ui, shell: &mut Shell) {
        let descriptor = self
            .descriptors
            .iter()
            .find(|d| d.qualified_name == self.draft.selected)
            .cloned();
        widgets::card(ui, |ui| {
            let title = descriptor
                .as_ref()
                .map(|d| d.class_name.as_str())
                .unwrap_or("Nenhuma estratégia");
            let module = descriptor.as_ref().map(|d| d.module_name.as_str()).unwrap_or("");
            widgets::card_header(ui, Icon::File, title, module, |ui| {
                if let Some(d) = &descriptor {
                    let tone = if !d.valid {
                        Tone::Danger
                    } else if d.contract == "Contrato completo" {
                        Tone::Success
                    } else {
                        Tone::Warning
                    };
                    widgets::badge(ui, &d.contract, tone);
                }
            });
            widgets::text_lines(ui, "strategy-details", &self.details_lines, 190.0);
            if descriptor.as_ref().is_some_and(|d| !d.valid) {
                ui.add_space(8.0);
                widgets::banner(
                    ui,
                    Tone::Danger,
                    Icon::Warning,
                    "Esta estratégia não pode ser usada: corrija o erro de importação ou o contrato.",
                );
            }
        });
        ui.add_space(14.0);
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Edit,
                "Configuração",
                "Modo de seleção e parâmetros da estratégia global",
                |_| {},
            );
            ui.add_enabled_ui(!shell.locked, |ui| {
                widgets::field(ui, "Modo de seleção", |ui| {
                    let options: Vec<(String, String, Tone)> = shell
                        .options
                        .strategy_modes
                        .iter()
                        .map(|m| (m.clone(), mode_label(m).to_string(), Tone::Accent))
                        .collect();
                    widgets::segmented(ui, &mut self.draft.mode, &options);
                });
                ui.add_space(8.0);
                widgets::field(ui, "Parâmetros (JSON)", |ui| {
                    if widgets::code_editor(ui, &mut self.draft.params, 8).changed() {
                        self.validate_params();
                    }
                });
                match &self.params_error {
                    Some(error) => {
                        ui.label(RichText::new(error).size(12.0).color(theme::DANGER));
                    }
                    None => {
                        ui.label(RichText::new("JSON válido").size(12.0).color(theme::SUCCESS));
                    }
                }
                ui.add_space(8.0);
                widgets::toggle(
                    ui,
                    &mut self.draft.apply_to_profiles,
                    "Aplicar estes parâmetros a todos os perfis",
                );
            });
        });
    }
}
