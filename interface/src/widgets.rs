//! Reusable, custom-painted components shared by every page.

use std::sync::Arc;

use egui::text::{LayoutJob, TextWrapping};
use egui::{
    Align, Align2, Color32, CornerRadius, Frame, Galley, Layout, Margin, Rect, Response, RichText, Sense,
    Stroke, StrokeKind, TextEdit, Ui, Vec2, pos2, vec2,
};

use crate::icons::{self, Icon};
use crate::logs::{Level, LogBuffer};
use crate::theme::{self, Tone};

// ---------------------------------------------------------------- layout

pub fn card<R>(ui: &mut Ui, add: impl FnOnce(&mut Ui) -> R) -> R {
    Frame::new()
        .fill(theme::CARD)
        .stroke(Stroke::new(1.0, theme::BORDER))
        .corner_radius(CornerRadius::same(12))
        .inner_margin(Margin::same(18))
        .shadow(theme::shadow(14, 3, 16))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui)
        })
        .inner
}

/// Title row inside a card: icon, title, optional subtitle and right-aligned actions.
pub fn card_header(ui: &mut Ui, icon: Icon, title: &str, subtitle: &str, actions: impl FnOnce(&mut Ui)) {
    ui.horizontal(|ui| {
        let (rect, _) = ui.allocate_exact_size(Vec2::splat(30.0), Sense::hover());
        ui.painter().rect_filled(rect, 8, theme::alpha(theme::ACCENT, 22));
        icons::paint(ui.painter(), rect.shrink(7.0), icon, theme::ACCENT);
        // Actions are laid out first so a long subtitle truncates instead of overlapping them.
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            actions(ui);
            ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                ui.vertical(|ui| {
                    ui.spacing_mut().item_spacing.y = 1.0;
                    ui.add(
                        egui::Label::new(
                            RichText::new(title)
                                .font(theme::semibold(15.0))
                                .color(theme::TEXT),
                        )
                        .truncate(),
                    );
                    if !subtitle.is_empty() {
                        ui.add(
                            egui::Label::new(RichText::new(subtitle).size(12.0).color(theme::TEXT_MUTED))
                                .truncate(),
                        );
                    }
                });
            });
        });
    });
    ui.add_space(10.0);
}

/// Lays `count` items out in rows of `columns` equal-width cells.
pub fn grid(ui: &mut Ui, columns: usize, count: usize, mut cell: impl FnMut(&mut Ui, usize)) {
    let columns = columns.max(1);
    let mut index = 0;
    while index < count {
        let row_len = columns.min(count - index);
        ui.columns(columns, |cols| {
            for (offset, col) in cols.iter_mut().take(row_len).enumerate() {
                cell(col, index + offset);
            }
        });
        index += row_len;
        if index < count {
            ui.add_space(4.0);
        }
    }
}

/// Picks a column count that keeps fields at least `min_width` wide.
pub fn columns_for(ui: &Ui, min_width: f32, max: usize) -> usize {
    ((ui.available_width() / min_width).floor() as usize).clamp(1, max)
}

pub fn field_label(ui: &mut Ui, label: &str) {
    ui.label(RichText::new(label).size(12.0).color(theme::TEXT_MUTED));
}

/// Label above a control, the standard form cell.
pub fn field<R>(ui: &mut Ui, label: &str, add: impl FnOnce(&mut Ui) -> R) -> R {
    ui.vertical(|ui| {
        ui.spacing_mut().item_spacing.y = 4.0;
        field_label(ui, label);
        add(ui)
    })
    .inner
}

pub fn separator(ui: &mut Ui) {
    let width = ui.available_width();
    let (rect, _) = ui.allocate_exact_size(vec2(width, 9.0), Sense::hover());
    ui.painter()
        .hline(rect.x_range(), rect.center().y, Stroke::new(1.0, theme::BORDER));
}

// ---------------------------------------------------------------- inputs

pub fn text_input(ui: &mut Ui, value: &mut String, hint: &str) -> Response {
    ui.add(
        TextEdit::singleline(value)
            .hint_text(RichText::new(hint).color(theme::TEXT_FAINT))
            .desired_width(f32::INFINITY)
            .margin(Margin::symmetric(10, 8)),
    )
}

pub fn secret_input(ui: &mut Ui, value: &mut String, hint: &str, reveal: bool) -> Response {
    ui.add(
        TextEdit::singleline(value)
            .hint_text(RichText::new(hint).color(theme::TEXT_FAINT))
            .password(!reveal)
            .desired_width(f32::INFINITY)
            .margin(Margin::symmetric(10, 8)),
    )
}

pub fn code_editor(ui: &mut Ui, value: &mut String, rows: usize) -> Response {
    ui.add(
        TextEdit::multiline(value)
            .font(egui::TextStyle::Monospace)
            .code_editor()
            .desired_rows(rows)
            .desired_width(f32::INFINITY)
            .margin(Margin::same(10)),
    )
}

/// Text field plus a "browse" button that opens a native file dialog.
pub fn path_input(ui: &mut Ui, value: &mut String, hint: &str, dialog: PathDialog) -> bool {
    let mut changed = false;
    ui.horizontal(|ui| {
        let button_width = 36.0;
        let width = (ui.available_width() - button_width - ui.spacing().item_spacing.x).max(80.0);
        ui.allocate_ui(vec2(width, theme::CONTROL_HEIGHT), |ui| {
            changed |= text_input(ui, value, hint).changed();
        });
        if icon_button(ui, Icon::Folder, "Procurar").clicked() {
            if let Some(path) = dialog.pick(value) {
                *value = path;
                changed = true;
            }
        }
    });
    changed
}

