//! Monitor: intelligent events with an inspector, journals, scanner, state and sources.

use std::time::{Duration, Instant};

use egui::{RichText, Sense, Ui, vec2};
use serde::Deserialize;
use serde_json::{Value, json};

use super::{Shell, list_of, split_lines, str_of, strings_of};
use crate::bridge::{Failure, PageId};
use crate::dialogs::Confirm;
use crate::icons::Icon;
use crate::theme::{self, Tone};
use crate::widgets::{self, Cell, Column};

const PAGE: PageId = PageId::Observability;
const REFRESH: Duration = Duration::from_secs(5);
const RECENT: usize = 8;

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Event {
    pub timestamp: String,
    pub severity: String,
    pub title: String,
    pub symbol: String,
    pub event_type: String,
    pub status: String,
    pub action: String,
    pub reason: String,
    pub details: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct Source {
    kind: String,
    display_name: String,
    path: String,
    exists: bool,
    size_label: String,
    modified_at: String,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Tab {
    Events,
    Journals,
    Scanner,
    State,
    Sources,
}

impl Tab {
    const ALL: [(Self, &'static str); 5] = [
        (Self::Events, "Eventos"),
        (Self::Journals, "Journals"),
        (Self::Scanner, "Scanner"),
        (Self::State, "Estado"),
        (Self::Sources, "Fontes"),
    ];

    fn key(self) -> &'static str {
        match self {
            Self::Events => "events",
            Self::Journals => "journals",
            Self::Scanner => "scanner",
            Self::State => "state",
            Self::Sources => "sources",
        }
    }
}

pub struct ObservabilityPage {
    events: Vec<Event>,
    recent: Vec<Event>,
    journal: Vec<String>,
    scanner: Vec<String>,
    state: Vec<String>,
    sources: Vec<Source>,
    journal_paths: Vec<String>,
    state_paths: Vec<String>,
    updated_at: String,
    existing: u64,
    tab: Tab,
    severity: String,
    search: String,
    filter_key: (u64, String, String),
    visible: Vec<usize>,
    selected: Option<usize>,
    detail_lines: Vec<String>,
    auto_refresh: bool,
    loading: bool,
    rotating: bool,
    loaded: bool,
    /// The last refresh skipped the journal, scanner and state tails.
    stale_tails: bool,
    revision: u64,
    next_refresh: Instant,
    confirm_rotation: Option<Confirm<&'static str>>,
    seen_rev: u64,
}

impl ObservabilityPage {
    pub fn new() -> Self {
        Self {
            events: Vec::new(),
            recent: Vec::new(),
            journal: Vec::new(),
            scanner: Vec::new(),
            state: Vec::new(),
            sources: Vec::new(),
            journal_paths: Vec::new(),
            state_paths: Vec::new(),
            updated_at: "—".into(),
            existing: 0,
            tab: Tab::Events,
            severity: "Todos".into(),
            search: String::new(),
            filter_key: (u64::MAX, String::new(), String::new()),
            visible: Vec::new(),
            selected: None,
            detail_lines: Vec::new(),
            auto_refresh: true,
            loading: false,
            rotating: false,
            loaded: false,
            stale_tails: false,
            revision: 0,
            next_refresh: Instant::now(),
            confirm_rotation: None,
            seen_rev: u64::MAX,
        }
    }

    /// Newest events first, for the dashboard.
    pub fn recent(&self) -> &[Event] {
        &self.recent
    }

    pub fn on_reply(&mut self, kind: &str, result: Result<Value, Failure>, shell: &mut Shell) {
        match (kind, result) {
            ("snapshot", Ok(value)) => {
                self.loading = false;
                // An events-only reply (dashboard) leaves the Monitor tabs as they were.
                if value.get("journal_text").is_some() {
                    self.loaded = true;
                }
                self.apply(&value);
            }
            ("rotate", Ok(value)) => {
                self.rotating = false;
                let moved = value.get("moved_count").and_then(Value::as_u64).unwrap_or(0);
                let message = str_of(&value, "message");
                if moved > 0 {
                    shell.toasts.success("Rotação concluída", message);
                } else {
                    shell.toasts.warn("Nenhum journal arquivado", message);
                }
                self.next_refresh = Instant::now();
            }
            ("locate", Ok(value)) => match value.get("path").and_then(Value::as_str) {
                Some(path) => shell.open_path(std::path::Path::new(path)),
                None => shell.toasts.warn(
                    "Caminho indisponível",
                    "Nenhum arquivo correspondente foi encontrado.",
                ),
            },
            (kind, Err(failure)) => {
                match kind {
                    "snapshot" => self.loading = false,
                    "rotate" => self.rotating = false,
                    _ => {}
                }
                shell.toasts.failure(&failure);
            }
            _ => {}
        }
    }

    fn apply(&mut self, value: &Value) {
        let previous = self
            .selected
            .and_then(|i| self.events.get(i))
            .map(|e| (e.timestamp.clone(), e.title.clone()));
        self.events = list_of(value, "events")
            .into_iter()
            .filter_map(|e| serde_json::from_value(e).ok())
            .collect();
        let mut recent = self.events.clone();
        recent.sort_by(|a, b| b.timestamp.cmp(&a.timestamp));
        recent.truncate(RECENT);
        self.recent = recent;
        // Sections missing from the reply were not requested: keep the previous data.
        if value.get("sources").is_some() {
            self.sources = list_of(value, "sources")
                .into_iter()
                .filter_map(|e| serde_json::from_value(e).ok())
                .collect();
            self.existing = value.get("existing_sources").and_then(Value::as_u64).unwrap_or(0);
        }
        if value.get("journal_text").is_some() {
            self.journal = split_lines(&str_of(value, "journal_text"));
            self.journal_paths = strings_of(value, "journal_paths");
        }
        if value.get("scanner_text").is_some() {
            self.scanner = split_lines(&str_of(value, "scanner_text"));
        }
        if value.get("state_text").is_some() {
            self.state = split_lines(&str_of(value, "state_text"));
            self.state_paths = strings_of(value, "state_paths");
        }
        self.updated_at = str_of(value, "updated_at");
        self.revision += 1;
        self.selected = previous.and_then(|(ts, title)| {
            self.events
                .iter()
                .position(|e| e.timestamp == ts && e.title == title)
        });
        self.refresh_detail();
    }

    fn refresh_filter(&mut self) {
        let key = (self.revision, self.severity.clone(), self.search.to_lowercase());
        if key == self.filter_key {
            return;
        }
        let needle = key.2.clone();
        self.visible = self
            .events
            .iter()
            .enumerate()
            .filter(|(_, e)| self.severity == "Todos" || e.severity.eq_ignore_ascii_case(&self.severity))
            .filter(|(_, e)| {
                needle.is_empty()
                    || [
                        &e.title,
                        &e.symbol,
                        &e.event_type,
                        &e.status,
                        &e.action,
                        &e.reason,
                        &e.details,
                        &e.timestamp,
                    ]
                    .iter()
                    .any(|field| field.to_lowercase().contains(&needle))
            })
            .map(|(index, _)| index)
            .collect();
        self.filter_key = key;
    }

    fn refresh_detail(&mut self) {
        self.detail_lines = match self.selected.and_then(|i| self.events.get(i)) {
            Some(event) => {
                let pretty = serde_json::from_str::<Value>(&event.details)
                    .ok()
                    .and_then(|v| serde_json::to_string_pretty(&v).ok())
                    .unwrap_or_else(|| event.details.clone());
                split_lines(&pretty)
            }
            None => Vec::new(),
        };
    }

    /// `visible` is true on the Monitor and on the dashboard (recent events); `full` is
    /// true only on the Monitor, which needs the journal, scanner and state tails too.
    pub fn tick(&mut self, shell: &mut Shell, visible: bool, full: bool) {
        if let Some(confirm) = &self.confirm_rotation {
            match confirm.show(&shell.bridge_ctx()) {
                Some(true) => {
                    let kind = confirm.payload;
                    self.confirm_rotation = None;
                    if shell.locked {
                        shell.toasts.warn(
                            "Rotação indisponível",
                            "A rotação não pode ser executada enquanto o Alphora estiver operando.",
                        );
                    } else {
                        self.rotating = true;
                        shell.call(PAGE, "rotate", "observability.rotate", json!({"kind": kind}));
                    }
                }
                Some(false) => self.confirm_rotation = None,
                None => {}
            }
        }
        if !shell.bridge.is_ready() || self.loading || self.rotating {
            return;
        }
        let config_changed = self.seen_rev != shell.config_rev;
        let due = visible && self.auto_refresh && Instant::now() >= self.next_refresh;
        // The first load is full so the Monitor opens instantly; afterwards the dashboard
        // only polls the events.
        let full = full || !self.loaded;
        if config_changed || due || !self.loaded || (full && self.stale_tails) {
            self.seen_rev = shell.config_rev;
            self.request(shell, full);
        }
        if visible && self.auto_refresh {
            shell
                .bridge_ctx()
                .request_repaint_after(self.next_refresh.saturating_duration_since(Instant::now()));
        }
    }

    /// Full refresh (every section), as the Monitor's refresh button does.
    pub fn refresh(&mut self, shell: &mut Shell) {
        self.request(shell, true);
    }

    fn request(&mut self, shell: &mut Shell, full: bool) {
        if self.loading {
            return;
        }
        self.loading = true;
        self.next_refresh = Instant::now() + REFRESH;
        // Events-only refreshes leave the tails behind; the Monitor refreshes them on open.
        self.stale_tails = !full;
        let params = if full {
            json!({})
        } else {
            json!({"sections": ["events"]})
        };
        shell.call(PAGE, "snapshot", "observability.snapshot", params);
    }

    pub fn request_rotation(&mut self, intelligent: bool, shell: &mut Shell) {
        if shell.locked {
            shell
                .toasts
                .warn("Rotação bloqueada", "Pare o Alphora antes de arquivar journals.");
            return;
        }
        self.confirm_rotation = Some(if intelligent {
            rotation_confirm("intelligent", "Arquivar decisões inteligentes")
        } else {
            rotation_confirm("journals", "Arquivar journals")
        });
    }

    pub fn ui(&mut self, ui: &mut Ui, shell: &mut Shell) {
        ui.horizontal(|ui| {
            let mut tab = self.tab.key().to_string();
            let tabs: Vec<(String, String, Tone)> = Tab::ALL
                .iter()
                .map(|(tab, label)| {
                    let label = if *tab == Tab::Events && self.loaded {
                        format!("{label} · {}", self.events.len())
                    } else {
                        label.to_string()
                    };
                    (tab.key().to_string(), label, Tone::Accent)
                })
                .collect();
            if widgets::segmented(ui, &mut tab, &tabs) {
                self.tab = Tab::ALL
                    .iter()
                    .find(|(t, _)| t.key() == tab)
                    .map(|(t, _)| *t)
                    .unwrap_or(Tab::Events);
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                ui.add_enabled_ui(!self.loading && shell.bridge.is_ready(), |ui| {
                    if widgets::icon_button(ui, Icon::Refresh, "Atualizar agora").clicked() {
                        self.refresh(shell);
                    }
                });
                widgets::toggle(ui, &mut self.auto_refresh, "Ao vivo");
                if self.loading {
                    widgets::spinner(ui, 16.0);
                }
                let tone = if self.existing > 0 {
                    Tone::Success
                } else {
                    Tone::Warning
                };
                widgets::badge(
                    ui,
                    &format!("{} fontes · {}", self.existing, self.updated_at),
                    tone,
                );
            });
        });
        ui.add_space(14.0);

        if !self.loaded {
            widgets::card(ui, |ui| widgets::skeleton_block(ui, 8));
            return;
        }
        match self.tab {
            Tab::Events => self.events_tab(ui, shell),
            Tab::Journals => self.text_tab(ui, shell, "Journals operacionais", "obs-journals", true),
            Tab::Scanner => self.text_tab(ui, shell, "Scanner scores", "obs-scanner", false),
            Tab::State => self.text_tab(ui, shell, "Estado do runtime", "obs-state", false),
            Tab::Sources => self.sources_tab(ui, shell),
        }
    }

    fn text_tab(&mut self, ui: &mut Ui, shell: &mut Shell, title: &str, id: &str, journals: bool) {
        let lines = match self.tab {
            Tab::Journals => &self.journal,
            Tab::Scanner => &self.scanner,
            _ => &self.state,
        };
        let mut locate = None;
        widgets::card(ui, |ui| {
            widgets::card_header(ui, Icon::File, title, &format!("{} linhas", lines.len()), |ui| {
                if widgets::icon_button(ui, Icon::Open, "Abrir o arquivo efetivo").clicked() {
                    locate = Some(if journals {
                        self.journal_paths.clone()
                    } else {
                        self.state_paths.clone()
                    });
                }
            });
            let height = (ui.available_height() - 20.0).max(360.0);
            widgets::text_lines(ui, id, lines, height);
        });
        if let Some(candidates) = locate {
            shell.call(
                PAGE,
                "locate",
                "observability.locate",
                json!({"candidates": candidates}),
            );
        }
    }

    fn events_tab(&mut self, ui: &mut Ui, shell: &mut Shell) {
        self.refresh_filter();
        // Severity chips with counts: one click filters, no dropdown digging.
        ui.horizontal_wrapped(|ui| {
            let mut chips: Vec<(String, usize)> = vec![("Todos".into(), self.events.len())];
            for severity in &shell.options.event_severities {
                let count = self
                    .events
                    .iter()
                    .filter(|e| e.severity.eq_ignore_ascii_case(severity))
                    .count();
                if count > 0 {
                    chips.push((severity.clone(), count));
                }
            }
            for (severity, count) in chips {
                let selected = self.severity == severity;
                if chip(ui, &severity, count, selected).clicked() {
                    self.severity = severity;
                }
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                ui.allocate_ui(vec2(280.0, theme::CONTROL_HEIGHT), |ui| {
                    widgets::text_input(ui, &mut self.search, "Buscar símbolo, título, motivo…");
                });
            });
        });
        ui.add_space(10.0);
        widgets::split(ui, 0.6, |ui, side| {
            if side == 0 {
                widgets::card(ui, |ui| self.events_table(ui));
            } else {
                widgets::card(ui, |ui| self.inspector(ui));
            }
        });
    }

    fn events_table(&mut self, ui: &mut Ui) {
        if self.visible.is_empty() {
            widgets::empty_state(
                ui,
                Icon::Eye,
                "Nenhum evento",
                "Os eventos do fluxo inteligente aparecerão aqui.",
            );
            return;
        }
        let columns = [
            Column {
                title: "Horário",
                width: 160.0,
            },
            Column {
                title: "Severidade",
                width: 120.0,
            },
            Column {
                title: "Título",
                width: 230.0,
            },
            Column {
                title: "Símbolo",
                width: 95.0,
            },
            Column {
                title: "Motivo",
                width: 260.0,
            },
        ];
        let columns = widgets::fit_columns(&columns, ui.available_width());
        let mut clicked = None;
        egui::ScrollArea::horizontal()
            .id_salt("obs-events-h")
            .show(ui, |ui| {
                ui.spacing_mut().item_spacing.y = 0.0;
                widgets::table_header(ui, &columns);
                egui::ScrollArea::vertical()
                    .id_salt("obs-events")
                    .max_height(520.0)
                    .auto_shrink([false, true])
                    .show_rows(ui, widgets::ROW_HEIGHT, self.visible.len(), |ui, range| {
                        for row in range {
                            let index = self.visible[row];
                            let event = &self.events[index];
                            let response = widgets::table_row(
                                ui,
                                &columns,
                                &[
                                    Cell::muted(&event.timestamp),
                                    Cell::badge(&event.severity, Tone::from_severity(&event.severity)),
                                    Cell::text(&event.title),
                                    Cell::text(&event.symbol),
                                    Cell::muted(&event.reason),
                                ],
                                self.selected == Some(index),
                                row % 2 == 1,
                            );
                            if response.clicked() {
                                clicked = Some(index);
                            }
                        }
                    });
            });
        if let Some(index) = clicked {
            self.selected = Some(index);
            self.refresh_detail();
        }
    }

    fn inspector(&mut self, ui: &mut Ui) {
        let Some(event) = self.selected.and_then(|i| self.events.get(i)) else {
            widgets::empty_state(
                ui,
                Icon::Search,
                "Selecione um evento",
                "Os detalhes completos aparecem aqui.",
            );
            return;
        };
        ui.horizontal(|ui| {
            widgets::badge(ui, &event.severity, Tone::from_severity(&event.severity));
            ui.label(
                RichText::new(&event.timestamp)
                    .size(12.0)
                    .color(theme::TEXT_MUTED),
            );
        });
        ui.add_space(6.0);
        ui.add(
            egui::Label::new(
                RichText::new(&event.title)
                    .font(theme::semibold(17.0))
                    .color(theme::TEXT),
            )
            .wrap(),
        );
        ui.add_space(8.0);
        widgets::grid(ui, 2, 4, |ui, index| {
            let (label, value) = match index {
                0 => ("Símbolo", &event.symbol),
                1 => ("Tipo", &event.event_type),
                2 => ("Status", &event.status),
                _ => ("Ação", &event.action),
            };
            widgets::mini_stat(ui, label, if value.is_empty() { "—" } else { value }, theme::TEXT);
        });
        if !event.reason.is_empty() {
            ui.add_space(8.0);
            widgets::field_label(ui, "Motivo");
            ui.add(egui::Label::new(RichText::new(&event.reason).size(13.0).color(theme::TEXT)).wrap());
        }
        ui.add_space(10.0);
        widgets::field_label(ui, "Detalhes");
        widgets::text_lines(ui, "obs-event-details", &self.detail_lines, 300.0);
    }

    fn sources_tab(&mut self, ui: &mut Ui, shell: &mut Shell) {
        let mut open = None;
        widgets::card(ui, |ui| {
            widgets::card_header(
                ui,
                Icon::Server,
                "Fontes do runtime",
                "Clique em uma linha para abrir o arquivo",
                |_| {},
            );
            if self.sources.is_empty() {
                widgets::empty_state(
                    ui,
                    Icon::Server,
                    "Nenhuma fonte",
                    "Journals, estados e locks aparecerão aqui.",
                );
                return;
            }
            let columns = [
                Column {
                    title: "Tipo",
                    width: 100.0,
                },
                Column {
                    title: "Arquivo",
                    width: 240.0,
                },
                Column {
                    title: "Caminho",
                    width: 380.0,
                },
                Column {
                    title: "Tamanho",
                    width: 100.0,
                },
                Column {
                    title: "Modificado",
                    width: 170.0,
                },
            ];
            let columns = widgets::fit_columns(&columns, ui.available_width());
            egui::ScrollArea::horizontal()
                .id_salt("obs-sources")
                .show(ui, |ui| {
                    ui.spacing_mut().item_spacing.y = 0.0;
                    widgets::table_header(ui, &columns);
                    for (index, source) in self.sources.iter().enumerate() {
                        let response = widgets::table_row(
                            ui,
                            &columns,
                            &[
                                Cell::badge(
                                    &source.kind,
                                    if source.exists { Tone::Accent } else { Tone::Muted },
                                ),
                                Cell::text(&source.display_name),
                                Cell::muted(&source.path),
                                Cell::muted(&source.size_label),
                                Cell::muted(&source.modified_at),
                            ],
                            false,
                            index % 2 == 1,
                        );
                        if response.clicked() {
                            open = Some(source.path.clone());
                        }
                    }
                });
        });
        if let Some(path) = open {
            shell.call(PAGE, "locate", "observability.locate", json!({"path": path}));
        }
    }
}

