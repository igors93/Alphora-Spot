//! Alphora desktop: native interface for the Python trading core.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod app;
mod bridge;
mod commands;
mod dialogs;
mod icons;
mod logs;
mod pages;
mod palette;
mod process;
mod profile;
mod theme;
mod toast;
mod widgets;
mod workspace;

use std::sync::Arc;

fn main() -> eframe::Result {
    install_crash_log();
    let viewport = egui::ViewportBuilder::default()
        .with_title("Alphora")
        .with_app_id("alphora-desktop")
        // Open maximized so the window always fits the monitor's work area (above the
        // taskbar); a fixed size taller than the screen hides the status bar, toasts and
        // the save bar. The inner size only applies when the user restores the window.
        .with_maximized(true)
        .with_inner_size([1280.0, 760.0])
        .with_min_inner_size([960.0, 600.0])
        .with_icon(Arc::new(icons::window_icon()));
    let options = eframe::NativeOptions {
        viewport,
        centered: true,
        ..Default::default()
    };
    eframe::run_native(
        "Alphora",
        options,
        Box::new(|cc| Ok(Box::new(app::AlphoraApp::new(cc)))),
    )
}

/// Writes panics to `logs/desktop-crash.log` (the release build has no console).
fn install_crash_log() {
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let root = workspace::Workspace::discover().working_dir();
        let dir = root.join("logs");
        let _ = std::fs::create_dir_all(&dir);
        let backtrace = std::backtrace::Backtrace::force_capture();
        let report = format!("{info}\n\n{backtrace}\n");
        let _ = std::fs::write(dir.join("desktop-crash.log"), report);
        default_hook(info);
    }));
}