#[derive(Clone, Copy)]
pub enum PathDialog {
    Csv,
    Model,
    Directory,
}

impl PathDialog {
    fn pick(self, current: &str) -> Option<String> {
        let mut dialog = rfd::FileDialog::new();
        let current = std::path::Path::new(current.trim());
        if let Some(parent) = current.parent().filter(|p| p.is_dir()) {
            dialog = dialog.set_directory(parent);
        }
        let picked = match self {
            Self::Csv => dialog.add_filter("CSV", &["csv"]).pick_file(),
            Self::Model => dialog.add_filter("Modelo de IA", &["json"]).pick_file(),
            Self::Directory => dialog.pick_folder(),
        };
        picked.map(|path| path.display().to_string())
    }
}

pub fn combo(ui: &mut Ui, id: impl egui::AsIdSalt, value: &mut String, options: &[String]) -> bool {
    let pairs: Vec<(String, String)> = options.iter().map(|o| (o.clone(), o.clone())).collect();
    combo_labeled(ui, id, value, &pairs)
}

/// Dropdown over `(value, label)` pairs; returns true when the selection changed.
pub fn combo_labeled(
    ui: &mut Ui,
    id: impl egui::AsIdSalt,
    value: &mut String,
    options: &[(String, String)],
) -> bool {
    let before = value.clone();
    let selected = options
        .iter()
        .find(|(v, _)| v == value)
        .map(|(_, label)| label.clone())
        .unwrap_or_else(|| {
            if value.is_empty() {
                "—".into()
            } else {
                value.clone()
            }
        });
    let width = ui.available_width().min(520.0);
    egui::ComboBox::from_id_salt(id)
        .selected_text(selected)
        .width(width)
        .height(340.0)
        .show_ui(ui, |ui| {
            for (option, label) in options {
                ui.selectable_value(value, option.clone(), label);
            }
        });
    *value != before
}

/// iOS-style switch with a trailing label.
pub fn toggle(ui: &mut Ui, on: &mut bool, label: &str) -> Response {
    let text_galley = ui
        .painter()
        .layout_no_wrap(label.to_owned(), theme::font(13.5), theme::TEXT);
    let size = vec2(38.0 + 10.0 + text_galley.size().x, 26.0);
    let (rect, mut response) = ui.allocate_exact_size(size, Sense::click());
    if response.clicked() {
        *on = !*on;
        response.mark_changed();
    }
    let enabled = ui.is_enabled();
    let how_on = ui.ctx().animate_bool(response.id, *on);
    let track = Rect::from_min_size(pos2(rect.left(), rect.center().y - 10.0), vec2(38.0, 20.0));
    let off_fill = theme::BORDER_STRONG;
    let fill = lerp_color(off_fill, theme::ACCENT_STRONG, how_on);
    let stroke = if response.hovered() {
        theme::alpha(theme::ACCENT, 160)
    } else {
        theme::BORDER_STRONG
    };
    let dim = |c: Color32| if enabled { c } else { theme::alpha(c, 110) };
    ui.painter().rect_filled(track, 10, dim(fill));
    ui.painter()
        .rect_stroke(track, 10, Stroke::new(1.0, dim(stroke)), StrokeKind::Inside);
    let knob_x = egui::lerp(track.left() + 10.0..=track.right() - 10.0, how_on);
    ui.painter()
        .circle_filled(pos2(knob_x, track.center().y), 7.0, dim(Color32::WHITE));
    ui.painter().galley(
        pos2(track.right() + 10.0, rect.center().y - text_galley.size().y / 2.0),
        text_galley,
        dim(theme::TEXT),
    );
    response
}

/// Pill-shaped segmented control; returns true when the value changed.
pub fn segmented(ui: &mut Ui, value: &mut String, options: &[(String, String, Tone)]) -> bool {
    let mut changed = false;
    Frame::new()
        .fill(theme::INPUT)
        .stroke(Stroke::new(1.0, theme::BORDER_STRONG))
        .corner_radius(CornerRadius::same(9))
        .inner_margin(Margin::same(3))
        .show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 2.0;
                for (option, label, tone) in options {
                    let selected = option == value;
                    let galley =
                        ui.painter()
                            .layout_no_wrap(label.clone(), theme::semibold(12.5), theme::TEXT);
                    let size = vec2(galley.size().x + 22.0, 28.0);
                    let (rect, response) = ui.allocate_exact_size(size, Sense::click());
                    let accent = match tone {
                        Tone::Neutral | Tone::Accent => theme::ACCENT,
                        other => other.color(),
                    };
                    let (fill, text) = if selected {
                        (theme::alpha(accent, 40), accent)
                    } else if response.hovered() {
                        (theme::RAISED, theme::TEXT)
                    } else {
                        (Color32::TRANSPARENT, theme::TEXT_MUTED)
                    };
                    ui.painter().rect_filled(rect, 7, fill);
                    if selected {
                        ui.painter().rect_stroke(
                            rect,
                            7,
                            Stroke::new(1.0, theme::alpha(accent, 140)),
                            StrokeKind::Inside,
                        );
                    }
                    let text = if ui.is_enabled() {
                        text
                    } else {
                        theme::alpha(text, 110)
                    };
                    ui.painter()
                        .galley(rect.center() - galley.size() / 2.0, galley, text);
                    if response.clicked() && !selected {
                        *value = option.clone();
                        changed = true;
                    }
                }
            });
        });
    changed
}

