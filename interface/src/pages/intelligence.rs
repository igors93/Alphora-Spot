//! Intelligent flow: activation level, probabilistic AI model, thresholds, operational
//! modules and diagnostics. Rendered as sections of the "Estratégia e IA" workspace.

use egui::{RichText, Ui};
use serde::Deserialize;
use serde_json::{Value, json};

use super::{Form, Shell, list_of, split_lines, str_of};
use crate::bridge::{Failure, PageId};
use crate::icons::Icon;
use crate::theme::{self, Tone};
use crate::widgets::{self, Kind, PathDialog};

const PAGE: PageId = PageId::Intelligence;

#[derive(Clone, Debug, Default, Deserialize)]
struct ModelProfile {
    label: String,
    model_id: String,
    path: String,
    timeframe: String,
    required_history: String,
    input_contract_valid: bool,
    input_contract_status: String,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DiagTab {
    Layers,
    Review,
    Processes,
    Cache,
}

pub struct IntelligencePage {
    form: Form,
    baseline: Form,
    execution_mode: String,
    models: Vec<ModelProfile>,
    model_status: (String, Tone),
    diagnostics: Value,
    diag_lines: Vec<String>,
    diag_tab: DiagTab,
    seen_rev: u64,
    loaded: bool,
    loading: bool,
    saving: bool,
    validating: bool,
    diag_loading: bool,
}

impl IntelligencePage {
    pub fn new() -> Self {
        Self {
            form: Form::default(),
            baseline: Form::default(),
            execution_mode: "DRY_RUN".into(),
            models: Vec::new(),
            model_status: ("Não validado".into(), Tone::Neutral),
            diagnostics: Value::Null,
            diag_lines: Vec::new(),
            diag_tab: DiagTab::Layers,
            seen_rev: u64::MAX,
            loaded: false,
            loading: false,
            saving: false,
            validating: false,
            diag_loading: false,
        }
    }

    pub fn dirty(&self) -> bool {
        self.form != self.baseline
    }

    pub fn discard(&mut self) {
        self.form = self.baseline.clone();
    }

    pub fn ai_enabled(&self) -> bool {
        self.form.is_on("ai_probabilistic_enabled")
    }

    pub fn save(&mut self, shell: &mut Shell) {
        if !self.dirty() || self.saving {
            return;
        }
        self.saving = true;
        shell.call(
            PAGE,
            "save",
            "intelligence.save",
            json!({"values": self.form.to_value()}),
        );
    }

    pub fn validate_model(&mut self, shell: &mut Shell) {
        if self.validating {
            return;
        }
        self.validating = true;
        shell.call(
            PAGE,
            "validate",
            "intelligence.validate_model",
            json!({
                "path": self.form.get("ai_global_model_path"),
                "require_approved": self.form.is_on("ai_require_model_approved"),
            }),
        );
    }

    pub fn refresh_diagnostics(&mut self, shell: &mut Shell) {
        self.diag_loading = true;
        shell.call(
            PAGE,
            "diagnostics",
            "intelligence.diagnostics",
            json!({"values": self.form.to_value()}),
        );
    }

    pub fn on_reply(&mut self, kind: &str, result: Result<Value, Failure>, shell: &mut Shell) {
        match (kind, result) {
            ("load" | "save", Ok(value)) => {
                let saved = kind == "save";
                self.loading = false;
                self.saving = false;
                self.loaded = true;
                self.execution_mode = str_of(&value, "execution_mode");
                self.form = Form::from_value(value.get("values").unwrap_or(&Value::Null));
                self.baseline = self.form.clone();
                self.set_models(&value);
                self.model_status = ("Não validado".into(), Tone::Neutral);
                self.refresh_diagnostics(shell);
                if saved {
                    shell.saved();
                    shell
                        .toasts
                        .success("Inteligência salva", "Configurações inteligentes atualizadas.");
                }
            }
            ("models", Ok(value)) => self.set_models(&value),
            ("validate", Ok(value)) => {
                self.validating = false;
                let ok = super::bool_of(&value, "ok");
                self.model_status = (
                    str_of(&value, "status"),
                    if ok { Tone::Success } else { Tone::Danger },
                );
            }
            ("diagnostics", Ok(value)) => {
                self.diag_loading = false;
                self.diagnostics = value;
                self.refresh_diag_lines();
            }
            (kind, Err(failure)) => {
                match kind {
                    "load" => self.loading = false,
                    "save" => self.saving = false,
                    "validate" => self.validating = false,
                    "diagnostics" => self.diag_loading = false,
                    _ => {}
                }
                shell.toasts.failure(&failure);
            }
            _ => {}
        }
    }

    fn set_models(&mut self, value: &Value) {
        self.models = list_of(value, "models")
            .into_iter()
            .filter_map(|item| serde_json::from_value(item).ok())
            .collect();
    }

    fn refresh_diag_lines(&mut self) {
        let key = match self.diag_tab {
            DiagTab::Layers => "layers",
            DiagTab::Review => "operation_review",
            DiagTab::Processes => "process_review",
            DiagTab::Cache => "cache_health",
        };
        self.diag_lines = split_lines(&str_of(&self.diagnostics, key));
    }

