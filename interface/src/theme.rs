//! Visual identity: light surfaces, light-blue accent and near-black text.
//!
//! Fonts are read from the operating system at startup (no bundled font or image
//! assets); egui's embedded fonts stay as fallback for missing glyphs.

use std::sync::Arc;

use egui::{
    Color32, CornerRadius, FontData, FontDefinitions, FontFamily, FontId, Margin, Shadow, Stroke, TextStyle,
    Vec2,
};

pub const BG: Color32 = Color32::from_rgb(0xF3, 0xF7, 0xFC);
pub const CARD: Color32 = Color32::from_rgb(0xFF, 0xFF, 0xFF);
pub const RAISED: Color32 = Color32::from_rgb(0xEC, 0xF4, 0xFC);
pub const HOVER: Color32 = Color32::from_rgb(0xE2, 0xEF, 0xFB);
pub const STRIPE: Color32 = Color32::from_rgb(0xF8, 0xFB, 0xFE);
pub const INPUT: Color32 = Color32::from_rgb(0xF8, 0xFA, 0xFD);
pub const BORDER: Color32 = Color32::from_rgb(0xE0, 0xE8, 0xF1);
pub const BORDER_STRONG: Color32 = Color32::from_rgb(0xC9, 0xD5, 0xE3);

pub const TEXT: Color32 = Color32::from_rgb(0x0B, 0x12, 0x1C);
pub const TEXT_MUTED: Color32 = Color32::from_rgb(0x4F, 0x5D, 0x70);
pub const TEXT_FAINT: Color32 = Color32::from_rgb(0x8A, 0x97, 0xA9);

pub const ACCENT: Color32 = Color32::from_rgb(0x1E, 0x9B, 0xEF);
pub const ACCENT_STRONG: Color32 = Color32::from_rgb(0x0F, 0x84, 0xD8);
pub const ACCENT_LIGHT: Color32 = Color32::from_rgb(0x7F, 0xCB, 0xFF);
pub const ACCENT_DEEP: Color32 = Color32::from_rgb(0x0A, 0x63, 0xB0);
pub const ON_ACCENT: Color32 = Color32::from_rgb(0xFF, 0xFF, 0xFF);

pub const SUCCESS: Color32 = Color32::from_rgb(0x0F, 0x9D, 0x58);
pub const WARNING: Color32 = Color32::from_rgb(0xD9, 0x7C, 0x00);
pub const DANGER: Color32 = Color32::from_rgb(0xDC, 0x2F, 0x45);

/// Dims the page behind modal dialogs.
pub const BACKDROP: Color32 = Color32::from_rgba_premultiplied(0x0B, 0x12, 0x1C, 0x55);

pub fn shadow(blur: u8, offset: i8, alpha: u8) -> Shadow {
    Shadow {
        offset: [0, offset],
        blur,
        spread: 0,
        color: Color32::from_rgba_unmultiplied(0x10, 0x2A, 0x4A, alpha),
    }
}

pub const RADIUS: u8 = 10;
pub const RADIUS_SMALL: u8 = 7;
pub const CONTROL_HEIGHT: f32 = 34.0;

pub const SEMIBOLD: &str = "semibold";

/// Semantic color for statuses reported by the core ("success", "danger", ...).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Tone {
    #[default]
    Neutral,
    Accent,
    Success,
    Warning,
    Danger,
    Muted,
}

impl Tone {
    pub fn parse(value: &str) -> Self {
        match value.trim().to_ascii_lowercase().as_str() {
            "success" | "ok" => Self::Success,
            "warning" | "warn" => Self::Warning,
            "danger" | "error" | "critical" => Self::Danger,
            "muted" => Self::Muted,
            "accent" | "info" => Self::Accent,
            _ => Self::Neutral,
        }
    }

    pub fn from_severity(severity: &str) -> Self {
        match severity.trim().to_ascii_uppercase().as_str() {
            "ERROR" | "CRITICAL" | "ERRO" | "BLOQUEADO" => Self::Danger,
            "WARNING" | "WARN" | "AGUARDANDO" | "WAIT" | "REVISÃO" => Self::Warning,
            "APLICADO" => Self::Success,
            "SHADOW" => Self::Muted,
            _ => Self::Accent,
        }
    }