// ---------------------------------------------------------------- buttons

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Primary,
    Secondary,
    Danger,
    Ghost,
}

pub fn button(ui: &mut Ui, kind: Kind, icon: Option<Icon>, label: &str) -> Response {
    let font = theme::semibold(13.0);
    let galley = ui.painter().layout_no_wrap(label.to_owned(), font, theme::TEXT);
    let icon_space = if icon.is_some() { 24.0 } else { 0.0 };
    let width = galley.size().x + icon_space + 28.0;
    let (rect, response) = ui.allocate_exact_size(vec2(width, theme::CONTROL_HEIGHT), Sense::click());
    let enabled = ui.is_enabled();
    let hover = hover_amount(ui, &response, enabled);
    let pressed = response.is_pointer_button_down_on() && enabled;

    let (fill, stroke, text) = match kind {
        Kind::Primary => (
            if pressed {
                theme::ACCENT_DEEP
            } else {
                lerp_color(theme::ACCENT, theme::ACCENT_STRONG, hover)
            },
            Color32::TRANSPARENT,
            theme::ON_ACCENT,
        ),
        Kind::Secondary => (
            lerp_color(theme::CARD, theme::HOVER, hover),
            lerp_color(theme::BORDER_STRONG, theme::alpha(theme::ACCENT, 150), hover),
            theme::TEXT,
        ),
        Kind::Danger => (
            theme::alpha(theme::DANGER, (24.0 + 26.0 * hover) as u8),
            theme::alpha(theme::DANGER, (110.0 + 90.0 * hover) as u8),
            theme::DANGER,
        ),
        Kind::Ghost => (
            theme::alpha(theme::RAISED, (255.0 * hover) as u8),
            Color32::TRANSPARENT,
            lerp_color(theme::TEXT_MUTED, theme::TEXT, hover),
        ),
    };
    let fade = |c: Color32| {
        if enabled {
            c
        } else {
            theme::alpha(c, (c.a() as f32 * 0.4) as u8)
        }
    };
    // A pressed button sinks by a pixel: immediate tactile feedback.
    let body = if pressed { rect.shrink(1.0) } else { rect };
    let painter = ui.painter();
    if kind == Kind::Primary && enabled {
        painter.rect_filled(
            body.translate(vec2(0.0, 2.0)),
            theme::RADIUS_SMALL,
            theme::alpha(theme::ACCENT_DEEP, (40.0 + 40.0 * hover) as u8),
        );
    }
    painter.rect_filled(body, theme::RADIUS_SMALL, fade(fill));
    if stroke != Color32::TRANSPARENT {
        painter.rect_stroke(
            body,
            theme::RADIUS_SMALL,
            Stroke::new(1.0, fade(stroke)),
            StrokeKind::Inside,
        );
    }
    let content_width = galley.size().x + icon_space;
    let mut x = body.center().x - content_width / 2.0;
    if let Some(icon) = icon {
        icons::paint(
            painter,
            icons::icon_rect(pos2(x + 8.0, body.center().y), 16.0),
            icon,
            fade(text),
        );
        x += icon_space;
    }
    painter.galley(
        pos2(x, body.center().y - galley.size().y / 2.0),
        galley,
        fade(text),
    );
    if enabled {
        response.on_hover_cursor(egui::CursorIcon::PointingHand)
    } else {
        response
    }
}

pub fn icon_button(ui: &mut Ui, icon: Icon, tooltip: &str) -> Response {
    let (rect, response) = ui.allocate_exact_size(Vec2::splat(theme::CONTROL_HEIGHT), Sense::click());
    let enabled = ui.is_enabled();
    let hover = hover_amount(ui, &response, enabled);
    let body = if response.is_pointer_button_down_on() && enabled {
        rect.shrink(1.0)
    } else {
        rect
    };
    let painter = ui.painter();
    painter.rect_filled(
        body,
        theme::RADIUS_SMALL,
        lerp_color(theme::CARD, theme::HOVER, hover),
    );
    painter.rect_stroke(
        body,
        theme::RADIUS_SMALL,
        Stroke::new(
            1.0,
            lerp_color(theme::BORDER_STRONG, theme::alpha(theme::ACCENT, 150), hover),
        ),
        StrokeKind::Inside,
    );
    let color = if enabled {
        lerp_color(theme::TEXT_MUTED, theme::ACCENT, hover)
    } else {
        theme::TEXT_FAINT
    };
    icons::paint(painter, body.shrink(9.0), icon, color);
    response.on_hover_text(tooltip)
}

/// Smoothed 0..1 hover state so colors ease in and out instead of snapping.
pub fn hover_amount(ui: &Ui, response: &Response, enabled: bool) -> f32 {
    ui.ctx()
        .animate_bool_with_time(response.id.with("hover"), response.hovered() && enabled, 0.12)
}

// ---------------------------------------------------------------- status

