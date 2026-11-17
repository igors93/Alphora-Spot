//! Vector icons drawn with the egui painter.
//!
//! Every icon is described in a 24×24 design grid and scaled to the target rect, so it
//! stays crisp at any DPI without shipping image files.

use std::f32::consts::{PI, TAU};

use egui::epaint::{CubicBezierShape, PathShape, PathStroke};
use egui::{Color32, Painter, Pos2, Rect, Shape, Stroke, Vec2, vec2};

use crate::theme;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Icon {
    Pulse,
    Coins,
    Target,
    Chip,
    Network,
    Candles,
    Eye,
    Key,
    Play,
    Stop,
    Refresh,
    Save,
    Plus,
    Copy,
    Trash,
    Folder,
    File,
    Check,
    Close,
    Warning,
    Info,
    Search,
    Download,
    Edit,
    Duplicate,
    Power,
    Lock,
    Archive,
    Server,
    Filter,
    Bolt,
    Open,
}

struct Pen<'a> {
    painter: &'a Painter,
    origin: Pos2,
    scale: f32,
    stroke: Stroke,
}

impl Pen<'_> {
    fn p(&self, x: f32, y: f32) -> Pos2 {
        self.origin + vec2(x, y) * self.scale
    }

    fn line(&self, points: &[(f32, f32)]) {
        let pts: Vec<Pos2> = points.iter().map(|&(x, y)| self.p(x, y)).collect();
        self.painter.add(Shape::line(pts, self.stroke));
    }

    fn closed(&self, points: &[(f32, f32)]) {
        let pts: Vec<Pos2> = points.iter().map(|&(x, y)| self.p(x, y)).collect();
        self.painter.add(Shape::closed_line(pts, self.stroke));
    }

    fn filled(&self, points: &[(f32, f32)], fill: Color32) {
        let pts: Vec<Pos2> = points.iter().map(|&(x, y)| self.p(x, y)).collect();
        self.painter
            .add(PathShape::convex_polygon(pts, fill, Stroke::NONE));
    }

    fn circle(&self, x: f32, y: f32, r: f32) {
        self.painter
            .circle_stroke(self.p(x, y), r * self.scale, self.stroke);
    }

    fn dot(&self, x: f32, y: f32, r: f32) {
        self.painter
            .circle_filled(self.p(x, y), r * self.scale, self.stroke.color);
    }

    fn rect(&self, x: f32, y: f32, w: f32, h: f32, r: f32) {
        let rect = Rect::from_min_size(self.p(x, y), vec2(w, h) * self.scale);
        self.painter.rect_stroke(
            rect,
            (r * self.scale) as u8,
            self.stroke,
            egui::StrokeKind::Middle,
        );
    }

    fn arc(&self, cx: f32, cy: f32, r: f32, from: f32, to: f32) {
        let steps = 24;
        let pts: Vec<Pos2> = (0..=steps)
            .map(|i| {
                let t = from + (to - from) * i as f32 / steps as f32;
                self.p(cx + r * t.cos(), cy + r * t.sin())
            })
            .collect();
        self.painter.add(Shape::line(pts, self.stroke));
    }
}

