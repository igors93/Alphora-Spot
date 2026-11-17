//! Modal dialogs: the start-up confirmation chain and generic confirmations.

use std::collections::BTreeSet;

use egui::{Align, CornerRadius, Frame, Layout, Margin, RichText, ScrollArea, Sense, Stroke, Vec2};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::icons::{self, Icon};
use crate::theme::{self, Tone};
use crate::widgets::{self, Kind};

/// A confirmation requested by the core before the engine may start.
#[derive(Clone, Debug, Deserialize)]
pub struct ConfirmationRequest {
    pub key: String,
    pub kind: String,
    pub title: String,
    pub message: String,
    #[serde(default)]
    pub severity: String,
    #[serde(default)]
    pub required_tokens: Vec<String>,
    #[serde(default)]
    pub checklist_items: Vec<String>,
    #[serde(default = "default_accept")]
    pub accept_label: String,
    #[serde(default = "default_reject")]
    pub reject_label: String,
}

fn default_accept() -> String {
    "Continuar".into()
}

fn default_reject() -> String {
    "Cancelar".into()
}

impl ConfirmationRequest {
    /// Same acceptance rules as the core; the core re-validates before acting.
    fn is_satisfied(&self, typed: &str, checked: &BTreeSet<usize>) -> bool {
        match self.kind.as_str() {
            "TYPED" => self.required_tokens.iter().any(|token| token == typed.trim()),
            "CHECKLIST" => checked.len() == self.checklist_items.len(),
            _ => true,
        }
    }
}

pub enum Outcome {
    Accepted(Value),
    Rejected,
}

pub struct ConfirmationDialog {
    pub request: ConfirmationRequest,
    typed: String,
    checked: BTreeSet<usize>,
}

impl ConfirmationDialog {
    pub fn new(request: ConfirmationRequest) -> Self {
        Self {
            request,
            typed: String::new(),
            checked: BTreeSet::new(),
        }
    }

    pub fn show(&mut self, ctx: &egui::Context) -> Option<Outcome> {
        let tone = match self.request.severity.as_str() {
            "ERROR" => Tone::Danger,
            "INFO" => Tone::Accent,
            _ => Tone::Warning,
        };
        let mut outcome = None;
        egui::Modal::new(egui::Id::unique(("confirmation", &self.request.key)))
            .backdrop_color(theme::BACKDROP)
            .frame(modal_frame())
            .show(ctx, |ui| {
                ui.set_width(560.0);
                dialog_title(ui, Icon::Warning, tone, &self.request.title);
                ScrollArea::vertical().max_height(300.0).show(ui, |ui| {
                    ui.label(RichText::new(&self.request.message).size(13.0).color(theme::TEXT));
                });
                ui.add_space(10.0);
                match self.request.kind.as_str() {
                    "CHECKLIST" => {
                        for (index, item) in self.request.checklist_items.iter().enumerate() {
                            let mut on = self.checked.contains(&index);
                            if widgets::toggle(ui, &mut on, item).changed() {
                                if on {
                                    self.checked.insert(index);
                                } else {
                                    self.checked.remove(&index);
                                }
                            }
                        }
                    }
                    "TYPED" => {
                        let hint = self.request.required_tokens.first().cloned().unwrap_or_default();
                        let response = widgets::text_input(ui, &mut self.typed, &hint);
                        response.request_focus();
                    }
                    _ => {}
                }
                ui.add_space(14.0);
                let satisfied = self.request.is_satisfied(&self.typed, &self.checked);
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    let accept_kind = if tone == Tone::Danger {
                        Kind::Danger
                    } else {
                        Kind::Primary
                    };
                    let accept = ui
                        .add_enabled_ui(satisfied, |ui| {
                            widgets::button(ui, accept_kind, Some(Icon::Check), &self.request.accept_label)
                        })
                        .inner;
                    let enter = satisfied && ui.input(|i| i.key_pressed(egui::Key::Enter));
                    if accept.clicked() || enter {
                        let checked: Vec<&String> = self
                            .checked
                            .iter()
                            .filter_map(|i| self.request.checklist_items.get(*i))
                            .collect();
                        outcome = Some(Outcome::Accepted(json!({
                            "key": self.request.key,
                            "accepted": true,
                            "typed_value": self.typed.trim(),
                            "checked_items": checked,
                        })));
                    }
                    if widgets::button(ui, Kind::Secondary, None, &self.request.reject_label).clicked()
                        || ui.input(|i| i.key_pressed(egui::Key::Escape))
                    {
                        outcome = Some(Outcome::Rejected);
                    }
                });
            });
        outcome
    }
}

/// Yes/no question for destructive or irreversible actions.
pub struct Confirm<T> {
    pub title: String,
    pub message: String,
    pub confirm_label: String,
    pub danger: bool,
    pub payload: T,
}

impl<T> Confirm<T> {
    pub fn new(
        title: impl Into<String>,
        message: impl Into<String>,
        confirm_label: impl Into<String>,
        payload: T,
    ) -> Self {
        Self {
            title: title.into(),
            message: message.into(),
            confirm_label: confirm_label.into(),
            danger: true,
            payload,
        }
    }

    pub fn safe(mut self) -> Self {
        self.danger = false;
        self
    }

    /// Returns `Some(true)` when confirmed, `Some(false)` when dismissed.
    pub fn show(&self, ctx: &egui::Context) -> Option<bool> {
        let tone = if self.danger { Tone::Danger } else { Tone::Accent };
        let mut result = None;
        egui::Modal::new(egui::Id::unique(("confirm", &self.title)))
            .backdrop_color(theme::BACKDROP)
            .frame(modal_frame())
            .show(ctx, |ui| {
                ui.set_width(480.0);
                dialog_title(
                    ui,
                    if self.danger { Icon::Warning } else { Icon::Info },
                    tone,
                    &self.title,
                );
                ui.label(RichText::new(&self.message).size(13.0).color(theme::TEXT));
                ui.add_space(16.0);
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    let kind = if self.danger { Kind::Danger } else { Kind::Primary };
                    if widgets::button(ui, kind, None, &self.confirm_label).clicked() {
                        result = Some(true);
                    }
                    if widgets::button(ui, Kind::Secondary, None, "Cancelar").clicked()
                        || ui.input(|i| i.key_pressed(egui::Key::Escape))
                    {
                        result = Some(false);
                    }
                });
            });
        result
    }
}

pub fn modal_frame() -> Frame {
    Frame::new()
        .fill(theme::CARD)
        .stroke(Stroke::new(1.0, theme::BORDER_STRONG))
        .corner_radius(CornerRadius::same(14))
        .inner_margin(Margin::same(22))
        .shadow(theme::shadow(40, 16, 46))
}

pub fn dialog_title(ui: &mut egui::Ui, icon: Icon, tone: Tone, title: &str) {
    ui.horizontal(|ui| {
        let (rect, _) = ui.allocate_exact_size(Vec2::splat(32.0), Sense::hover());
        ui.painter().rect_filled(rect, 9, tone.soft());
        icons::paint(ui.painter(), rect.shrink(7.0), icon, tone.color());
        ui.label(
            RichText::new(title)
                .font(theme::semibold(17.0))
                .color(theme::TEXT),
        );
    });
    ui.add_space(12.0);
}
