//! Every user action as a value: the menu bar, keyboard shortcuts and the command
//! palette all dispatch the same [`Command`]s, so they can never drift apart.

use egui::{Key, KeyboardShortcut, Modifiers};

use crate::icons::Icon;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Workspace {
    Dashboard,
    Symbols,
    Strategy,
    Research,
    Monitor,
    Settings,
}

impl Workspace {
    pub const ALL: [Self; 6] = [
        Self::Dashboard,
        Self::Symbols,
        Self::Strategy,
        Self::Research,
        Self::Monitor,
        Self::Settings,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::Dashboard => "Painel",
            Self::Symbols => "Símbolos",
            Self::Strategy => "Estratégia e IA",
            Self::Research => "Pesquisa",
            Self::Monitor => "Monitor",
            Self::Settings => "Configurações",
        }
    }

    pub fn description(self) -> &'static str {
        match self {
            Self::Dashboard => "Estado do engine, prontidão e posições",
            Self::Symbols => "Perfis por moeda e risco do portfólio",
            Self::Strategy => "Estratégia global, IA e módulos inteligentes",
            Self::Research => "Backtest e treinamento de modelos",
            Self::Monitor => "Eventos, journals e estado do runtime",
            Self::Settings => "Credenciais e núcleo Python",
        }
    }

    pub fn icon(self) -> Icon {
        match self {
            Self::Dashboard => Icon::Pulse,
            Self::Symbols => Icon::Coins,
            Self::Strategy => Icon::Chip,
            Self::Research => Icon::Candles,
            Self::Monitor => Icon::Eye,
            Self::Settings => Icon::Key,
        }
    }

    pub fn shortcut(self) -> KeyboardShortcut {
        let key = match self {
            Self::Dashboard => Key::Num1,
            Self::Symbols => Key::Num2,
            Self::Strategy => Key::Num3,
            Self::Research => Key::Num4,
            Self::Monitor => Key::Num5,
            Self::Settings => Key::Num6,
        };
        KeyboardShortcut::new(Modifiers::COMMAND, key)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Command {
    Goto(Workspace),
    Start,
    Stop,
    ForceStop,
    Save,
    Reload,
    Palette,
    Console,
    Shortcuts,
    About,
    RunBacktest,
    DownloadCandles,
    FetchRules,
    TrainAll,
    TrainGlobal,
    TrainRefined,
    CancelResearch,
    ValidateModel,
    RefreshDiagnostics,
    RotateJournals,
    RotateIntelligent,
    RestartCore,
    OpenProject,
    OpenConfig,
    OpenData,
    OpenReports,
    OpenRuntime,
    OpenGovernorState,
    OpenGovernorEvents,
    ZoomIn,
    ZoomOut,
    ZoomReset,
    Quit,
}

impl Command {
    /// Commands offered by the palette, in display order.
    pub const PALETTE: [Self; 33] = [
        Self::Goto(Workspace::Dashboard),
        Self::Goto(Workspace::Symbols),
        Self::Goto(Workspace::Strategy),
        Self::Goto(Workspace::Research),
        Self::Goto(Workspace::Monitor),
        Self::Goto(Workspace::Settings),
        Self::Start,
        Self::Stop,
        Self::ForceStop,
        Self::Save,
        Self::Reload,
        Self::Console,
        Self::RunBacktest,
        Self::DownloadCandles,
        Self::FetchRules,
        Self::TrainAll,
        Self::TrainGlobal,
        Self::TrainRefined,
        Self::CancelResearch,
        Self::ValidateModel,
        Self::RefreshDiagnostics,
        Self::RotateJournals,
        Self::RotateIntelligent,
        Self::RestartCore,
        Self::OpenProject,
        Self::OpenConfig,
        Self::OpenData,
        Self::OpenReports,
        Self::OpenRuntime,
        Self::OpenGovernorState,
        Self::OpenGovernorEvents,
        Self::Shortcuts,
        Self::About,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::Goto(workspace) => workspace.label(),
            Self::Start => "Iniciar Alphora",
            Self::Stop => "Parar Alphora",
            Self::ForceStop => "Forçar parada do engine",
            Self::Save => "Salvar alterações",
            Self::Reload => "Recarregar configuração",
            Self::Palette => "Paleta de comandos",
            Self::Console => "Mostrar ou ocultar console",
            Self::Shortcuts => "Atalhos de teclado",
            Self::About => "Sobre o Alphora",
            Self::RunBacktest => "Executar backtest",
            Self::DownloadCandles => "Baixar candles históricos",
            Self::FetchRules => "Buscar regras do mercado na Binance",
            Self::TrainAll => "Treinar modelos global e refinados",
            Self::TrainGlobal => "Treinar modelo global",
            Self::TrainRefined => "Treinar modelo refinado",
            Self::CancelResearch => "Cancelar tarefa de pesquisa",
            Self::ValidateModel => "Validar modelo global de IA",
            Self::RefreshDiagnostics => "Atualizar diagnóstico inteligente",
            Self::RotateJournals => "Arquivar journals…",
            Self::RotateIntelligent => "Arquivar decisões inteligentes…",
            Self::RestartCore => "Reiniciar núcleo Python",
            Self::OpenProject => "Abrir pasta do projeto",
            Self::OpenConfig => "Abrir pasta de configuração",
            Self::OpenData => "Abrir pasta de dados",
            Self::OpenReports => "Abrir pasta de relatórios",
            Self::OpenRuntime => "Abrir pasta de runtime",
            Self::OpenGovernorState => "Abrir estado do Governor",
            Self::OpenGovernorEvents => "Abrir eventos do Governor",
            Self::ZoomIn => "Aumentar zoom",
            Self::ZoomOut => "Diminuir zoom",
            Self::ZoomReset => "Zoom padrão",
            Self::Quit => "Sair",
        }
    }

    pub fn group(self) -> &'static str {
        match self {
            Self::Goto(_) => "Ir para",
            Self::Start | Self::Stop | Self::ForceStop => "Operação",
            Self::RunBacktest
            | Self::DownloadCandles
            | Self::FetchRules
            | Self::TrainAll
            | Self::TrainGlobal
            | Self::TrainRefined
            | Self::CancelResearch => "Pesquisa",
            Self::OpenProject
            | Self::OpenConfig
            | Self::OpenData
            | Self::OpenReports
            | Self::OpenRuntime
            | Self::OpenGovernorState
            | Self::OpenGovernorEvents => "Arquivos",
            _ => "Geral",
        }
    }

    pub fn icon(self) -> Icon {
        match self {
            Self::Goto(workspace) => workspace.icon(),
            Self::Start => Icon::Play,
            Self::Stop => Icon::Stop,
            Self::ForceStop => Icon::Power,
            Self::Save => Icon::Save,
            Self::Reload | Self::RefreshDiagnostics | Self::RestartCore => Icon::Refresh,
            Self::Palette => Icon::Bolt,
            Self::Console => Icon::File,
            Self::Shortcuts | Self::About => Icon::Info,
            Self::RunBacktest => Icon::Candles,
            Self::DownloadCandles => Icon::Download,
            Self::FetchRules => Icon::Server,
            Self::TrainAll | Self::TrainGlobal | Self::TrainRefined => Icon::Network,
            Self::CancelResearch => Icon::Close,
            Self::ValidateModel => Icon::Check,
            Self::RotateJournals | Self::RotateIntelligent => Icon::Archive,
            Self::OpenProject | Self::OpenConfig | Self::OpenData | Self::OpenReports | Self::OpenRuntime => {
                Icon::Folder
            }
            Self::OpenGovernorState | Self::OpenGovernorEvents => Icon::Open,
            Self::ZoomIn | Self::ZoomOut | Self::ZoomReset => Icon::Eye,
            Self::Quit => Icon::Power,
        }
    }

    pub fn shortcut(self) -> Option<KeyboardShortcut> {
        let ctrl = |key| Some(KeyboardShortcut::new(Modifiers::COMMAND, key));
        match self {
            Self::Goto(workspace) => Some(workspace.shortcut()),
            Self::Start => Some(KeyboardShortcut::new(Modifiers::NONE, Key::F9)),
            Self::Stop => Some(KeyboardShortcut::new(Modifiers::SHIFT, Key::F9)),
            Self::Save => ctrl(Key::S),
            Self::Reload => Some(KeyboardShortcut::new(Modifiers::NONE, Key::F5)),
            Self::Palette => ctrl(Key::K),
            Self::Console => ctrl(Key::J),
            Self::Shortcuts => Some(KeyboardShortcut::new(Modifiers::NONE, Key::F1)),
            Self::RunBacktest => Some(KeyboardShortcut::new(Modifiers::COMMAND, Key::B)),
            Self::ZoomIn => ctrl(Key::Plus),
            Self::ZoomOut => ctrl(Key::Minus),
            Self::ZoomReset => ctrl(Key::Num0),
            Self::Quit => ctrl(Key::Q),
            _ => None,
        }
    }

    /// Every command that owns a global keyboard shortcut.
    pub fn with_shortcuts() -> impl Iterator<Item = (Self, KeyboardShortcut)> {
        let workspaces = Workspace::ALL.into_iter().map(Self::Goto);
        let others = [
            Self::Start,
            Self::Stop,
            Self::Save,
            Self::Reload,
            Self::Palette,
            Self::Console,
            Self::Shortcuts,
            Self::RunBacktest,
            Self::ZoomIn,
            Self::ZoomOut,
            Self::ZoomReset,
            Self::Quit,
        ];
        workspaces
            .chain(others)
            .filter_map(|command| command.shortcut().map(|shortcut| (command, shortcut)))
    }
}

