//! Pages and the context they share.

pub mod backtest;
pub mod credentials;
pub mod intelligence;
pub mod observability;
pub mod operation;
pub mod strategy;
pub mod symbols;
pub mod training;

use std::collections::BTreeMap;
use std::path::Path;
use std::time::Duration;

use serde_json::{Map, Value};

use crate::bridge::{Bridge, Options, PageId, route};
use crate::toast::Toasts;

/// Everything a page may touch while handling input or replies.
pub struct Shell<'a> {
    pub bridge: &'a mut Bridge,
    pub toasts: &'a mut Toasts,
    pub options: &'a Options,
    /// True while the engine runs or is being started/stopped: configuration is frozen.
    pub locked: bool,
    /// Bumped whenever any page persists configuration; pages reload lazily.
    pub config_rev: u64,
    pub config_saved: &'a mut bool,
}

impl Shell<'_> {
    pub fn call(&mut self, page: PageId, kind: &'static str, method: &str, params: Value) {
        self.bridge.call(route(page, kind), method, params);
    }

    pub fn call_slow(
        &mut self,
        page: PageId,
        kind: &'static str,
        method: &str,
        params: Value,
        timeout: Duration,
    ) {
        self.bridge
            .call_with_timeout(route(page, kind), method, params, timeout);
    }

    /// Notifies the other pages that the configuration on disk changed.
    pub fn saved(&mut self) {
        *self.config_saved = true;
    }

    /// Opens a file or folder with the operating system's default handler.
    pub fn open_path(&mut self, path: &Path) {
        if !path.exists() {
            self.toasts.warn(
                "Arquivo não encontrado",
                format!("O caminho ainda não existe:\n{}", path.display()),
            );
            return;
        }
        if let Err(error) = open_with_system(path) {
            self.toasts
                .error("Não foi possível abrir", format!("{}: {error}", path.display()));
        }
    }

    pub fn root(&self) -> std::path::PathBuf {
        self.bridge.root()
    }

    pub fn bridge_ctx(&self) -> egui::Context {
        self.bridge.ctx()
    }
}

fn open_with_system(path: &Path) -> std::io::Result<()> {
    let mut command = if cfg!(windows) {
        let mut command = std::process::Command::new("explorer");
        command.arg(path);
        command
    } else if cfg!(target_os = "macos") {
        let mut command = std::process::Command::new("open");
        command.arg(path);
        command
    } else {
        let mut command = std::process::Command::new("xdg-open");
        command.arg(path);
        command
    };
    let mut child = command.spawn()?;
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}

/// Flat editable view of a JSON object: strings/numbers as text, booleans as flags.
#[derive(Clone, Debug, Default)]
pub struct Form {
    text: BTreeMap<String, String>,
    flags: BTreeMap<String, bool>,
}

/// Rendering a field inserts it empty; a missing field and an empty one are equal, so
/// merely drawing a form never marks it as modified.
impl PartialEq for Form {
    fn eq(&self, other: &Self) -> bool {
        let text_keys = self.text.keys().chain(other.text.keys());
        let flag_keys = self.flags.keys().chain(other.flags.keys());
        text_keys.into_iter().all(|key| self.get(key) == other.get(key))
            && flag_keys
                .into_iter()
                .all(|key| self.is_on(key) == other.is_on(key))
    }
}

impl Form {
    pub fn from_value(value: &Value) -> Self {
        let mut form = Self::default();
        if let Some(object) = value.as_object() {
            for (key, item) in object {
                match item {
                    Value::Bool(flag) => {
                        form.flags.insert(key.clone(), *flag);
                    }
                    Value::String(text) => {
                        form.text.insert(key.clone(), text.clone());
                    }
                    Value::Number(number) => {
                        form.text.insert(key.clone(), number.to_string());
                    }
                    Value::Null => {
                        form.text.insert(key.clone(), String::new());
                    }
                    _ => {}
                }
            }
        }
        form
    }

    pub fn text(&mut self, key: &str) -> &mut String {
        self.text.entry(key.to_owned()).or_default()
    }

    pub fn get(&self, key: &str) -> &str {
        self.text.get(key).map(String::as_str).unwrap_or_default()
    }

    pub fn set(&mut self, key: &str, value: impl Into<String>) {
        self.text.insert(key.to_owned(), value.into());
    }

    pub fn flag(&mut self, key: &str) -> &mut bool {
        self.flags.entry(key.to_owned()).or_default()
    }

    pub fn is_on(&self, key: &str) -> bool {
        self.flags.get(key).copied().unwrap_or_default()
    }