    pub fn color(self) -> Color32 {
        match self {
            Self::Neutral => TEXT_MUTED,
            Self::Accent => ACCENT,
            Self::Success => SUCCESS,
            Self::Warning => WARNING,
            Self::Danger => DANGER,
            Self::Muted => TEXT_FAINT,
        }
    }

    /// Translucent background used behind badges and banners.
    pub fn soft(self) -> Color32 {
        let c = self.color();
        Color32::from_rgba_unmultiplied(c.r(), c.g(), c.b(), 26)
    }
}

pub fn alpha(color: Color32, alpha: u8) -> Color32 {
    Color32::from_rgba_unmultiplied(color.r(), color.g(), color.b(), alpha)
}

pub fn font(size: f32) -> FontId {
    FontId::proportional(size)
}

pub fn semibold(size: f32) -> FontId {
    FontId::new(size, FontFamily::Name(SEMIBOLD.into()))
}

pub fn mono(size: f32) -> FontId {
    FontId::monospace(size)
}

pub fn apply(ctx: &egui::Context) {
    ctx.set_theme(egui::ThemePreference::Light);
    ctx.set_fonts(load_fonts());
    ctx.all_styles_mut(|style| {
        style.text_styles = [
            (TextStyle::Small, font(11.5)),
            (TextStyle::Body, font(13.5)),
            (TextStyle::Button, font(13.5)),
            (TextStyle::Heading, semibold(21.0)),
            (TextStyle::Monospace, mono(12.5)),
        ]
        .into();

        let spacing = &mut style.spacing;
        spacing.item_spacing = Vec2::new(10.0, 8.0);
        spacing.button_padding = Vec2::new(12.0, 6.0);
        spacing.interact_size = Vec2::new(40.0, CONTROL_HEIGHT - 4.0);
        spacing.combo_height = 320.0;
        spacing.menu_margin = Margin::same(6);
        spacing.window_margin = Margin::same(18);
        spacing.scroll.bar_width = 8.0;
        spacing.scroll.floating = true;
        spacing.tooltip_width = 420.0;

        let visuals = &mut style.visuals;
        visuals.dark_mode = false;
        visuals.override_text_color = None;
        visuals.panel_fill = BG;
        visuals.window_fill = CARD;
        visuals.window_stroke = Stroke::new(1.0, BORDER_STRONG);
        visuals.window_corner_radius = CornerRadius::same(14);
        visuals.menu_corner_radius = CornerRadius::same(RADIUS);
        visuals.window_shadow = shadow(36, 12, 40);
        visuals.popup_shadow = shadow(22, 8, 36);
        visuals.faint_bg_color = RAISED;
        visuals.extreme_bg_color = INPUT;
        visuals.text_edit_bg_color = Some(INPUT);
        visuals.code_bg_color = INPUT;
        visuals.hyperlink_color = ACCENT;
        visuals.warn_fg_color = WARNING;
        visuals.error_fg_color = DANGER;
        visuals.selection.bg_fill = alpha(ACCENT_LIGHT, 110);
        visuals.selection.stroke = Stroke::new(1.0, ACCENT);
        visuals.text_cursor.stroke = Stroke::new(1.5, ACCENT);
        visuals.striped = false;
        visuals.indent_has_left_vline = false;
        visuals.collapsing_header_frame = false;
        visuals.disabled_alpha = 0.45;

        let radius = CornerRadius::same(RADIUS_SMALL);
        let widgets = &mut visuals.widgets;
        widgets.noninteractive.bg_fill = CARD;
        widgets.noninteractive.weak_bg_fill = CARD;
        widgets.noninteractive.bg_stroke = Stroke::new(1.0, BORDER);
        widgets.noninteractive.fg_stroke = Stroke::new(1.0, TEXT_MUTED);
        widgets.noninteractive.corner_radius = radius;

        widgets.inactive.bg_fill = INPUT;
        widgets.inactive.weak_bg_fill = CARD;
        widgets.inactive.bg_stroke = Stroke::new(1.0, BORDER_STRONG);
        widgets.inactive.fg_stroke = Stroke::new(1.0, TEXT);
        widgets.inactive.corner_radius = radius;
        widgets.inactive.expansion = 0.0;

        widgets.hovered.bg_fill = RAISED;
        widgets.hovered.weak_bg_fill = HOVER;
        widgets.hovered.bg_stroke = Stroke::new(1.0, alpha(ACCENT, 150));
        widgets.hovered.fg_stroke = Stroke::new(1.0, TEXT);
        widgets.hovered.corner_radius = radius;
        widgets.hovered.expansion = 0.0;

        widgets.active.bg_fill = HOVER;
        widgets.active.weak_bg_fill = HOVER;
        widgets.active.bg_stroke = Stroke::new(1.0, ACCENT);
        widgets.active.fg_stroke = Stroke::new(1.0, TEXT);
        widgets.active.corner_radius = radius;
        widgets.active.expansion = 0.0;

        widgets.open.bg_fill = RAISED;
        widgets.open.weak_bg_fill = RAISED;
        widgets.open.bg_stroke = Stroke::new(1.0, ACCENT);
        widgets.open.fg_stroke = Stroke::new(1.0, TEXT);
        widgets.open.corner_radius = radius;
    });
}