    pub fn tick(&mut self, shell: &mut Shell) {
        if shell.bridge.is_ready() && self.seen_rev != shell.config_rev && !self.loading && !self.dirty() {
            self.seen_rev = shell.config_rev;
            self.loading = true;
            shell.call(PAGE, "load", "intelligence.load", json!({}));
        }
    }

    fn loading_placeholder(&self, ui: &mut Ui) -> bool {
        if self.loaded {
            return false;
        }
        widgets::card(ui, |ui| widgets::skeleton_block(ui, 6));
        true
    }

    // ------------------------------------------------------------ sections

    pub fn ai_ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        if self.loading_placeholder(ui) {
            return;
        }
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Chip,
                "IA probabilística",
                "Modelo global que filtra e direciona sinais",
                |ui| {
                    widgets::toggle(ui, self.form.flag("ai_probabilistic_enabled"), "Ativada");
                },
            );
            let columns = widgets::columns_for(ui, 300.0, 2);
            let mut timeframe_changed = false;
            let mut chosen: Option<ModelProfile> = None;
            widgets::grid(ui, columns, 2, |ui, index| {
                if index == 0 {
                    widgets::field(ui, "Timeframe operacional", |ui| {
                        let options: Vec<(String, String)> = shell
                            .options
                            .timeframes
                            .iter()
                            .map(|t| (t.value.clone(), format!("{} · {}", t.value, t.label)))
                            .collect();
                        timeframe_changed = widgets::combo_labeled(
                            ui,
                            "ai-timeframe",
                            self.form.text("ai_operational_timeframe"),
                            &options,
                        );
                    });
                } else {
                    widgets::field(ui, "Perfil de modelo compatível", |ui| {
                        let mut selected = self.form.get("ai_operational_model_id").to_string();
                        let mut options = vec![(String::new(), "Nenhum (caminho manual)".to_string())];
                        options.extend(self.models.iter().map(|m| (m.model_id.clone(), m.label.clone())));
                        if widgets::combo_labeled(ui, "ai-model", &mut selected, &options) {
                            self.form.set("ai_operational_model_id", selected.clone());
                            chosen = self.models.iter().find(|m| m.model_id == selected).cloned();
                        }
                    });
                }
            });
            if timeframe_changed {
                shell.call(
                    PAGE,
                    "models",
                    "intelligence.models",
                    json!({"timeframe": self.form.get("ai_operational_timeframe"), "execution_mode": self.execution_mode}),
                );
            }
            if let Some(model) = chosen {
                self.form.set("ai_operational_timeframe", model.timeframe.clone());
                self.form.set("ai_global_model_path", model.path.clone());
                self.model_status = if model.input_contract_valid {
                    (
                        format!("Perfil compatível · histórico {}", model.required_history),
                        Tone::Success,
                    )
                } else {
                    (model.input_contract_status.clone(), Tone::Danger)
                };
            }
            ui.add_space(6.0);
            widgets::field(ui, "Modelo global", |ui| {
                if widgets::path_input(
                    ui,
                    self.form.text("ai_global_model_path"),
                    "models/ai/global/...",
                    PathDialog::Model,
                ) {
                    self.model_status = ("Não validado".into(), Tone::Neutral);
                }
            });
            ui.add_space(8.0);
            ui.horizontal(|ui| {
                ui.add_enabled_ui(!self.validating, |ui| {
                    if widgets::button(ui, Kind::Secondary, Some(Icon::Check), "Validar modelo").clicked() {
                        self.validate_model(shell);
                    }
                });
                if self.validating {
                    widgets::spinner(ui, 18.0);
                }
                widgets::badge(ui, &self.model_status.0, self.model_status.1);
            });
        });
        ui.add_space(14.0);
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Network,
                "Uso dos modelos",
                "Como o Governor escolhe o modelo de cada símbolo",
                |_| {},
            );
            widgets::toggle(
                ui,
                self.form.flag("ai_require_model_approved"),
                "Exigir modelo aprovado para operar",
            );
            ui.add_space(4.0);
            widgets::toggle(
                ui,
                self.form.flag("ai_use_symbol_refined_models"),
                "Usar modelos refinados por símbolo",
            );
            ui.add_space(4.0);
            widgets::toggle(
                ui,
                self.form.flag("ai_fallback_to_global_model"),
                "Usar o modelo global quando não houver refinado",
            );
        });
    }

    pub fn thresholds_ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        if self.loading_placeholder(ui) {
            return;
        }
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Bolt,
                "Nível de ativação",
                "Permitido conforme o modo de execução atual",
                |ui| {
                    widgets::badge(
                        ui,
                        &self.execution_mode,
                        super::operation::mode_tone(&self.execution_mode),
                    );
                },
            );
            let levels = shell.options.activation_levels(&self.execution_mode).to_vec();
            let mut level = self.form.get("intelligent_activation_level").to_string();
            let options: Vec<(String, String, Tone)> = levels
                .iter()
                .map(|l| (l.clone(), level_label(l), Tone::Accent))
                .collect();
            if widgets::segmented(ui, &mut level, &options) {
                self.form.set("intelligent_activation_level", level);
            }
        });
        ui.add_space(14.0);
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Filter,
                "Limiares de decisão",
                "Probabilidades entre 0 e 1 · expected value em bps",
                |_| {},
            );
            let fields = &shell.options.probability_fields;
            let columns = widgets::columns_for(ui, 260.0, 3);
            widgets::grid(ui, columns, fields.len(), |ui, index| {
                let field = &fields[index];
                widgets::field(ui, &field.label, |ui| {
                    let response = widgets::text_input(ui, self.form.text(&field.key), "0.0");
                    let value = super::parse_f64(self.form.get(&field.key));
                    let invalid = match value {
                        None => true,
                        Some(v) if field.key.ends_with("_bps") => v < 0.0,
                        Some(v) => !(0.0..=1.0).contains(&v),
                    };
                    if invalid && !self.form.get(&field.key).is_empty() {
                        ui.painter().rect_stroke(
                            response.rect,
                            theme::RADIUS_SMALL,
                            egui::Stroke::new(1.5, theme::DANGER),
                            egui::StrokeKind::Inside,
                        );
                    }
                });
            });
        });
    }

    pub fn modules_ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        if self.loading_placeholder(ui) {
            return;
        }
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Network,
                "Módulos operacionais",
                "Sem personalização, os modos seguem o nível de ativação",
                |ui| {
                    widgets::toggle(
                        ui,
                        self.form.flag("operational_module_modes_customized"),
                        "Personalizar",
                    );
                },
            );
            let customized = self.form.is_on("operational_module_modes_customized");
            let modules = &shell.options.modules;
            ui.add_enabled_ui(customized, |ui| {
                let columns = widgets::columns_for(ui, 300.0, 2);
                widgets::grid(ui, columns, modules.len(), |ui, index| {
                    let module = &modules[index];
                    widgets::field(ui, &module.label, |ui| {
                        let options: Vec<(String, String)> = module
                            .options
                            .iter()
                            .map(|o| (o.value.clone(), o.label.clone()))
                            .collect();
                        let value = self.form.text(&module.key);
                        widgets::combo_labeled(ui, ("module", &module.key), value, &options);
                        let description = module
                            .options
                            .iter()
                            .find(|o| &o.value == value)
                            .map(|o| o.description.as_str())
                            .unwrap_or_default();
                        ui.label(RichText::new(description).size(11.5).color(theme::TEXT_FAINT));
                    });
                    ui.add_space(6.0);
                });
            });
        });
    }

    pub fn diagnostics_ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Eye,
                "Diagnóstico",
                "Usa a configuração atual, inclusive alterações ainda não salvas",
                |ui| {
                    ui.add_enabled_ui(!self.diag_loading && shell.bridge.is_ready(), |ui| {
                        if widgets::icon_button(ui, Icon::Refresh, "Atualizar diagnóstico").clicked() {
                            self.refresh_diagnostics(shell);
                        }
                    });
                    if self.diag_loading {
                        widgets::spinner(ui, 16.0);
                    }
                },
            );
            let mut tab = match self.diag_tab {
                DiagTab::Layers => "layers",
                DiagTab::Review => "review",
                DiagTab::Processes => "processes",
                DiagTab::Cache => "cache",
            }
            .to_string();
            let tabs = [
                ("layers".to_string(), "Camadas".to_string(), Tone::Accent),
                (
                    "review".to_string(),
                    "Revisão operacional".to_string(),
                    Tone::Accent,
                ),
                ("processes".to_string(), "Processos".to_string(), Tone::Accent),
                ("cache".to_string(), "Stream e cache".to_string(), Tone::Accent),
            ];
            if widgets::segmented(ui, &mut tab, &tabs) {
                self.diag_tab = match tab.as_str() {
                    "review" => DiagTab::Review,
                    "processes" => DiagTab::Processes,
                    "cache" => DiagTab::Cache,
                    _ => DiagTab::Layers,
                };
                self.refresh_diag_lines();
            }
            ui.add_space(8.0);
            if self.diag_lines.is_empty() && self.diag_loading {
                widgets::skeleton_block(ui, 8);
            } else {
                widgets::text_lines(ui, "intelligence-diagnostics", &self.diag_lines, 420.0);
            }
        });
    }
}

fn level_label(level: &str) -> String {
    match level {
        "SHADOW_ONLY" => "Somente sombra".into(),
        "PAPER_ACTIVE" => "Ativo em paper".into(),
        "TESTNET_OBSERVE" | "REAL_OBSERVE" => "Observar".into(),
        "TESTNET_REDUCE" | "REAL_REDUCE" => "Reduzir risco".into(),
        "TESTNET_BLOCK" | "REAL_BLOCK" => "Bloquear".into(),
        other => other.into(),
    }
}