pub fn badge(ui: &mut Ui, text: &str, tone: Tone) -> Response {
    let galley = ui
        .painter()
        .layout_no_wrap(text.to_owned(), theme::semibold(11.5), tone.color());
    let size = vec2(galley.size().x + 26.0, 22.0);
    let (rect, response) = ui.allocate_exact_size(size, Sense::hover());
    let painter = ui.painter();
    painter.rect_filled(rect, 11, tone.soft());
    painter.rect_stroke(
        rect,
        11,
        Stroke::new(1.0, theme::alpha(tone.color(), 60)),
        StrokeKind::Inside,
    );
    painter.circle_filled(pos2(rect.left() + 11.0, rect.center().y), 3.0, tone.color());
    painter.galley(
        pos2(rect.left() + 19.0, rect.center().y - galley.size().y / 2.0),
        galley,
        tone.color(),
    );
    response
}

/// Status dot with an optional soft "live" halo.
pub fn status_dot(ui: &Ui, center: egui::Pos2, tone: Tone, live: bool) {
    let painter = ui.painter();
    if live {
        let t = ui.input(|i| i.time) as f32;
        let phase = (t * 1.6).fract();
        painter.circle_filled(
            center,
            4.0 + 7.0 * phase,
            theme::alpha(tone.color(), (90.0 * (1.0 - phase)) as u8),
        );
        ui.ctx()
            .request_repaint_after(std::time::Duration::from_millis(33));
    }
    painter.circle_filled(center, 4.5, tone.color());
}

pub fn stat_tile(ui: &mut Ui, icon: Icon, label: &str, value: &str, tone: Tone) {
    Frame::new()
        .fill(theme::CARD)
        .stroke(Stroke::new(1.0, theme::BORDER))
        .corner_radius(CornerRadius::same(12))
        .inner_margin(Margin::symmetric(16, 14))
        .shadow(theme::shadow(14, 3, 16))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                let (rect, _) = ui.allocate_exact_size(Vec2::splat(34.0), Sense::hover());
                let color = match tone {
                    Tone::Neutral => theme::ACCENT,
                    other => other.color(),
                };
                ui.painter().rect_filled(rect, 9, theme::alpha(color, 24));
                icons::paint(ui.painter(), rect.shrink(8.0), icon, color);
                ui.vertical(|ui| {
                    ui.spacing_mut().item_spacing.y = 2.0;
                    ui.label(RichText::new(label).size(11.5).color(theme::TEXT_MUTED));
                    let galley = elided(
                        ui,
                        value,
                        theme::semibold(16.0),
                        theme::TEXT,
                        ui.available_width(),
                    );
                    let (rect, response) = ui.allocate_exact_size(galley.size(), Sense::hover());
                    ui.painter().galley(rect.min, galley.clone(), theme::TEXT);
                    if galley.elided {
                        response.on_hover_text(value);
                    }
                });
            });
        });
}

pub fn banner(ui: &mut Ui, tone: Tone, icon: Icon, text: &str) {
    Frame::new()
        .fill(tone.soft())
        .stroke(Stroke::new(1.0, theme::alpha(tone.color(), 70)))
        .corner_radius(CornerRadius::same(10))
        .inner_margin(Margin::symmetric(14, 10))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                let (rect, _) = ui.allocate_exact_size(Vec2::splat(18.0), Sense::hover());
                icons::paint(ui.painter(), rect, icon, tone.color());
                ui.label(RichText::new(text).color(theme::TEXT).size(13.0));
            });
        });
}

pub fn spinner(ui: &mut Ui, size: f32) {
    let (rect, _) = ui.allocate_exact_size(Vec2::splat(size), Sense::hover());
    let t = ui.input(|i| i.time) as f32;
    let start = t * 5.0;
    let points: Vec<egui::Pos2> = (0..=20)
        .map(|i| {
            let a = start + i as f32 / 20.0 * 4.2;
            rect.center() + vec2(a.cos(), a.sin()) * (size / 2.0 - 2.0)
        })
        .collect();
    ui.painter()
        .add(egui::Shape::line(points, Stroke::new(2.0, theme::ACCENT)));
    ui.ctx()
        .request_repaint_after(std::time::Duration::from_millis(16));
}

pub fn indeterminate_bar(ui: &mut Ui) {
    let width = ui.available_width();
    let (rect, _) = ui.allocate_exact_size(vec2(width, 4.0), Sense::hover());
    ui.painter().rect_filled(rect, 2, theme::RAISED);
    let t = (ui.input(|i| i.time) as f32 * 0.6).fract();
    let segment = rect.width() * 0.28;
    let x = rect.left() - segment + (rect.width() + segment) * t;
    let bar = Rect::from_min_max(
        pos2(x.max(rect.left()), rect.top()),
        pos2((x + segment).min(rect.right()), rect.bottom()),
    );
    ui.painter().rect_filled(bar, 2, theme::ACCENT);
    ui.ctx()
        .request_repaint_after(std::time::Duration::from_millis(16));
}

pub fn empty_state(ui: &mut Ui, icon: Icon, title: &str, text: &str) {
    ui.vertical_centered(|ui| {
        ui.add_space(18.0);
        let (rect, _) = ui.allocate_exact_size(Vec2::splat(40.0), Sense::hover());
        ui.painter()
            .circle_filled(rect.center(), 20.0, theme::alpha(theme::ACCENT, 18));
        icons::paint(ui.painter(), rect.shrink(10.0), icon, theme::ACCENT);
        ui.add_space(6.0);
        ui.label(
            RichText::new(title)
                .font(theme::semibold(14.0))
                .color(theme::TEXT),
        );
        ui.label(RichText::new(text).size(12.5).color(theme::TEXT_MUTED));
        ui.add_space(18.0);
    });
}

