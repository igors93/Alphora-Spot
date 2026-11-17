//! Locates the Python core: project root, interpreter and the shared run id.

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Debug)]
pub struct Workspace {
    /// Source checkout or explicit `ALPHORA_PROJECT_ROOT`; `None` lets the core pick the
    /// per-user directory of an installed package (reported back by the bridge).
    pub root: Option<PathBuf>,
    pub python: PathBuf,
    /// Shared by the interface, the bridge and every child so audit trails line up.
    pub run_id: String,
}

impl Workspace {
    pub fn discover() -> Self {
        let root = explicit_root().or_else(find_source_checkout);
        let python = find_python(root.as_deref());
        Self {
            root,
            python,
            run_id: new_run_id(),
        }
    }

    /// Directory used as working directory for children before the bridge reports one.
    pub fn working_dir(&self) -> PathBuf {
        self.root
            .clone()
            .or_else(|| std::env::current_dir().ok())
            .unwrap_or_else(|| PathBuf::from("."))
    }

    /// Environment applied to every Python child (bridge, engine, tasks).
    pub fn base_env(&self, root: &Path) -> Vec<(String, String)> {
        let mut env = vec![
            ("ALPHORA_PROJECT_ROOT".into(), root.display().to_string()),
            ("ALPHORA_RUN_ID".into(), self.run_id.clone()),
            ("ALPHORA_PARENT_RUN_ID".into(), self.run_id.clone()),
            ("PYTHONUTF8".into(), "1".into()),
            ("PYTHONIOENCODING".into(), "utf-8".into()),
            ("PYTHONUNBUFFERED".into(), "1".into()),
        ];
        let src = root.join("src");
        if src.join("desktop").join("bridge").is_dir() {
            let separator = if cfg!(windows) { ";" } else { ":" };
            let python_path = match std::env::var("PYTHONPATH") {
                Ok(existing) if !existing.trim().is_empty() => {
                    format!("{}{separator}{existing}", src.display())
                }
                _ => src.display().to_string(),
            };
            env.push(("PYTHONPATH".into(), python_path));
        }
        env
    }
}

fn explicit_root() -> Option<PathBuf> {
    let value = std::env::var("ALPHORA_PROJECT_ROOT").ok()?;
    let trimmed = value.trim();
    (!trimmed.is_empty()).then(|| PathBuf::from(trimmed))
}

fn find_source_checkout() -> Option<PathBuf> {
    let starts = [
        std::env::current_exe()
            .ok()
            .and_then(|exe| exe.parent().map(Path::to_path_buf)),
        std::env::current_dir().ok(),
    ];
    starts.into_iter().flatten().find_map(|start| {
        start.ancestors().find_map(|dir| {
            let is_checkout = dir.join("main.py").is_file()
                && dir.join("pyproject.toml").is_file()
                && dir.join("src").join("desktop").join("bridge").is_dir();
            is_checkout.then(|| dir.to_path_buf())
        })
    })
}

fn find_python(root: Option<&Path>) -> PathBuf {
    if let Ok(explicit) = std::env::var("ALPHORA_PYTHON") {
        if !explicit.trim().is_empty() {
            return PathBuf::from(explicit.trim());
        }
    }
    if let Some(root) = root {
        let relative: &[&str] = if cfg!(windows) {
            &[".venv/Scripts/python.exe", "venv/Scripts/python.exe"]
        } else {
            &[".venv/bin/python", "venv/bin/python"]
        };
        if let Some(found) = relative.iter().map(|rel| root.join(rel)).find(|p| p.is_file()) {
            return found;
        }
    }
    PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
}

fn new_run_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or_default();
    let mixed = (nanos as u64) ^ ((std::process::id() as u64) << 40) ^ 0x9E37_79B9_7F4A_7C15;
    format!("{:012x}", mixed & 0xFFFF_FFFF_FFFF)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_id_is_twelve_hex_chars() {
        let id = new_run_id();
        assert_eq!(id.len(), 12);
        assert!(id.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn base_env_points_python_to_source_tree() {
        let dir = std::env::temp_dir().join(format!("alphora-ws-{}", new_run_id()));
        std::fs::create_dir_all(dir.join("src").join("desktop").join("bridge")).unwrap();
        let workspace = Workspace {
            root: Some(dir.clone()),
            python: PathBuf::from("python"),
            run_id: "abc".into(),
        };
        let env = workspace.base_env(&dir);
        let python_path = env.iter().find(|(k, _)| k == "PYTHONPATH").unwrap();
        assert!(python_path.1.starts_with(&dir.join("src").display().to_string()));
        assert!(env.iter().any(|(k, v)| k == "ALPHORA_RUN_ID" && v == "abc"));
        std::fs::remove_dir_all(dir).ok();
    }
}