/// Case- and accent-insensitive subsequence score; `None` when `query` does not match.
pub fn fuzzy_score(query: &str, text: &str) -> Option<i32> {
    let query = fold(query);
    if query.is_empty() {
        return Some(0);
    }
    let text = fold(text);
    if let Some(position) = text.find(&query) {
        // Contiguous matches win, earlier ones more so.
        return Some(1000 - position as i32);
    }
    let mut score = 0;
    let mut chars = text.chars().enumerate();
    let mut last = 0;
    for wanted in query.chars() {
        let (index, _) = chars.find(|(_, c)| *c == wanted)?;
        score += 10 - (index - last).min(9) as i32;
        last = index;
    }
    Some(score)
}

fn fold(text: &str) -> String {
    text.chars()
        .map(|c| match c {
            'á' | 'à' | 'â' | 'ã' | 'Á' | 'À' | 'Â' | 'Ã' => 'a',
            'é' | 'ê' | 'É' | 'Ê' => 'e',
            'í' | 'Í' => 'i',
            'ó' | 'ô' | 'õ' | 'Ó' | 'Ô' | 'Õ' => 'o',
            'ú' | 'Ú' => 'u',
            'ç' | 'Ç' => 'c',
            other => other.to_ascii_lowercase(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fuzzy_matching_ignores_case_and_accents() {
        assert!(fuzzy_score("estrat", "Estratégia e IA").is_some());
        assert!(fuzzy_score("ESTRATEGIA", "Estratégia e IA").is_some());
        assert!(fuzzy_score("bkt", "Executar backtest").is_some());
        assert_eq!(fuzzy_score("xyz", "Painel"), None);
        let contiguous = fuzzy_score("back", "Executar backtest").unwrap();
        let scattered = fuzzy_score("bkt", "Executar backtest").unwrap();
        assert!(contiguous > scattered);
    }

    #[test]
    fn every_palette_command_has_a_label_and_unique_shortcuts() {
        for command in Command::PALETTE {
            assert!(!command.label().is_empty());
        }
        let shortcuts: Vec<_> = Command::with_shortcuts().map(|(_, s)| s).collect();
        for (index, shortcut) in shortcuts.iter().enumerate() {
            assert!(
                !shortcuts[index + 1..].contains(shortcut),
                "duplicate {shortcut:?}"
            );
        }
    }
}