pub fn key_value(ui: &mut Ui, key: &str, value: &str) {
    ui.horizontal(|ui| {
        ui.label(RichText::new(key).size(12.5).color(theme::TEXT_MUTED));
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            ui.label(RichText::new(value).size(12.5).color(theme::TEXT));
        });
    });
}

// ---------------------------------------------------------------- text helpers

pub fn elided(ui: &Ui, text: &str, font: egui::FontId, color: Color32, max_width: f32) -> Arc<Galley> {
    let mut job = LayoutJob::simple_singleline(text.to_owned(), font, color);
    job.wrap = TextWrapping {
        max_width: max_width.max(8.0),
        max_rows: 1,
        break_anywhere: true,
        overflow_character: Some('…'),
    };
    ui.painter().layout_job(job)
}

pub fn lerp_color(a: Color32, b: Color32, t: f32) -> Color32 {
    let mix = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t).round() as u8;
    Color32::from_rgba_unmultiplied(
        mix(a.r(), b.r()),
        mix(a.g(), b.g()),
        mix(a.b(), b.b()),
        mix(a.a(), b.a()),
    )
}

// ---------------------------------------------------------------- tables

pub struct Column {
    pub title: &'static str,
    pub width: f32,
}

pub const ROW_HEIGHT: f32 = 34.0;

pub fn table_width(columns: &[Column]) -> f32 {
    columns.iter().map(|c| c.width).sum()
}

/// Adapts the designed column widths to the width actually available.
///
/// Extra space goes to the last column (the free-text one: reason, details, path).
/// When space is short every column shrinks in proportion, down to 60% of its design
/// width; only below that does the table keep its width and scroll horizontally.
pub fn fit_columns(columns: &[Column], available: f32) -> Vec<Column> {
    const MIN_SHARE: f32 = 0.6;
    let total = table_width(columns);
    let mut fitted: Vec<Column> = columns
        .iter()
        .map(|column| Column {
            title: column.title,
            width: column.width,
        })
        .collect();
    if total <= 0.0 || fitted.is_empty() {
        return fitted;
    }
    if available >= total {
        if let Some(last) = fitted.last_mut() {
            last.width += available - total;
        }
    } else {
        let scale = (available / total).max(MIN_SHARE);
        for column in &mut fitted {
            column.width = (column.width * scale).floor();
        }
    }
    fitted
}

pub fn table_header(ui: &mut Ui, columns: &[Column]) {
    let (rect, _) = ui.allocate_exact_size(
        vec2(table_width(columns).max(ui.available_width()), 30.0),
        Sense::hover(),
    );
    let painter = ui.painter();
    painter.rect_filled(
        rect,
        CornerRadius {
            nw: 8,
            ne: 8,
            sw: 0,
            se: 0,
        },
        theme::RAISED,
    );
    let mut x = rect.left();
    for column in columns {
        let cell = Rect::from_min_size(pos2(x, rect.top()), vec2(column.width, rect.height()));
        let galley = elided(
            ui,
            column.title,
            theme::semibold(11.5),
            theme::TEXT_MUTED,
            column.width - 20.0,
        );
        painter.galley(
            pos2(cell.left() + 12.0, cell.center().y - galley.size().y / 2.0),
            galley,
            theme::TEXT_MUTED,
        );
        x += column.width;
    }
}

pub struct Cell {
    pub text: String,
    pub color: Color32,
    pub badge: Option<Tone>,
}

impl Cell {
    pub fn text(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            color: theme::TEXT,
            badge: None,
        }
    }

    pub fn muted(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            color: theme::TEXT_MUTED,
            badge: None,
        }
    }

    pub fn colored(text: impl Into<String>, color: Color32) -> Self {
        Self {
            text: text.into(),
            color,
            badge: None,
        }
    }

    pub fn badge(text: impl Into<String>, tone: Tone) -> Self {
        Self {
            text: text.into(),
            color: tone.color(),
            badge: Some(tone),
        }
    }
}

/// Paints one clickable table row; long cell values are elided with a tooltip.
pub fn table_row(ui: &mut Ui, columns: &[Column], cells: &[Cell], selected: bool, striped: bool) -> Response {
    let width = table_width(columns).max(ui.available_width());
    let (rect, response) = ui.allocate_exact_size(vec2(width, ROW_HEIGHT), Sense::click());
    let painter = ui.painter();
    let fill = if selected {
        theme::alpha(theme::ACCENT, 30)
    } else if response.hovered() {
        theme::HOVER
    } else if striped {
        theme::STRIPE
    } else {
        Color32::TRANSPARENT
    };
    painter.rect_filled(rect, 0, fill);
    if selected {
        painter.rect_filled(
            Rect::from_min_size(rect.min, vec2(3.0, rect.height())),
            0,
            theme::ACCENT,
        );
    }
    painter.hline(
        rect.x_range(),
        rect.bottom() - 0.5,
        Stroke::new(1.0, theme::BORDER),
    );
    let mut x = rect.left();
    let mut hover_text: Option<&str> = None;
    let pointer = ui.input(|i| i.pointer.hover_pos());
    for (column, cell) in columns.iter().zip(cells) {
        let cell_rect = Rect::from_min_size(pos2(x, rect.top()), vec2(column.width, rect.height()));
        if let Some(tone) = cell.badge {
            let galley = elided(
                ui,
                &cell.text,
                theme::semibold(11.0),
                tone.color(),
                column.width - 40.0,
            );
            let pill = Rect::from_min_size(
                pos2(cell_rect.left() + 10.0, cell_rect.center().y - 10.0),
                vec2(galley.size().x + 22.0, 20.0),
            );
            painter.rect_filled(pill, 10, tone.soft());
            painter.circle_filled(pos2(pill.left() + 9.0, pill.center().y), 2.5, tone.color());
            painter.galley(
                pos2(pill.left() + 16.0, pill.center().y - galley.size().y / 2.0),
                galley,
                tone.color(),
            );
        } else {
            let galley = elided(ui, &cell.text, theme::font(12.5), cell.color, column.width - 22.0);
            if galley.elided && pointer.is_some_and(|p| cell_rect.contains(p)) {
                hover_text = Some(&cell.text);
            }
            painter.galley(
                pos2(
                    cell_rect.left() + 12.0,
                    cell_rect.center().y - galley.size().y / 2.0,
                ),
                galley,
                cell.color,
            );
        }
        x += column.width;
    }
    match hover_text {
        Some(text) => response.on_hover_text(text.to_owned()),
        None => response,
    }
}

