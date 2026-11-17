//! Binance API credentials per environment. Secrets never come back from the core:
//! only a masked key preview and "configured" flags are displayed.

use egui::{RichText, Ui};
use serde::Deserialize;
use serde_json::{Value, json};

use super::{Shell, list_of, str_of};
use crate::bridge::{Failure, PageId};
use crate::dialogs::Confirm;
use crate::icons::Icon;
use crate::theme::{self, Tone};
use crate::widgets::{self, Kind};

const PAGE: PageId = PageId::Credentials;

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Summary {
    environment: String,
    configured: bool,
    api_key_configured: bool,
    api_secret_configured: bool,
    api_key_preview: String,
    status_label: String,
    tone: String,
}

pub struct CredentialsPage {
    summaries: Vec<Summary>,
    storage_path: String,
    protection: String,
    environment: String,
    api_key: String,
    api_secret: String,
    reveal: bool,
    busy: bool,
    confirm_clear: Option<Confirm<String>>,
    seen_rev: u64,
}

impl CredentialsPage {
    pub fn new() -> Self {
        Self {
            summaries: Vec::new(),
            storage_path: String::new(),
            protection: String::new(),
            environment: "TESTNET".into(),
            api_key: String::new(),
            api_secret: String::new(),
            reveal: false,
            busy: false,
            confirm_clear: None,
            seen_rev: u64::MAX,
        }
    }

    pub fn has_draft(&self) -> bool {
        !self.api_key.is_empty() || !self.api_secret.is_empty()
    }

    /// True once both environments are configured (used by the settings navigator).
    pub fn all_configured(&self) -> bool {
        !self.summaries.is_empty() && self.summaries.iter().all(|s| s.configured)
    }

    pub fn save(&mut self, shell: &mut Shell) {
        if !self.has_draft() || self.busy {
            return;
        }
        if shell.locked {
            shell
                .toasts
                .warn("Edição bloqueada", "Pare o Alphora antes de alterar credenciais.");
            return;
        }
        self.busy = true;
        shell.call(
            PAGE,
            "save",
            "credentials.save",
            json!({"environment": self.environment, "api_key": self.api_key.trim(), "api_secret": self.api_secret.trim()}),
        );
    }

    pub fn discard(&mut self) {
        self.clear_drafts();
    }

    fn clear_drafts(&mut self) {
        // Overwrite before dropping so secrets do not linger in the reused buffers.
        for buffer in [&mut self.api_key, &mut self.api_secret] {
            let len = buffer.len();
            buffer.clear();
            buffer.extend(std::iter::repeat_n('\0', len));
            buffer.clear();
        }
        self.reveal = false;
    }

    pub fn on_reply(&mut self, kind: &str, result: Result<Value, Failure>, shell: &mut Shell) {
        self.busy = false;
        match result {
            Ok(value) => {
                self.apply(&value);
                match kind {
                    "save" => {
                        self.clear_drafts();
                        shell.saved();
                        shell.toasts.success(
                            "Credenciais atualizadas",
                            format!("Credenciais de {} salvas. {}.", self.environment, self.protection),
                        );
                    }
                    "clear" => {
                        self.clear_drafts();
                        shell.saved();
                        shell.toasts.success(
                            "Credenciais removidas",
                            format!("As credenciais de {} foram removidas.", self.environment),
                        );
                    }
                    _ => {}
                }
            }
            Err(failure) => shell.toasts.failure(&failure),
        }
    }

    fn apply(&mut self, value: &Value) {
        self.summaries = list_of(value, "summaries")
            .into_iter()
            .filter_map(|item| serde_json::from_value(item).ok())
            .collect();
        self.storage_path = str_of(value, "storage_path");
        self.protection = str_of(value, "protection");
    }

    pub fn tick(&mut self, shell: &mut Shell) {
        if shell.bridge.is_ready() && self.seen_rev != shell.config_rev && !self.busy {
            self.seen_rev = shell.config_rev;
            self.busy = true;
            shell.call(PAGE, "load", "credentials.load", json!({}));
        }
    }