    /// Overlays every value from `value` (an object) onto the form.
    pub fn merge(&mut self, value: &Value) {
        let other = Self::from_value(value);
        self.text.extend(other.text);
        self.flags.extend(other.flags);
    }

    pub fn to_value(&self) -> Value {
        let mut map = Map::new();
        for (key, text) in &self.text {
            map.insert(key.clone(), Value::String(text.clone()));
        }
        for (key, flag) in &self.flags {
            map.insert(key.clone(), Value::Bool(*flag));
        }
        Value::Object(map)
    }
}

pub fn str_of(value: &Value, key: &str) -> String {
    match value.get(key) {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

pub fn bool_of(value: &Value, key: &str) -> bool {
    value.get(key).and_then(Value::as_bool).unwrap_or_default()
}

pub fn list_of(value: &Value, key: &str) -> Vec<Value> {
    value
        .get(key)
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

pub fn strings_of(value: &Value, key: &str) -> Vec<String> {
    list_of(value, key)
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| item.to_string())
        })
        .collect()
}

/// Human-readable rendering of a JSON scalar coming from the core.
pub fn display(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "—".into(),
        Some(Value::String(text)) if text.is_empty() => "—".into(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Bool(flag)) => if *flag { "Sim" } else { "Não" }.into(),
        Some(Value::Number(number)) => format_number(number.as_f64().unwrap_or_default(), number.is_f64()),
        Some(other) => other.to_string(),
    }
}

fn format_number(value: f64, fractional: bool) -> String {
    if !fractional {
        return format!("{value:.0}");
    }
    let text = format!("{value:.4}");
    let text = text.trim_end_matches('0').trim_end_matches('.');
    if text == "-0" { "0".into() } else { text.into() }
}

pub fn parse_f64(text: &str) -> Option<f64> {
    text.trim()
        .replace(',', ".")
        .parse::<f64>()
        .ok()
        .filter(|v| v.is_finite())
}

pub fn split_lines(text: &str) -> Vec<String> {
    text.lines().map(str::to_owned).collect()
}

pub fn format_duration(duration: Duration) -> String {
    let seconds = duration.as_secs();
    let (hours, minutes, seconds) = (seconds / 3600, (seconds % 3600) / 60, seconds % 60);
    if hours > 0 {
        format!("{hours:02}:{minutes:02}:{seconds:02}")
    } else {
        format!("{minutes:02}:{seconds:02}")
    }
}

/// Approximate wall-clock span of `candles` bars of `minutes` each.
pub fn horizon_label(minutes: u64, candles: u64) -> String {
    let total = minutes * candles.max(1);
    if total < 60 {
        format!("~{total} minutos")
    } else if total < 1440 {
        format!("~{} horas", total / 60)
    } else {
        format!("~{} dias", total / 1440)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn form_round_trips_scalars_and_ignores_nested() {
        let value = json!({"a": "x", "b": 3, "c": true, "d": {"nested": 1}, "e": null});
        let mut form = Form::from_value(&value);
        assert_eq!(form.get("b"), "3");
        assert!(form.is_on("c"));
        form.text("a").push('y');
        let back = form.to_value();
        assert_eq!(back["a"], "xy");
        assert_eq!(back["e"], "");
        assert!(back.get("d").is_none());
    }

    #[test]
    fn rendering_empty_fields_does_not_make_a_form_dirty() {
        let baseline = Form::from_value(&json!({"a": "1"}));
        let mut form = baseline.clone();
        form.text("never-loaded");
        form.flag("unset");
        assert_eq!(form, baseline);
        form.text("a").push('2');
        assert_ne!(form, baseline);
    }

    #[test]
    fn display_formats_core_values() {
        assert_eq!(display(Some(&json!(1.5000))), "1.5");
        assert_eq!(display(Some(&json!(12))), "12");
        assert_eq!(display(Some(&json!(""))), "—");
        assert_eq!(display(Some(&json!(true))), "Sim");
    }

    #[test]
    fn horizon_matches_core_rounding() {
        assert_eq!(horizon_label(5, 6), "~30 minutos");
        assert_eq!(horizon_label(15, 8), "~2 horas");
        assert_eq!(horizon_label(240, 12), "~2 dias");
    }

    #[test]
    fn durations_are_compact() {
        assert_eq!(format_duration(Duration::from_secs(75)), "01:15");
        assert_eq!(format_duration(Duration::from_secs(3_725)), "01:02:05");
    }
}
