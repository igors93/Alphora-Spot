//! Ctrl+K command palette: fuzzy search over every screen and action.

use egui::{Align2, CornerRadius, Frame, Key, Margin, RichText, Sense, Stroke, TextEdit, vec2};

use crate::commands::{Command, fuzzy_score};
use crate::icons;
use crate::theme;
use crate::widgets;

const VISIBLE_ROWS: usize = 9;

#[derive(Default)]
pub struct Palette {
    open: bool,
    query: String,
    cursor: usize,
    focus_pending: bool,
}

impl Palette {
    pub fn is_open(&self) -> bool {
        self.open
    }

    pub fn toggle(&mut self) {
        if self.open {
            self.open = false;
        } else {
            self.open = true;
            self.query.clear();
            self.cursor = 0;
            self.focus_pending = true;
        }
    }

    /// Shows the palette; returns the command the user picked, if any.
    pub fn show(&mut self, ctx: &egui::Context, enabled: impl Fn(Command) -> bool) -> Option<Command> {
        if !self.open {
            return None;
        }
        let mut matches: Vec<(i32, usize, Command)> = Command::PALETTE
            .iter()
            .enumerate()
            .filter_map(|(order, command)| {
                let haystack = format!("{} {}", command.label(), command.group());
                fuzzy_score(&self.query, &haystack).map(|score| (score, order, *command))
            })
            .collect();
        matches.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        self.cursor = self.cursor.min(matches.len().saturating_sub(1));

        let mut picked = None;
        let (up, down, enter, escape) = ctx.input_mut(|input| {
            (
                input.consume_key(egui::Modifiers::NONE, Key::ArrowUp),
                input.consume_key(egui::Modifiers::NONE, Key::ArrowDown),
                input.consume_key(egui::Modifiers::NONE, Key::Enter),
                input.consume_key(egui::Modifiers::NONE, Key::Escape),
            )
        });
        if escape {
            self.open = false;
            return None;
        }
        if up {
            self.cursor = self.cursor.saturating_sub(1);
        }
        if down && self.cursor + 1 < matches.len() {
            self.cursor += 1;
        }
        if enter {
            if let Some((_, _, command)) = matches.get(self.cursor) {
                if enabled(*command) {
                    picked = Some(*command);
                }
            }
        }

        let modal = egui::Modal::new(egui::Id::unique("command-palette"))
            .backdrop_color(theme::BACKDROP)
            .area(
                egui::Modal::default_area(egui::Id::unique("command-palette-area"))
                    .anchor(Align2::CENTER_TOP, vec2(0.0, 110.0)),
            )
            .frame(
                Frame::new()
                    .fill(theme::CARD)
                    .stroke(Stroke::new(1.0, theme::BORDER_STRONG))
                    .corner_radius(CornerRadius::same(14))
                    .inner_margin(Margin::same(10))
                    .shadow(theme::shadow(40, 16, 50)),
            )
            .show(ctx, |ui| {
                ui.set_width(620.0);
                ui.horizontal(|ui| {
                    let (rect, _) = ui.allocate_exact_size(vec2(22.0, 22.0), Sense::hover());
                    icons::paint(ui.painter(), rect, icons::Icon::Search, theme::TEXT_MUTED);
                    let response = ui.add(
                        TextEdit::singleline(&mut self.query)
                            .hint_text(RichText::new("Buscar telas e ações…").color(theme::TEXT_FAINT))
                            .font(theme::font(16.0))
                            .frame(Frame::NONE)
                            .desired_width(f32::INFINITY),
                    );
                    if self.focus_pending {
                        response.request_focus();
                        self.focus_pending = false;
                    }
                    if response.changed() {
                        self.cursor = 0;
                    }
                });
                ui.add_space(6.0);
                widgets::separator(ui);
                if matches.is_empty() {
                    ui.add_space(12.0);
                    ui.label(RichText::new("Nada encontrado").color(theme::TEXT_MUTED));
                    ui.add_space(12.0);
                    return;
                }
                let start = self.cursor.saturating_sub(VISIBLE_ROWS - 1);
                for (row, (_, _, command)) in matches.iter().enumerate().skip(start).take(VISIBLE_ROWS) {
                    let active = enabled(*command);
                    let (rect, response) =
                        ui.allocate_exact_size(vec2(ui.available_width(), 40.0), Sense::click());
                    if response.hovered() {
                        self.cursor = row;
                    }
                    let selected = row == self.cursor;
                    let painter = ui.painter();
                    if selected {
                        painter.rect_filled(rect, 9, theme::alpha(theme::ACCENT, 26));
                    }
                    let fg = if active { theme::TEXT } else { theme::TEXT_FAINT };
                    let icon_color = if !active {
                        theme::TEXT_FAINT
                    } else if selected {
                        theme::ACCENT
                    } else {
                        theme::TEXT_MUTED
                    };
                    icons::paint(
                        painter,
                        icons::icon_rect(rect.left_center() + vec2(20.0, 0.0), 18.0),
                        command.icon(),
                        icon_color,
                    );
                    painter.text(
                        rect.left_center() + vec2(42.0, 0.0),
                        Align2::LEFT_CENTER,
                        command.label(),
                        theme::font(14.0),
                        fg,
                    );
                    let mut right = rect.right() - 12.0;
                    if let Some(shortcut) = command.shortcut() {
                        let text = ctx.format_shortcut(&shortcut);
                        let galley = painter.layout_no_wrap(text, theme::semibold(11.5), theme::TEXT_MUTED);
                        let key = egui::Rect::from_min_size(
                            egui::pos2(right - galley.size().x - 14.0, rect.center().y - 11.0),
                            vec2(galley.size().x + 14.0, 22.0),
                        );
                        painter.rect_filled(key, 6, theme::RAISED);
                        painter.rect_stroke(
                            key,
                            6,
                            Stroke::new(1.0, theme::BORDER),
                            egui::StrokeKind::Inside,
                        );
                        painter.galley(key.center() - galley.size() / 2.0, galley, theme::TEXT_MUTED);
                        right = key.left() - 10.0;
                    }
                    painter.text(
                        egui::pos2(right, rect.center().y),
                        Align2::RIGHT_CENTER,
                        command.group(),
                        theme::font(12.0),
                        theme::TEXT_FAINT,
                    );
                    if response.clicked() && active {
                        picked = Some(*command);
                    }
                }
                ui.add_space(4.0);
                ui.label(
                    RichText::new("↑ ↓ navegar   ·   Enter executar   ·   Esc fechar")
                        .size(11.5)
                        .color(theme::TEXT_FAINT),
                );
            });
        if modal.should_close() {
            self.open = false;
        }
        if picked.is_some() {
            self.open = false;
        }
        picked
    }
}