/// Paints `icon` centered in `rect` with the given color.
pub fn paint(painter: &Painter, rect: Rect, icon: Icon, color: Color32) {
    let side = rect.width().min(rect.height());
    let scale = side / 24.0;
    let origin = rect.center() - Vec2::splat(side / 2.0);
    let pen = Pen {
        painter,
        origin,
        scale,
        stroke: Stroke::new((1.7 * scale).max(1.15), color),
    };

    match icon {
        Icon::Pulse => pen.line(&[
            (2.5, 12.5),
            (6.5, 12.5),
            (9.0, 6.0),
            (13.0, 18.5),
            (15.5, 10.5),
            (17.0, 12.5),
            (21.5, 12.5),
        ]),
        Icon::Coins => {
            pen.circle(9.0, 9.0, 5.5);
            pen.arc(15.0, 15.0, 5.5, -PI * 0.62, PI * 1.12);
            pen.line(&[(9.0, 6.6), (9.0, 11.4)]);
        }
        Icon::Target => {
            pen.circle(12.0, 12.0, 8.5);
            pen.circle(12.0, 12.0, 4.5);
            pen.dot(12.0, 12.0, 1.4);
        }
        Icon::Chip => {
            pen.rect(6.5, 6.5, 11.0, 11.0, 2.0);
            pen.rect(9.5, 9.5, 5.0, 5.0, 1.0);
            for offset in [9.5_f32, 14.5] {
                pen.line(&[(offset, 3.0), (offset, 6.5)]);
                pen.line(&[(offset, 17.5), (offset, 21.0)]);
                pen.line(&[(3.0, offset), (6.5, offset)]);
                pen.line(&[(17.5, offset), (21.0, offset)]);
            }
        }
        Icon::Network => {
            let nodes = [(5.0, 6.0), (5.0, 18.0), (12.0, 12.0), (19.0, 6.0), (19.0, 18.0)];
            for &(x, y) in &[nodes[0], nodes[1], nodes[3], nodes[4]] {
                pen.line(&[(x, y), nodes[2]]);
            }
            for &(x, y) in &nodes {
                pen.painter.circle_filled(pen.p(x, y), 2.6 * scale, theme::CARD);
                pen.circle(x, y, 2.6);
            }
        }
        Icon::Candles => {
            pen.line(&[(6.0, 3.5), (6.0, 20.5)]);
            pen.rect(4.0, 7.0, 4.0, 8.0, 1.0);
            pen.line(&[(12.0, 5.5), (12.0, 18.0)]);
            pen.filled(&[(10.0, 9.0), (14.0, 9.0), (14.0, 14.5), (10.0, 14.5)], color);
            pen.line(&[(18.0, 2.5), (18.0, 15.5)]);
            pen.rect(16.0, 5.0, 4.0, 7.0, 1.0);
        }
        Icon::Eye => {
            let left = pen.p(2.0, 12.0);
            let right = pen.p(22.0, 12.0);
            for sign in [-1.0_f32, 1.0] {
                painter.add(CubicBezierShape::from_points_stroke(
                    [
                        left,
                        pen.p(7.0, 12.0 + 8.5 * sign),
                        pen.p(17.0, 12.0 + 8.5 * sign),
                        right,
                    ],
                    false,
                    Color32::TRANSPARENT,
                    PathStroke::new(pen.stroke.width, color),
                ));
            }
            pen.circle(12.0, 12.0, 3.2);
        }
        Icon::Key => {
            pen.circle(7.5, 12.0, 4.0);
            pen.line(&[(11.5, 12.0), (21.0, 12.0)]);
            pen.line(&[(17.5, 12.0), (17.5, 15.5)]);
            pen.line(&[(20.5, 12.0), (20.5, 14.5)]);
        }
        Icon::Play => pen.filled(&[(8.0, 5.0), (19.5, 12.0), (8.0, 19.0)], color),
        Icon::Stop => {
            let r = Rect::from_min_size(pen.p(6.0, 6.0), Vec2::splat(12.0 * scale));
            painter.rect_filled(r, (2.0 * scale) as u8, color);
        }
        Icon::Refresh => {
            pen.arc(12.0, 12.0, 7.5, -PI * 0.15, PI * 1.55);
            pen.line(&[(19.6, 4.8), (19.6, 9.6), (14.8, 9.6)]);
        }
        Icon::Save => {
            pen.closed(&[(4.5, 4.5), (16.5, 4.5), (19.5, 7.5), (19.5, 19.5), (4.5, 19.5)]);
            pen.line(&[(8.0, 4.5), (8.0, 9.0), (15.0, 9.0), (15.0, 4.5)]);
            pen.rect(8.0, 13.0, 8.0, 6.5, 0.5);
        }
        Icon::Plus => {
            pen.line(&[(12.0, 5.0), (12.0, 19.0)]);
            pen.line(&[(5.0, 12.0), (19.0, 12.0)]);
        }
        Icon::Copy | Icon::Duplicate => {
            pen.rect(8.5, 8.5, 11.0, 11.0, 2.0);
            pen.line(&[(5.5, 15.5), (4.5, 15.5), (4.5, 4.5), (15.5, 4.5), (15.5, 5.5)]);
            if icon == Icon::Duplicate {
                pen.line(&[(14.0, 11.5), (14.0, 16.5)]);
                pen.line(&[(11.5, 14.0), (16.5, 14.0)]);
            }
        }
        Icon::Trash => {
            pen.line(&[(4.0, 6.5), (20.0, 6.5)]);
            pen.line(&[(9.0, 6.5), (9.0, 4.0), (15.0, 4.0), (15.0, 6.5)]);
            pen.closed(&[(6.0, 6.5), (18.0, 6.5), (17.0, 20.0), (7.0, 20.0)]);
            pen.line(&[(10.0, 10.0), (10.0, 16.5)]);
            pen.line(&[(14.0, 10.0), (14.0, 16.5)]);
        }
        Icon::Folder => pen.closed(&[
            (3.0, 6.0),
            (9.5, 6.0),
            (11.5, 8.0),
            (21.0, 8.0),
            (21.0, 19.0),
            (3.0, 19.0),
        ]),
        Icon::File => {
            pen.closed(&[(6.0, 3.5), (14.0, 3.5), (18.5, 8.0), (18.5, 20.5), (6.0, 20.5)]);
            pen.line(&[(13.5, 3.5), (13.5, 8.5), (18.5, 8.5)]);
            pen.line(&[(9.0, 13.0), (15.5, 13.0)]);
            pen.line(&[(9.0, 16.5), (15.5, 16.5)]);
        }
        Icon::Check => pen.line(&[(5.0, 12.5), (10.0, 17.5), (19.5, 7.0)]),
        Icon::Close => {
            pen.line(&[(6.5, 6.5), (17.5, 17.5)]);
            pen.line(&[(17.5, 6.5), (6.5, 17.5)]);
        }
        Icon::Warning => {
            pen.closed(&[(12.0, 3.5), (21.5, 20.0), (2.5, 20.0)]);
            pen.line(&[(12.0, 9.5), (12.0, 14.0)]);
            pen.dot(12.0, 17.0, 1.1);
        }
        Icon::Info => {
            pen.circle(12.0, 12.0, 9.0);
            pen.line(&[(12.0, 11.0), (12.0, 16.5)]);
            pen.dot(12.0, 7.8, 1.1);
        }
        Icon::Search => {
            pen.circle(10.5, 10.5, 6.0);
            pen.line(&[(15.0, 15.0), (20.5, 20.5)]);
        }
        Icon::Download => {
            pen.line(&[(12.0, 3.5), (12.0, 15.0)]);
            pen.line(&[(7.0, 10.5), (12.0, 15.5), (17.0, 10.5)]);
            pen.line(&[(4.5, 20.0), (19.5, 20.0)]);
        }
        Icon::Edit => {
            pen.closed(&[(15.5, 4.5), (19.5, 8.5), (9.0, 19.0), (4.5, 19.5), (5.0, 15.0)]);
            pen.line(&[(13.0, 7.0), (17.0, 11.0)]);
        }
        Icon::Power => {
            pen.arc(12.0, 13.0, 7.5, -PI * 0.28, PI * 1.28);
            pen.line(&[(12.0, 3.0), (12.0, 11.5)]);
        }
        Icon::Lock => {
            pen.rect(5.0, 11.0, 14.0, 9.5, 2.0);
            pen.arc(12.0, 11.0, 4.5, PI, TAU);
            pen.dot(12.0, 15.5, 1.3);
        }
        Icon::Archive => {
            pen.rect(3.5, 4.5, 17.0, 4.5, 1.0);
            pen.closed(&[(5.0, 9.0), (19.0, 9.0), (19.0, 19.5), (5.0, 19.5)]);
            pen.line(&[(10.0, 13.0), (14.0, 13.0)]);
        }
        Icon::Server => {
            pen.rect(4.0, 4.0, 16.0, 7.0, 1.5);
            pen.rect(4.0, 13.0, 16.0, 7.0, 1.5);
            pen.dot(7.5, 7.5, 1.0);
            pen.dot(7.5, 16.5, 1.0);
        }
        Icon::Filter => pen.closed(&[
            (3.5, 5.0),
            (20.5, 5.0),
            (14.0, 12.5),
            (14.0, 19.0),
            (10.0, 20.5),
            (10.0, 12.5),
        ]),
        Icon::Open => {
            pen.line(&[(13.0, 4.5), (19.5, 4.5), (19.5, 11.0)]);
            pen.line(&[(19.5, 4.5), (11.0, 13.0)]);
            pen.line(&[(16.5, 14.5), (16.5, 19.5), (4.5, 19.5), (4.5, 7.5), (9.5, 7.5)]);
        }
        Icon::Bolt => pen.filled(
            &[
                (13.5, 2.5),
                (5.5, 13.5),
                (11.0, 13.5),
                (10.5, 21.5),
                (18.5, 10.5),
                (13.0, 10.5),
            ],
            color,
        ),
    }
}