fn load_fonts() -> FontDefinitions {
    let mut fonts = FontDefinitions::default();
    let candidates = system_font_candidates();

    let mut install = |name: &str, family: FontFamily, paths: &[String]| {
        if let Some(bytes) = paths.iter().find_map(|path| std::fs::read(path).ok()) {
            fonts
                .font_data
                .insert(name.to_owned(), Arc::new(FontData::from_owned(bytes)));
            fonts
                .families
                .entry(family)
                .or_default()
                .insert(0, name.to_owned());
            true
        } else {
            false
        }
    };

    install("ui-regular", FontFamily::Proportional, &candidates.regular);
    if !install(
        "ui-semibold",
        FontFamily::Name(SEMIBOLD.into()),
        &candidates.semibold,
    ) {
        install(
            "ui-semibold-fallback",
            FontFamily::Name(SEMIBOLD.into()),
            &candidates.regular,
        );
    }
    install("ui-mono", FontFamily::Monospace, &candidates.mono);

    // Keep egui's embedded fonts as glyph fallback for every family.
    let fallback = fonts
        .families
        .get(&FontFamily::Proportional)
        .cloned()
        .unwrap_or_default();
    let semibold = fonts
        .families
        .entry(FontFamily::Name(SEMIBOLD.into()))
        .or_default();
    for name in fallback {
        if !semibold.contains(&name) {
            semibold.push(name);
        }
    }
    fonts
}

struct FontCandidates {
    regular: Vec<String>,
    semibold: Vec<String>,
    mono: Vec<String>,
}

fn system_font_candidates() -> FontCandidates {
    if cfg!(windows) {
        let dir = std::env::var("WINDIR").unwrap_or_else(|_| "C:\\Windows".into());
        let path = |name: &str| format!("{dir}\\Fonts\\{name}");
        FontCandidates {
            regular: vec![path("segoeui.ttf")],
            semibold: vec![path("seguisb.ttf"), path("segoeuib.ttf")],
            mono: vec![path("CascadiaMono.ttf"), path("consola.ttf")],
        }
    } else if cfg!(target_os = "macos") {
        FontCandidates {
            regular: vec!["/System/Library/Fonts/Supplemental/Arial.ttf".into()],
            semibold: vec!["/System/Library/Fonts/Supplemental/Arial Bold.ttf".into()],
            mono: vec!["/System/Library/Fonts/Supplemental/Courier New.ttf".into()],
        }
    } else {
        let roots = ["/usr/share/fonts/truetype", "/usr/share/fonts"];
        let join = |rel: &[&str]| -> Vec<String> {
            roots
                .iter()
                .flat_map(|root| rel.iter().map(move |r| format!("{root}/{r}")))
                .collect()
        };
        FontCandidates {
            regular: join(&[
                "inter/Inter-Regular.ttf",
                "noto/NotoSans-Regular.ttf",
                "dejavu/DejaVuSans.ttf",
            ]),
            semibold: join(&[
                "inter/Inter-SemiBold.ttf",
                "noto/NotoSans-SemiBold.ttf",
                "dejavu/DejaVuSans-Bold.ttf",
            ]),
            mono: join(&[
                "jetbrains-mono/JetBrainsMono-Regular.ttf",
                "noto/NotoSansMono-Regular.ttf",
                "dejavu/DejaVuSansMono.ttf",
            ]),
        }
    }
}