/// Filter pill with a counter.
fn chip(ui: &mut Ui, label: &str, count: usize, selected: bool) -> egui::Response {
    let tone = if label == "Todos" {
        Tone::Accent
    } else {
        Tone::from_severity(label)
    };
    let text = format!("{label}  {count}");
    let galley = ui
        .painter()
        .layout_no_wrap(text, theme::semibold(12.0), theme::TEXT);
    let (rect, response) = ui.allocate_exact_size(vec2(galley.size().x + 24.0, 28.0), Sense::click());
    let hover = widgets::hover_amount(ui, &response, true);
    let (fill, stroke, color) = if selected {
        (
            theme::alpha(tone.color(), 36),
            theme::alpha(tone.color(), 160),
            tone.color(),
        )
    } else {
        (
            widgets::lerp_color(theme::CARD, theme::HOVER, hover),
            theme::BORDER_STRONG,
            theme::TEXT_MUTED,
        )
    };
    ui.painter().rect_filled(rect, 14, fill);
    ui.painter()
        .rect_stroke(rect, 14, egui::Stroke::new(1.0, stroke), egui::StrokeKind::Inside);
    ui.painter()
        .galley(rect.center() - galley.size() / 2.0, galley, color);
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

fn rotation_confirm(kind: &'static str, title: &str) -> Confirm<&'static str> {
    Confirm::new(
        title,
        "Os arquivos atuais serão movidos para data/audit e novos arquivos vazios serão criados nos caminhos originais. Os dados anteriores serão preservados para auditoria. Deseja continuar?",
        "Arquivar",
        kind,
    )
    .safe()
}