/// The Alphora mark: a light-blue gradient tile with an upward chevron and pulse.
/// `background` is the surface behind the tile, used to round its corners.
pub fn paint_logo(painter: &Painter, rect: Rect, background: Color32) {
    let radius = rect.width() * 0.26;
    let mut mesh = egui::Mesh::default();
    let top = theme::ACCENT;
    let bottom = theme::ACCENT_DEEP;
    mesh.colored_vertex(rect.left_top(), top);
    mesh.colored_vertex(rect.right_top(), top);
    mesh.colored_vertex(rect.right_bottom(), bottom);
    mesh.colored_vertex(rect.left_bottom(), bottom);
    mesh.add_triangle(0, 1, 2);
    mesh.add_triangle(0, 2, 3);
    // Clip the gradient to a rounded tile by painting it then the rounded mask edges.
    let tile = painter.with_clip_rect(rect);
    tile.add(Shape::mesh(mesh));
    paint_corner_masks(&tile, rect, radius, background);

    let w = rect.width();
    let p = |x: f32, y: f32| rect.min + vec2(x * w, y * w);
    let stroke = Stroke::new((w * 0.085).max(1.5), theme::ON_ACCENT);
    painter.add(Shape::line(
        vec![p(0.24, 0.74), p(0.5, 0.24), p(0.76, 0.74)],
        stroke,
    ));
    painter.add(Shape::line(
        vec![
            p(0.33, 0.58),
            p(0.45, 0.58),
            p(0.52, 0.48),
            p(0.58, 0.64),
            p(0.67, 0.58),
        ],
        Stroke::new((w * 0.06).max(1.2), theme::ON_ACCENT),
    ));
}