// ---------------------------------------------------------------- read-only text

/// Virtualized monospace viewer for large read-only text (journals, state files).
pub fn text_lines(ui: &mut Ui, id: &str, lines: &[String], height: f32) {
    Frame::new()
        .fill(theme::INPUT)
        .stroke(Stroke::new(1.0, theme::BORDER))
        .corner_radius(CornerRadius::same(10))
        .inner_margin(Margin::symmetric(12, 8))
        .show(ui, |ui| {
            let row_height = 17.0;
            let frame_rect = ui.max_rect();
            egui::ScrollArea::both()
                .id_salt(id)
                .max_height(height)
                .auto_shrink([false, false])
                .show_rows(ui, row_height, lines.len(), |ui, range| {
                    ui.spacing_mut().item_spacing.y = 0.0;
                    for line in &lines[range] {
                        let color = if line.starts_with(' ') || line.starts_with('{') || line.starts_with('"')
                        {
                            theme::TEXT_MUTED
                        } else {
                            theme::TEXT
                        };
                        let galley = ui
                            .painter()
                            .layout_no_wrap(line.clone(), theme::mono(12.0), color);
                        let width = galley.size().x.max(ui.available_width());
                        let (rect, _) = ui.allocate_exact_size(vec2(width, row_height), Sense::hover());
                        ui.painter().galley(
                            pos2(rect.left(), rect.center().y - galley.size().y / 2.0),
                            galley,
                            color,
                        );
                    }
                });
            // Copy button floating over the top-right corner of the viewer.
            let button = egui::Rect::from_min_size(
                pos2(frame_rect.right() - 30.0, frame_rect.top()),
                Vec2::splat(26.0),
            );
            ui.scope_builder(egui::UiBuilder::new().max_rect(button), |ui| {
                let (rect, response) = ui.allocate_exact_size(Vec2::splat(26.0), Sense::click());
                let hovered = response.hovered();
                ui.painter().rect_filled(
                    rect,
                    6,
                    if hovered {
                        theme::HOVER
                    } else {
                        theme::alpha(theme::CARD, 220)
                    },
                );
                icons::paint(
                    ui.painter(),
                    rect.shrink(6.0),
                    Icon::Copy,
                    if hovered { theme::ACCENT } else { theme::TEXT_FAINT },
                );
                if response.on_hover_text("Copiar texto").clicked() {
                    ui.ctx().copy_text(lines.join(
                        "
",
                    ));
                }
            });
        });
}

// ---------------------------------------------------------------- logs

pub struct LogView {
    pub min_level: Level,
    pub search: String,
    pub follow: bool,
    cache_key: (u64, Level, String),
    visible: Vec<usize>,
}

impl Default for LogView {
    fn default() -> Self {
        Self {
            min_level: Level::Info,
            search: String::new(),
            follow: true,
            cache_key: (u64::MAX, Level::Info, String::new()),
            visible: Vec::new(),
        }
    }
}

pub enum LogAction {
    None,
    Clear,
}