    pub fn ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        let summaries = self.summaries.clone();
        let columns = widgets::columns_for(ui, 360.0, 2);
        widgets::grid(ui, columns, summaries.len(), |ui, index| {
            let summary = &summaries[index];
            widgets::card(ui, |ui| {
                let title = if summary.environment == "REAL" {
                    "Conta real"
                } else {
                    "Testnet / demo"
                };
                widgets::card_header(ui, Icon::Key, title, &summary.environment, |ui| {
                    widgets::badge(ui, &summary.status_label, Tone::parse(&summary.tone));
                });
                widgets::key_value(ui, "API key", &summary.api_key_preview);
                widgets::key_value(ui, "API key configurada", yes_no(summary.api_key_configured));
                widgets::key_value(
                    ui,
                    "API secret configurado",
                    yes_no(summary.api_secret_configured),
                );
                widgets::key_value(ui, "Pronto para operar", yes_no(summary.configured));
            });
        });
        ui.add_space(14.0);

        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Edit,
                "Atualizar credenciais",
                "Campos em branco mantêm o valor atual salvo.",
                |_| {},
            );
            ui.add_enabled_ui(!shell.locked && !self.busy && shell.bridge.is_ready(), |ui| {
                widgets::field(ui, "Ambiente", |ui| {
                    let options: Vec<(String, String, Tone)> = shell
                        .options
                        .credential_environments
                        .iter()
                        .map(|e| (e.clone(), e.clone(), if e == "REAL" { Tone::Danger } else { Tone::Accent }))
                        .collect();
                    let mut selected = self.environment.clone();
                    if widgets::segmented(ui, &mut selected, &options) {
                        if self.has_draft() {
                            shell.toasts.warn("Alterações não salvas", "Salve ou descarte os campos informados antes de trocar de ambiente.");
                        } else {
                            self.environment = selected;
                        }
                    }
                });
                ui.add_space(6.0);
                let columns = widgets::columns_for(ui, 340.0, 2);
                widgets::grid(ui, columns, 2, |ui, index| {
                    if index == 0 {
                        widgets::field(ui, "API key", |ui| {
                            widgets::secret_input(ui, &mut self.api_key, "Nova API key", self.reveal);
                        });
                    } else {
                        widgets::field(ui, "API secret", |ui| {
                            widgets::secret_input(ui, &mut self.api_secret, "Novo API secret", self.reveal);
                        });
                    }
                });
                ui.add_space(6.0);
                widgets::toggle(ui, &mut self.reveal, "Mostrar valores digitados");
                ui.add_space(12.0);
                ui.horizontal(|ui| {
                    ui.add_enabled_ui(self.has_draft(), |ui| {
                        if widgets::button(ui, Kind::Primary, Some(Icon::Save), "Salvar credenciais").clicked() {
                            self.save(shell);
                        }
                        if widgets::button(ui, Kind::Ghost, None, "Descartar").clicked() {
                            self.clear_drafts();
                        }
                    });
                    if widgets::button(ui, Kind::Danger, Some(Icon::Trash), "Remover credenciais").clicked() {
                        self.confirm_clear = Some(Confirm::new(
                            "Remover credenciais",
                            format!("Remover API key e secret de {}? O Alphora não poderá operar neste ambiente até que novas chaves sejam salvas.", self.environment),
                            "Remover",
                            self.environment.clone(),
                        ));
                    }
                });
            });
        });
        ui.add_space(14.0);

        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Lock,
                "Armazenamento",
                "As chaves ficam no arquivo de configuração local, fora do controle de versão.",
                |_| {},
            );
            widgets::key_value(ui, "Arquivo", &self.storage_path);
            widgets::key_value(ui, "Proteção", &self.protection);
            ui.add_space(4.0);
            ui.label(
                RichText::new("Use chaves sem permissão de saque e restrinja o acesso por IP na Binance.")
                    .size(12.0)
                    .color(theme::TEXT_FAINT),
            );
        });

        if let Some(confirm) = &self.confirm_clear {
            match confirm.show(ui.ctx()) {
                Some(true) => {
                    let environment = confirm.payload.clone();
                    self.confirm_clear = None;
                    self.busy = true;
                    shell.call(
                        PAGE,
                        "clear",
                        "credentials.clear",
                        json!({"environment": environment}),
                    );
                }
                Some(false) => self.confirm_clear = None,
                None => {}
            }
        }
    }
}

fn yes_no(value: bool) -> &'static str {
    if value { "Sim" } else { "Não" }
}