fn paint_corner_masks(painter: &Painter, rect: Rect, radius: f32, color: Color32) {
    let steps = 10;
    let corners = [
        (rect.left_top(), vec2(radius, radius), PI, 1.5 * PI),
        (rect.right_top(), vec2(-radius, radius), 1.5 * PI, TAU),
        (rect.right_bottom(), vec2(-radius, -radius), 0.0, 0.5 * PI),
        (rect.left_bottom(), vec2(radius, -radius), 0.5 * PI, PI),
    ];
    for (corner, offset, from, to) in corners {
        let center = corner + offset;
        let mut points = vec![corner];
        for i in 0..=steps {
            let t = from + (to - from) * i as f32 / steps as f32;
            points.push(center + vec2(t.cos(), t.sin()) * radius);
        }
        painter.add(Shape::convex_polygon(points, color, Stroke::NONE));
    }
}

/// Window/taskbar icon rasterized in code (anti-aliased signed-distance shapes).
pub fn window_icon() -> egui::IconData {
    const SIZE: u32 = 64;
    let mut rgba = Vec::with_capacity((SIZE * SIZE * 4) as usize);
    let top = theme::ACCENT;
    let bottom = theme::ACCENT_DEEP;
    let ink = theme::ON_ACCENT;
    let s = SIZE as f32;
    let chevron = [(0.24, 0.74), (0.5, 0.24), (0.76, 0.74)];
    let pulse = [
        (0.33, 0.58),
        (0.45, 0.58),
        (0.52, 0.48),
        (0.58, 0.64),
        (0.67, 0.58),
    ];

    for y in 0..SIZE {
        for x in 0..SIZE {
            let px = (x as f32 + 0.5) / s;
            let py = (y as f32 + 0.5) / s;
            let tile = rounded_box_coverage(px, py, 0.26, s);
            let t = py;
            let lerp = |a: u8, b: u8| (a as f32 + (b as f32 - a as f32) * t).round() as u8;
            let mut color = [
                lerp(top.r(), bottom.r()),
                lerp(top.g(), bottom.g()),
                lerp(top.b(), bottom.b()),
            ];
            let stroke_cov = polyline_coverage(px, py, &chevron, 0.0425, s)
                .max(polyline_coverage(px, py, &pulse, 0.03, s));
            for (channel, ink_channel) in color.iter_mut().zip([ink.r(), ink.g(), ink.b()]) {
                *channel =
                    (*channel as f32 * (1.0 - stroke_cov) + ink_channel as f32 * stroke_cov).round() as u8;
            }
            rgba.extend_from_slice(&[color[0], color[1], color[2], (tile * 255.0).round() as u8]);
        }
    }
    egui::IconData {
        rgba,
        width: SIZE,
        height: SIZE,
    }
}

fn rounded_box_coverage(px: f32, py: f32, radius: f32, size: f32) -> f32 {
    let qx = (px - 0.5).abs() - (0.5 - radius);
    let qy = (py - 0.5).abs() - (0.5 - radius);
    let outside = (qx.max(0.0).powi(2) + qy.max(0.0).powi(2)).sqrt();
    let distance = outside + qx.max(qy).min(0.0) - radius;
    (0.5 - distance * size).clamp(0.0, 1.0)
}

fn polyline_coverage(px: f32, py: f32, points: &[(f32, f32)], half_width: f32, size: f32) -> f32 {
    let mut best = f32::MAX;
    for pair in points.windows(2) {
        let (ax, ay) = pair[0];
        let (bx, by) = pair[1];
        let (dx, dy) = (bx - ax, by - ay);
        let t = (((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)).clamp(0.0, 1.0);
        let (cx, cy) = (ax + dx * t - px, ay + dy * t - py);
        best = best.min((cx * cx + cy * cy).sqrt());
    }
    (0.5 - (best - half_width) * size).clamp(0.0, 1.0)
}

/// Square of `size` centered on `center`, the usual target rect for an icon.
pub fn icon_rect(center: Pos2, size: f32) -> Rect {
    Rect::from_center_size(center, Vec2::splat(size))
}