/// Toolbar + virtualized log list. Filtering is cached per buffer revision.
pub fn log_view(ui: &mut Ui, id: &str, logs: &LogBuffer, view: &mut LogView, height: f32) -> LogAction {
    let mut action = LogAction::None;
    ui.horizontal(|ui| {
        let mut level = match view.min_level {
            Level::Info => "INFO",
            Level::Warning => "WARNING",
            Level::Error => "ERROR",
        }
        .to_string();
        let options = [
            ("INFO".to_string(), "Tudo".to_string(), Tone::Accent),
            ("WARNING".to_string(), "Avisos".to_string(), Tone::Warning),
            ("ERROR".to_string(), "Erros".to_string(), Tone::Danger),
        ];
        if segmented(ui, &mut level, &options) {
            view.min_level = Level::parse(&level);
        }
        ui.add_sized(
            vec2(220.0, theme::CONTROL_HEIGHT),
            TextEdit::singleline(&mut view.search)
                .hint_text(RichText::new("Buscar nos logs").color(theme::TEXT_FAINT))
                .margin(Margin::symmetric(10, 8)),
        );
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            if icon_button(ui, Icon::Trash, "Limpar logs").clicked() {
                action = LogAction::Clear;
            }
            if icon_button(ui, Icon::Copy, "Copiar logs").clicked() {
                ui.ctx().copy_text(logs.to_plain_text());
            }
            toggle(ui, &mut view.follow, "Acompanhar");
        });
    });
    ui.add_space(6.0);

    let key = (logs.revision(), view.min_level, view.search.to_lowercase());
    if key != view.cache_key {
        let needle = key.2.clone();
        view.visible = logs
            .iter()
            .enumerate()
            .filter(|(_, entry)| entry.level >= view.min_level)
            .filter(|(_, entry)| needle.is_empty() || entry.message.to_lowercase().contains(&needle))
            .map(|(index, _)| index)
            .collect();
        view.cache_key = key;
    }

    Frame::new()
        .fill(theme::INPUT)
        .stroke(Stroke::new(1.0, theme::BORDER))
        .corner_radius(CornerRadius::same(10))
        .inner_margin(Margin::symmetric(4, 4))
        .show(ui, |ui| {
            if view.visible.is_empty() {
                ui.set_min_height(height.min(140.0));
                empty_state(
                    ui,
                    Icon::File,
                    "Sem registros",
                    "As mensagens aparecerão aqui em tempo real.",
                );
                return;
            }
            let row_height = 22.0;
            egui::ScrollArea::vertical()
                .id_salt(id)
                .max_height(height)
                .auto_shrink([false, false])
                .stick_to_bottom(view.follow)
                .show_rows(ui, row_height, view.visible.len(), |ui, range| {
                    ui.spacing_mut().item_spacing.y = 0.0;
                    for &index in &view.visible[range] {
                        let Some(entry) = logs.get(index) else { continue };
                        log_row(ui, entry, row_height);
                    }
                });
        });
    action
}

fn log_row(ui: &mut Ui, entry: &crate::logs::LogEntry, height: f32) {
    let (rect, response) = ui.allocate_exact_size(vec2(ui.available_width(), height), Sense::hover());
    let painter = ui.painter();
    if response.hovered() {
        painter.rect_filled(rect, 4, theme::HOVER);
    }
    let tone = match entry.level {
        Level::Info => Tone::Accent,
        Level::Warning => Tone::Warning,
        Level::Error => Tone::Danger,
    };
    let y = rect.center().y;
    if !entry.continuation {
        painter.text(
            pos2(rect.left() + 8.0, y),
            Align2::LEFT_CENTER,
            &entry.time,
            theme::mono(11.5),
            theme::TEXT_FAINT,
        );
        let pill = Rect::from_min_size(pos2(rect.left() + 74.0, y - 8.0), vec2(48.0, 16.0));
        painter.rect_filled(pill, 8, tone.soft());
        painter.text(
            pill.center(),
            Align2::CENTER_CENTER,
            entry.level.label(),
            theme::semibold(10.0),
            tone.color(),
        );
        painter.text(
            pos2(rect.left() + 130.0, y),
            Align2::LEFT_CENTER,
            entry.source,
            theme::font(11.5),
            theme::TEXT_MUTED,
        );
    }
    let message_left = rect.left() + 222.0;
    let color = match entry.level {
        Level::Info => theme::TEXT,
        other => {
            if other == Level::Error {
                theme::DANGER
            } else {
                theme::WARNING
            }
        }
    };
    let galley = elided(
        ui,
        &entry.message,
        theme::mono(12.0),
        color,
        rect.right() - message_left - 8.0,
    );
    let elided_text = galley.elided;
    ui.painter()
        .galley(pos2(message_left, y - galley.size().y / 2.0), galley, color);
    if elided_text {
        response.on_hover_text(entry.message.clone());
    }
}

// ---------------------------------------------------------------- loading & navigation

/// Shimmering placeholder shown until the first data arrives (no layout jump later).
pub fn skeleton(ui: &mut Ui, width: f32, height: f32) {
    let (rect, _) = ui.allocate_exact_size(vec2(width.min(ui.available_width()), height), Sense::hover());
    let painter = ui.painter_at(rect);
    painter.rect_filled(rect, 7, theme::RAISED);
    let t = (ui.input(|i| i.time) as f32 * 0.9).fract();
    let band = rect.width().max(120.0) * 0.35;
    let x = rect.left() - band + (rect.width() + 2.0 * band) * t;
    let mut mesh = egui::Mesh::default();
    let clear = theme::alpha(Color32::WHITE, 0);
    let shine = theme::alpha(Color32::WHITE, 150);
    for (i, (from, to, a, b)) in [
        (x, x + band / 2.0, clear, shine),
        (x + band / 2.0, x + band, shine, clear),
    ]
    .into_iter()
    .enumerate()
    {
        let base = (i * 4) as u32;
        mesh.colored_vertex(pos2(from, rect.top()), a);
        mesh.colored_vertex(pos2(to, rect.top()), b);
        mesh.colored_vertex(pos2(to, rect.bottom()), b);
        mesh.colored_vertex(pos2(from, rect.bottom()), a);
        mesh.add_triangle(base, base + 1, base + 2);
        mesh.add_triangle(base, base + 2, base + 3);
    }
    painter.add(egui::Shape::mesh(mesh));
    ui.ctx()
        .request_repaint_after(std::time::Duration::from_millis(33));
}

/// A few stacked skeleton lines, the loading state of a card.
pub fn skeleton_block(ui: &mut Ui, lines: usize) {
    for index in 0..lines {
        let width = ui.available_width() * if index % 3 == 2 { 0.55 } else { 1.0 };
        skeleton(ui, width, 18.0);
        ui.add_space(8.0);
    }
}

