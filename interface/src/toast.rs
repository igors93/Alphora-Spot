//! Non-blocking notifications stacked in the top-right corner.

use std::time::{Duration, Instant};

use egui::{Align2, CornerRadius, Frame, Margin, RichText, Sense, Stroke, Vec2, vec2};

use crate::icons::{self, Icon};
use crate::theme::{self, Tone};

const MAX_VISIBLE: usize = 5;

struct Toast {
    id: u64,
    title: String,
    message: String,
    tone: Tone,
    expires: Option<Instant>,
}

#[derive(Default)]
pub struct Toasts {
    items: Vec<Toast>,
    next_id: u64,
}

impl Toasts {
    pub fn info(&mut self, title: impl Into<String>, message: impl Into<String>) {
        self.push(
            title.into(),
            message.into(),
            Tone::Accent,
            Some(Duration::from_secs(5)),
        );
    }

    pub fn success(&mut self, title: impl Into<String>, message: impl Into<String>) {
        self.push(
            title.into(),
            message.into(),
            Tone::Success,
            Some(Duration::from_secs(5)),
        );
    }

    pub fn warn(&mut self, title: impl Into<String>, message: impl Into<String>) {
        self.push(
            title.into(),
            message.into(),
            Tone::Warning,
            Some(Duration::from_secs(9)),
        );
    }

    /// Errors stay until dismissed: an operator must not miss them.
    pub fn error(&mut self, title: impl Into<String>, message: impl Into<String>) {
        self.push(title.into(), message.into(), Tone::Danger, None);
    }

    pub fn failure(&mut self, failure: &crate::bridge::Failure) {
        let title = if failure.title.is_empty() {
            "Erro"
        } else {
            failure.title.as_str()
        };
        self.error(title, failure.message.clone());
    }

    fn push(&mut self, title: String, message: String, tone: Tone, ttl: Option<Duration>) {
        // Collapse repeats so a polling failure does not flood the screen.
        if let Some(existing) = self
            .items
            .iter_mut()
            .find(|t| t.title == title && t.message == message)
        {
            existing.expires = ttl.map(|ttl| Instant::now() + ttl);
            return;
        }
        self.next_id += 1;
        self.items.push(Toast {
            id: self.next_id,
            title,
            message,
            tone,
            expires: ttl.map(|ttl| Instant::now() + ttl),
        });
        if self.items.len() > MAX_VISIBLE {
            let removable = self.items.iter().position(|t| t.expires.is_some()).unwrap_or(0);
            self.items.remove(removable);
        }
    }

    pub fn show(&mut self, ctx: &egui::Context) {
        let now = Instant::now();
        self.items.retain(|t| t.expires.is_none_or(|at| at > now));
        if let Some(next) = self.items.iter().filter_map(|t| t.expires).min() {
            ctx.request_repaint_after(next.saturating_duration_since(now));
        }
        if self.items.is_empty() {
            return;
        }
        let mut dismissed = Vec::new();
        egui::Area::new(egui::Id::unique("alphora-toasts"))
            .anchor(Align2::RIGHT_BOTTOM, vec2(-18.0, -42.0))
            .order(egui::Order::Foreground)
            .interactable(true)
            .show(ctx, |ui| {
                ui.set_width(380.0);
                for toast in &self.items {
                    let response = Frame::new()
                        .fill(theme::CARD)
                        .stroke(Stroke::new(1.0, theme::alpha(toast.tone.color(), 90)))
                        .corner_radius(CornerRadius::same(12))
                        .inner_margin(Margin::symmetric(14, 12))
                        .shadow(theme::shadow(24, 8, 40))
                        .show(ui, |ui| {
                            ui.set_width(352.0);
                            ui.horizontal_top(|ui| {
                                let (rect, _) = ui.allocate_exact_size(Vec2::splat(20.0), Sense::hover());
                                let icon = match toast.tone {
                                    Tone::Danger => Icon::Warning,
                                    Tone::Warning => Icon::Warning,
                                    Tone::Success => Icon::Check,
                                    _ => Icon::Info,
                                };
                                icons::paint(ui.painter(), rect, icon, toast.tone.color());
                                ui.vertical(|ui| {
                                    ui.set_width(300.0);
                                    ui.spacing_mut().item_spacing.y = 3.0;
                                    ui.label(
                                        RichText::new(&toast.title)
                                            .font(theme::semibold(13.5))
                                            .color(theme::TEXT),
                                    );
                                    if !toast.message.is_empty() {
                                        ui.label(
                                            RichText::new(&toast.message).size(12.5).color(theme::TEXT_MUTED),
                                        );
                                    }
                                });
                                let (close, response) =
                                    ui.allocate_exact_size(Vec2::splat(18.0), Sense::click());
                                let color = if response.hovered() {
                                    theme::TEXT
                                } else {
                                    theme::TEXT_FAINT
                                };
                                icons::paint(ui.painter(), close.shrink(3.0), Icon::Close, color);
                                response.clicked()
                            })
                            .inner
                        });
                    if response.inner {
                        dismissed.push(toast.id);
                    }
                    ui.add_space(8.0);
                }
            });
        self.items.retain(|t| !dismissed.contains(&t.id));
    }
}