pub struct NavEntry<'a> {
    pub icon: Icon,
    pub label: &'a str,
    pub hint: &'a str,
    /// Small colored dot after the label (unsaved changes, running task, ...).
    pub marker: Option<Tone>,
}

/// Vertical list of sub-sections inside a workspace; returns the clicked index.
pub fn section_nav(ui: &mut Ui, id: &str, entries: &[NavEntry<'_>], selected: usize) -> Option<usize> {
    let mut clicked = None;
    for (index, entry) in entries.iter().enumerate() {
        let (rect, response) = ui.allocate_exact_size(vec2(ui.available_width(), 50.0), Sense::click());
        let hover =
            ui.ctx()
                .animate_bool_with_time(ui.scope_id().with((id, index)), response.hovered(), 0.12);
        let active = index == selected;
        let painter = ui.painter();
        if active {
            painter.rect_filled(rect, 9, theme::alpha(theme::ACCENT, 26));
            painter.rect_filled(
                Rect::from_min_size(
                    pos2(rect.left(), rect.top() + 12.0),
                    vec2(3.0, rect.height() - 24.0),
                ),
                2,
                theme::ACCENT,
            );
        } else if hover > 0.0 {
            painter.rect_filled(rect, 9, theme::alpha(theme::RAISED, (255.0 * hover) as u8));
        }
        let color = if active {
            theme::ACCENT
        } else {
            lerp_color(theme::TEXT_MUTED, theme::TEXT, hover)
        };
        icons::paint(
            painter,
            icons::icon_rect(pos2(rect.left() + 22.0, rect.center().y), 18.0),
            entry.icon,
            color,
        );
        let text_left = rect.left() + 42.0;
        let label_font = if active {
            theme::semibold(13.5)
        } else {
            theme::font(13.5)
        };
        let label = elided(
            ui,
            entry.label,
            label_font,
            theme::TEXT,
            rect.right() - text_left - 22.0,
        );
        let label_width = label.size().x;
        ui.painter()
            .galley(pos2(text_left, rect.top() + 8.0), label, theme::TEXT);
        let hint = elided(
            ui,
            entry.hint,
            theme::font(11.5),
            theme::TEXT_MUTED,
            rect.right() - text_left - 10.0,
        );
        ui.painter()
            .galley(pos2(text_left, rect.top() + 28.0), hint, theme::TEXT_MUTED);
        if let Some(tone) = entry.marker {
            ui.painter().circle_filled(
                pos2(text_left + label_width + 9.0, rect.top() + 16.0),
                3.5,
                tone.color(),
            );
        }
        if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
            clicked = Some(index);
        }
    }
    clicked
}

/// Readiness line: green check, red alert or neutral dot, plus a short explanation.
pub fn check_item(ui: &mut Ui, state: Option<bool>, title: &str, detail: &str) {
    ui.horizontal(|ui| {
        let (rect, _) = ui.allocate_exact_size(Vec2::splat(26.0), Sense::hover());
        let (tone, icon) = match state {
            Some(true) => (Tone::Success, Icon::Check),
            Some(false) => (Tone::Danger, Icon::Warning),
            None => (Tone::Muted, Icon::Info),
        };
        ui.painter().circle_filled(rect.center(), 12.0, tone.soft());
        icons::paint(ui.painter(), rect.shrink(7.0), icon, tone.color());
        ui.vertical(|ui| {
            ui.spacing_mut().item_spacing.y = 0.0;
            ui.label(
                RichText::new(title)
                    .font(theme::semibold(13.0))
                    .color(theme::TEXT),
            );
            ui.add(egui::Label::new(RichText::new(detail).size(12.0).color(theme::TEXT_MUTED)).truncate());
        });
    });
}

/// Compact label/value pair used in inspector strips.
pub fn mini_stat(ui: &mut Ui, label: &str, value: &str, color: Color32) {
    ui.vertical(|ui| {
        ui.spacing_mut().item_spacing.y = 1.0;
        ui.label(RichText::new(label).size(11.5).color(theme::TEXT_MUTED));
        ui.add(egui::Label::new(RichText::new(value).font(theme::semibold(14.5)).color(color)).truncate());
    });
}

/// Two side-by-side columns (`ratio` of the width on the left); stacks them on narrow
/// windows. `add` is called with side 0 (left) and then side 1 (right).
pub fn split(ui: &mut Ui, ratio: f32, mut add: impl FnMut(&mut Ui, usize)) {
    const GAP: f32 = 16.0;
    let total = ui.available_width();
    if total < 860.0 {
        add(ui, 0);
        ui.add_space(GAP);
        add(ui, 1);
        return;
    }
    let left_width = ((total - GAP) * ratio).floor();
    let right_width = total - GAP - left_width;
    let top_left = ui.cursor().min;
    let bottom = ui.max_rect().bottom();
    let mut height: f32 = 0.0;
    for (side, (x, width)) in [(0.0, left_width), (left_width + GAP, right_width)]
        .into_iter()
        .enumerate()
    {
        let min = top_left + vec2(x, 0.0);
        let rect = Rect::from_min_max(min, pos2(min.x + width, bottom.max(min.y + 1.0)));
        let mut child = ui.new_child(
            egui::UiBuilder::new()
                .max_rect(rect)
                .layout(Layout::top_down(Align::Min)),
        );
        child.set_width(width);
        add(&mut child, side);
        height = height.max(child.min_rect().height());
    }
    ui.advance_cursor_after_rect(Rect::from_min_size(top_left, vec2(total, height)));
}
