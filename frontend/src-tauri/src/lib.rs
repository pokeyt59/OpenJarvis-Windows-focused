use std::sync::Arc;
use std::time::Duration;
use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::Manager;
use tauri_plugin_autostart::MacosLauncher;
use tokio::sync::Mutex;

const OLLAMA_PORT: u16 = 11434;
const JARVIS_PORT: u16 = 8000;

/// Tiny fallback model used only when even the machine's chosen model can't
/// be pulled (e.g. no network on first boot).
const FALLBACK_MODEL: &str = "qwen3:0.6b";

/// Qwen3.5 model variants, ordered smallest to largest.
/// Each entry is (ollama_tag, approximate_download_size_gb, min_ram_gb).
const QWEN35_MODELS: &[(&str, f64, f64)] = &[
    ("qwen3.5:0.8b", 1.0, 4.0),
    ("qwen3.5:2b", 2.7, 6.0),
    ("qwen3.5:4b", 3.4, 8.0),
    ("qwen3.5:9b", 6.6, 12.0),
    ("qwen3.5:27b", 17.0, 24.0),
    ("qwen3.5:35b", 24.0, 32.0),
    ("qwen3.5:122b", 81.0, 96.0),
];

/// Get total system RAM in GB (cached — RAM is fixed for the process run and
/// the Windows path shells out to `wmic`, which we don't want to repeat on
/// every model-selection call during boot).
fn total_ram_gb() -> f64 {
    use std::sync::OnceLock;
    static CACHE: OnceLock<f64> = OnceLock::new();
    *CACHE.get_or_init(detect_total_ram_gb)
}

/// Detect total system RAM in GB (uncached; see `total_ram_gb`).
fn detect_total_ram_gb() -> f64 {
    #[cfg(target_os = "macos")]
    {
        use std::process::Command;
        if let Ok(output) = Command::new("sysctl").args(["-n", "hw.memsize"]).output() {
            if let Ok(s) = String::from_utf8(output.stdout) {
                if let Ok(bytes) = s.trim().parse::<u64>() {
                    return bytes as f64 / (1024.0 * 1024.0 * 1024.0);
                }
            }
        }
    }
    #[cfg(target_os = "linux")]
    {
        if let Ok(contents) = std::fs::read_to_string("/proc/meminfo") {
            for line in contents.lines() {
                if line.starts_with("MemTotal:") {
                    if let Some(kb_str) = line.split_whitespace().nth(1) {
                        if let Ok(kb) = kb_str.parse::<u64>() {
                            return kb as f64 / (1024.0 * 1024.0);
                        }
                    }
                }
            }
        }
    }
    #[cfg(target_os = "windows")]
    {
        use std::process::Command;
        // wmic returns TotalVisibleMemorySize in KB
        let mut cmd = Command::new("wmic");
        cmd.args(["OS", "get", "TotalVisibleMemorySize", "/value"]);
        hide_console_std(&mut cmd);
        if let Ok(output) = cmd.output()
        {
            if let Ok(s) = String::from_utf8(output.stdout) {
                for line in s.lines() {
                    if let Some(val) = line.strip_prefix("TotalVisibleMemorySize=") {
                        if let Ok(kb) = val.trim().parse::<u64>() {
                            return kb as f64 / (1024.0 * 1024.0);
                        }
                    }
                }
            }
        }
    }
    8.0
}

/// Return the list of Qwen3.5 models that fit in `ram` GB, smallest first.
fn models_that_fit_for_ram(ram: f64) -> Vec<&'static str> {
    QWEN35_MODELS
        .iter()
        .filter(|(_, _, min_ram)| ram >= *min_ram)
        .map(|(tag, _, _)| *tag)
        .collect()
}

/// Pick this machine's default model from total RAM — conservatively.
///
/// Mirrors the Python `_MODEL_TIERS` policy (`core/config.py`): reserve
/// ~4 GB for the OS + Chromium webview + KV-cache, so the default never
/// over-commits memory and swaps. Critically an **8 GB** machine gets
/// `qwen3.5:2b`, NOT `qwen3.5:4b` — 4b needs the whole 8 GB for itself and
/// thrashes once Windows and the webview are accounted for.
fn preferred_model() -> &'static str {
    preferred_model_for_ram(total_ram_gb())
}

fn preferred_model_for_ram(ram: f64) -> &'static str {
    let pick = if ram < 5.0 {
        FALLBACK_MODEL
    } else if ram <= 14.0 {
        "qwen3.5:2b"
    } else if ram <= 24.0 {
        "qwen3.5:4b"
    } else if ram <= 44.0 {
        "qwen3.5:9b"
    } else {
        "qwen3.5:27b"
    };
    // Safety net: never hand back a model the RAM table says can't load.
    let fitting = models_that_fit_for_ram(ram);
    if pick == FALLBACK_MODEL || fitting.contains(&pick) {
        pick
    } else {
        fitting.first().copied().unwrap_or(FALLBACK_MODEL)
    }
}

/// Small model pulled first for a fast first-open, never larger than the
/// machine's default. On <=14 GB this equals `preferred_model()` (already
/// small: 2b, or the 0.6b fallback); bigger machines open fast on 4b and
/// upgrade to their larger default in the background (Phase 4).
fn startup_model() -> &'static str {
    startup_model_for_ram(total_ram_gb())
}

fn startup_model_for_ram(ram: f64) -> &'static str {
    if ram <= 14.0 {
        preferred_model_for_ram(ram)
    } else {
        "qwen3.5:4b"
    }
}

#[cfg(test)]
mod model_selection_tests {
    use super::{preferred_model_for_ram, startup_model_for_ram, FALLBACK_MODEL};

    #[test]
    fn eight_gb_uses_2b_never_4b() {
        // The whole point of the 8 GB tuning: 4b needs the entire 8 GB for
        // itself and swaps once Windows + the webview are accounted for.
        assert_eq!(preferred_model_for_ram(8.0), "qwen3.5:2b");
        assert_eq!(preferred_model_for_ram(7.9), "qwen3.5:2b");
        assert_eq!(startup_model_for_ram(8.0), "qwen3.5:2b");
    }

    #[test]
    fn tiers_match_python_policy() {
        assert_eq!(preferred_model_for_ram(4.0), FALLBACK_MODEL); // too small for a real model
        assert_eq!(preferred_model_for_ram(12.0), "qwen3.5:2b");
        assert_eq!(preferred_model_for_ram(16.0), "qwen3.5:4b");
        assert_eq!(preferred_model_for_ram(32.0), "qwen3.5:9b");
        assert_eq!(preferred_model_for_ram(48.0), "qwen3.5:27b");
    }

    #[test]
    fn big_machines_open_fast_then_upgrade() {
        // Fast-open stays small (4b) even when the machine's default is larger.
        assert_eq!(startup_model_for_ram(48.0), "qwen3.5:4b");
        assert_ne!(startup_model_for_ram(48.0), preferred_model_for_ram(48.0));
    }
}

/// Get the user home directory, handling both Unix (HOME) and Windows (USERPROFILE).
fn home_dir() -> String {
    std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Console-window suppression (Windows only).
//
// The Tauri shell is built with `windows_subsystem = "windows"` so it never
// owns a console. But every child process we spawn — `uv run`, `ollama
// serve`, `git clone`, `where`, `wmic`, `taskkill` — is a console-subsystem
// binary, and Windows hands each one a fresh black console window unless
// CREATE_NO_WINDOW (0x08000000) is passed at spawn time.
//
// These helpers are no-ops on macOS/Linux so call sites stay portable.
// ---------------------------------------------------------------------------

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[cfg(target_os = "windows")]
fn hide_console(cmd: &mut tokio::process::Command) -> &mut tokio::process::Command {
    cmd.creation_flags(CREATE_NO_WINDOW)
}

#[cfg(not(target_os = "windows"))]
fn hide_console(cmd: &mut tokio::process::Command) -> &mut tokio::process::Command {
    cmd
}

#[cfg(target_os = "windows")]
fn hide_console_std(cmd: &mut std::process::Command) -> &mut std::process::Command {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(CREATE_NO_WINDOW)
}

#[cfg(not(target_os = "windows"))]
fn hide_console_std(cmd: &mut std::process::Command) -> &mut std::process::Command {
    cmd
}

/// Resolve full path to a binary by checking common locations.
/// macOS .app bundles don't inherit the shell PATH, so we probe manually.
fn resolve_bin(name: &str) -> String {
    let home = home_dir();

    #[cfg(not(target_os = "windows"))]
    let candidates = vec![
        format!("/opt/homebrew/bin/{name}"),
        format!("{home}/.local/bin/{name}"),
        format!("{home}/.cargo/bin/{name}"),
        format!("/usr/local/bin/{name}"),
        format!("/usr/bin/{name}"),
    ];

    #[cfg(target_os = "windows")]
    let candidates = {
        let localappdata = std::env::var("LOCALAPPDATA").unwrap_or_default();
        let programfiles = std::env::var("ProgramFiles").unwrap_or_default();
        let programfiles_x86 = std::env::var("ProgramFiles(x86)").unwrap_or_default();
        vec![
            // Git for Windows — standard install paths
            format!("{programfiles}\\Git\\cmd\\{name}.exe"),
            format!("{programfiles_x86}\\Git\\cmd\\{name}.exe"),
            format!("{localappdata}\\Programs\\Git\\cmd\\{name}.exe"),
            // Scoop package manager
            format!("{home}\\scoop\\shims\\{name}.exe"),
            // winget shims (e.g. `winget install ngrok.ngrok`)
            format!("{localappdata}\\Microsoft\\WinGet\\Links\\{name}.exe"),
            // Cargo, local bin
            format!("{home}\\.cargo\\bin\\{name}.exe"),
            format!("{home}\\.local\\bin\\{name}.exe"),
            // Generic program locations
            format!("{localappdata}\\Programs\\{name}\\{name}.exe"),
            format!("{programfiles}\\{name}\\{name}.exe"),
            // Ollama installs to LOCALAPPDATA on Windows
            format!("{localappdata}\\Programs\\Ollama\\{name}.exe"),
            // uv installs via pip/pipx
            format!("{home}\\AppData\\Roaming\\Python\\Scripts\\{name}.exe"),
        ]
    };

    for path in &candidates {
        if std::path::Path::new(path).exists() {
            return path.clone();
        }
    }

    // Fallback: ask the OS to find it on PATH.
    // On Windows this uses `where.exe`, on Unix `which`.
    #[cfg(target_os = "windows")]
    {
        let mut probe = std::process::Command::new("where");
        probe.arg(format!("{name}.exe"));
        hide_console_std(&mut probe);
        if let Ok(output) = probe.output()
        {
            if output.status.success() {
                let stdout = String::from_utf8_lossy(&output.stdout);
                if let Some(first_line) = stdout.lines().next() {
                    let p = first_line.trim();
                    if !p.is_empty() && std::path::Path::new(p).exists() {
                        return p.to_string();
                    }
                }
            }
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        if let Ok(output) = std::process::Command::new("which").arg(name).output() {
            if output.status.success() {
                let stdout = String::from_utf8_lossy(&output.stdout);
                if let Some(first_line) = stdout.lines().next() {
                    let p = first_line.trim();
                    if !p.is_empty() && std::path::Path::new(p).exists() {
                        return p.to_string();
                    }
                }
            }
        }
    }

    name.to_string()
}

/// Find the OpenJarvis project root (contains pyproject.toml).
/// Checks OPENJARVIS_ROOT env var, walks up from the executable, then
/// probes common clone locations.
fn find_project_root() -> Option<std::path::PathBuf> {
    // 1. Explicit env var override
    if let Ok(root) = std::env::var("OPENJARVIS_ROOT") {
        let path = std::path::PathBuf::from(&root);
        if path.join("pyproject.toml").exists() {
            return Some(path);
        }
    }

    // 2. Walk up from the running executable (works in dev and .app bundle)
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent().map(|p| p.to_path_buf());
        for _ in 0..8 {
            if let Some(ref d) = dir {
                if d.join("pyproject.toml").exists() {
                    return Some(d.clone());
                }
                dir = d.parent().map(|p| p.to_path_buf());
            }
        }
    }

    // 3. Fallback: well-known direct paths
    let home = home_dir();
    let direct = [
        format!("{home}/OpenJarvis"),
        format!("{home}/projects/hazy/OpenJarvis"),
        format!("{home}/projects/OpenJarvis"),
        format!("{home}/src/OpenJarvis"),
        format!("{home}/Documents/OpenJarvis"),
        format!("{home}/Desktop/OpenJarvis"),
        format!("{home}/Developer/OpenJarvis"),
        format!("{home}/dev/OpenJarvis"),
        format!("{home}/Code/OpenJarvis"),
        format!("{home}/code/OpenJarvis"),
        format!("{home}/repos/OpenJarvis"),
        format!("{home}/github/OpenJarvis"),
    ];
    for p in &direct {
        let path = std::path::PathBuf::from(p);
        if path.join("pyproject.toml").exists() {
            return Some(path);
        }
    }

    // 4. Shallow scan: look for OpenJarvis one level inside common parent dirs.
    //    This catches clones like ~/Documents/my-stuff/OpenJarvis without
    //    needing to enumerate every possible intermediate folder.
    let scan_parents = [
        format!("{home}/Documents"),
        format!("{home}/Desktop"),
        format!("{home}/Developer"),
        format!("{home}/projects"),
        format!("{home}/repos"),
        format!("{home}/src"),
        format!("{home}/Code"),
        format!("{home}/code"),
        format!("{home}/dev"),
        format!("{home}/github"),
    ];
    for parent in &scan_parents {
        let parent_path = std::path::PathBuf::from(parent);
        if let Ok(entries) = std::fs::read_dir(&parent_path) {
            for entry in entries.flatten() {
                let candidate = entry.path().join("OpenJarvis");
                if candidate.join("pyproject.toml").exists() {
                    return Some(candidate);
                }
                // Also check if the entry itself is OpenJarvis (case-insensitive match)
                if let Some(name) = entry.file_name().to_str() {
                    if name.eq_ignore_ascii_case("openjarvis")
                        && entry.path().join("pyproject.toml").exists()
                    {
                        return Some(entry.path());
                    }
                }
            }
        }
    }

    None
}

// ---------------------------------------------------------------------------
// BackendManager — owns the Ollama + Jarvis server child processes
// ---------------------------------------------------------------------------

struct ChildHandle {
    child: tokio::process::Child,
}

impl ChildHandle {
    async fn kill(&mut self) {
        let _ = self.child.kill().await;
    }
}

/// Parameters captured the last time we successfully started the Python
/// server — so a "Restart" button can re-spawn it without going through the
/// 1–2 minute Ollama-check + model-pull + uv-sync prelude again.
#[derive(Default, Clone)]
struct LastBoot {
    project_root: Option<std::path::PathBuf>,
    model: Option<String>,
    uv_bin: Option<String>,
}

#[derive(Default)]
struct BackendManager {
    ollama: Option<ChildHandle>,
    jarvis: Option<ChildHandle>,
    /// Hidden ngrok agent opened on demand for inbound webhooks (SendBlue).
    /// Tracked here so it's torn down with the app like the other children.
    ngrok: Option<ChildHandle>,
    last_boot: LastBoot,
}

impl BackendManager {
    async fn stop_all(&mut self) {
        if let Some(ref mut h) = self.jarvis {
            h.kill().await;
        }
        self.jarvis = None;
        if let Some(ref mut h) = self.ngrok {
            h.kill().await;
        }
        self.ngrok = None;
        if let Some(ref mut h) = self.ollama {
            h.kill().await;
        }
        self.ollama = None;
    }

    /// Tear down only the ngrok tunnel (SendBlue "Remove" / stop_ngrok_tunnel).
    /// Leaves Ollama and the Python server running.
    async fn stop_ngrok(&mut self) {
        if let Some(ref mut h) = self.ngrok {
            h.kill().await;
        }
        self.ngrok = None;
    }

    /// Kill only the Python server, leaving Ollama running. Used by the
    /// "Stop API" / "Restart" buttons — Ollama re-init is expensive and
    /// almost never the thing the user is trying to bounce.
    async fn stop_jarvis(&mut self) {
        if let Some(ref mut h) = self.jarvis {
            h.kill().await;
        }
        self.jarvis = None;
    }
}

type SharedBackend = Arc<Mutex<BackendManager>>;

// ---------------------------------------------------------------------------
// Setup status (reported to frontend)
// ---------------------------------------------------------------------------

#[derive(serde::Serialize, Clone)]
struct SetupStatus {
    phase: String,
    detail: String,
    ollama_ready: bool,
    server_ready: bool,
    model_ready: bool,
    error: Option<String>,
}

impl Default for SetupStatus {
    fn default() -> Self {
        Self {
            phase: "starting".into(),
            detail: "Initializing...".into(),
            ollama_ready: false,
            server_ready: false,
            model_ready: false,
            error: None,
        }
    }
}

type SharedStatus = Arc<Mutex<SetupStatus>>;

// ---------------------------------------------------------------------------
// Health-check helpers
// ---------------------------------------------------------------------------

async fn wait_for_url(url: &str, timeout: Duration) -> bool {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let deadline = tokio::time::Instant::now() + timeout;
    while tokio::time::Instant::now() < deadline {
        if let Ok(resp) = client.get(url).send().await {
            if resp.status().is_success() {
                return true;
            }
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    false
}

async fn ollama_has_model(model: &str) -> bool {
    let url = format!("http://127.0.0.1:{}/api/tags", OLLAMA_PORT);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .unwrap();
    if let Ok(resp) = client.get(&url).send().await {
        if let Ok(body) = resp.json::<serde_json::Value>().await {
            if let Some(models) = body.get("models").and_then(|m| m.as_array()) {
                return models.iter().any(|m| {
                    m.get("name")
                        .and_then(|n| n.as_str())
                        .map(|n| {
                            n == model
                                || n.strip_suffix(":latest") == Some(model)
                                || model.strip_suffix(":latest") == Some(n)
                        })
                        .unwrap_or(false)
                });
            }
        }
    }
    false
}

async fn pull_model(model: &str) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{}/api/pull", OLLAMA_PORT);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .post(&url)
        .json(&serde_json::json!({"name": model, "stream": false}))
        .send()
        .await
        .map_err(|e| format!("Pull request failed: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("Pull returned status {}", resp.status()));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// uv sync error formatting (pure helpers — unit-tested, see #331)
// ---------------------------------------------------------------------------

/// Last `max_chars` characters of a `uv sync` stderr stream, trimmed.
///
/// uv's actionable diagnostic almost always lands at the tail of the
/// stream, so when surfacing a failure to the user we show the end, not
/// the (usually noisy progress-spinner) beginning. Operates on `char`
/// boundaries so it never splits a multi-byte UTF-8 codepoint — important
/// because Windows consoles emit non-ASCII (cp9xx) bytes.
fn uv_sync_stderr_tail(stderr: &str, max_chars: usize) -> String {
    let total = stderr.chars().count();
    let skip = total.saturating_sub(max_chars);
    stderr.chars().skip(skip).collect::<String>().trim().to_string()
}

/// Error message shown when `uv sync` runs but exits non-zero (#331).
///
/// `exit_code` is `None` when the process was terminated by a signal with
/// no exit code (rendered as "unknown" rather than a misleading -1).
fn format_uv_sync_failure(
    root: &std::path::Path,
    exit_code: Option<i32>,
    stderr: &str,
) -> String {
    let code = exit_code
        .map(|c| c.to_string())
        .unwrap_or_else(|| "unknown".to_string());
    format!(
        "`uv sync` failed in {} (exit {}). Last output:\n\n{}\n\n\
         Try opening a terminal in that directory and running \
         `uv sync --extra server` manually for the full output.",
        root.display(),
        code,
        uv_sync_stderr_tail(stderr, 800),
    )
}

/// Error message shown when `uv sync` can't even be spawned (#331) —
/// e.g. the resolved `uv` binary doesn't exist or isn't executable.
fn format_uv_sync_spawn_error(root: &std::path::Path, uv_bin: &str, err: &str) -> String {
    format!(
        "Could not run `uv sync`: {}. Verify uv is installed at \
         `{}` and the OpenJarvis repo is at `{}`.",
        err,
        uv_bin,
        root.display(),
    )
}

// ---------------------------------------------------------------------------
// Backend boot sequence (runs in background after app launch)
// ---------------------------------------------------------------------------

/// If something is already listening on JARVIS_PORT, kill it.
///
/// Used at boot (clean up zombies from a previous run that didn't shut down
/// gracefully) AND by restart_jarvis (defence-in-depth — kill() on our own
/// child should be enough, but if the user clicked Restart because the
/// server already wedged, the port may need a forced takedown).
async fn free_jarvis_port_if_busy() {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let busy = client
        .get(format!("http://127.0.0.1:{}/health", JARVIS_PORT))
        .send()
        .await
        .is_ok();
    if !busy {
        return;
    }
    #[cfg(unix)]
    {
        let _ = tokio::process::Command::new("fuser")
            .args(["-k", &format!("{}/tcp", JARVIS_PORT)])
            .output()
            .await;
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    #[cfg(target_os = "windows")]
    {
        let mut killer = tokio::process::Command::new("cmd");
        killer.args(["/C", &format!(
            "for /f \"tokens=5\" %a in ('netstat -ano ^| findstr :{port} ^| findstr LISTENING') do taskkill /PID %a /F",
            port = JARVIS_PORT,
        )]);
        hide_console(&mut killer);
        let _ = killer.output().await;
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

/// Spawn `uv run jarvis serve` and block until /health responds.
///
/// Records (root, model, uv_bin) in BackendManager.last_boot so that
/// restart_jarvis can call this again without redoing the boot prelude
/// (Ollama check, model pull, uv sync). On failure, the SetupStatus.error
/// field is populated and Err(detail) is returned.
async fn spawn_jarvis_server(
    backend: SharedBackend,
    status: SharedStatus,
    project_root: std::path::PathBuf,
    model: String,
    uv_bin: String,
) -> Result<(), String> {
    let mut cmd = tokio::process::Command::new(&uv_bin);
    cmd.args([
        "run",
        // --no-sync: the boot prelude already ran `uv sync --inexact`, so
        // don't re-reconcile here. A plain `uv run` re-prunes packages not
        // in the lockfile — including the maturin-built `openjarvis_rust`
        // extension we depend on (web_search/SSRF has no Python fallback).
        "--no-sync",
        "jarvis",
        "serve",
        "--port",
        &JARVIS_PORT.to_string(),
        "--model",
        &model,
        "--agent",
        "simple",
    ])
    .stdout(std::process::Stdio::null())
    .stderr(std::process::Stdio::piped())
    .current_dir(&project_root);
    hide_console(&mut cmd);

    // Inject cloud API keys from ~/.openjarvis/cloud-keys.env so the server
    // can see them without the user having to set system-wide env vars.
    for (key, value) in read_cloud_keys() {
        cmd.env(&key, &value);
    }

    match cmd.spawn() {
        Ok(child) => {
            let mut mgr = backend.lock().await;
            mgr.jarvis = Some(ChildHandle { child });
            mgr.last_boot = LastBoot {
                project_root: Some(project_root.clone()),
                model: Some(model.clone()),
                uv_bin: Some(uv_bin.clone()),
            };
        }
        Err(e) => {
            let detail = format!(
                "Could not start jarvis server: {}. \
                 Make sure uv is installed (https://astral.sh/uv) and the OpenJarvis repo is cloned at {}",
                e,
                project_root.display(),
            );
            status.lock().await.error = Some(detail.clone());
            return Err(detail);
        }
    }

    let server_url = format!("http://127.0.0.1:{}/health", JARVIS_PORT);
    let server_ok = wait_for_url(&server_url, Duration::from_secs(600)).await;

    if !server_ok {
        // Try to read stderr from the failed process for a useful error.
        let mut stderr_msg = String::new();
        {
            let mut mgr = backend.lock().await;
            if let Some(ref mut h) = mgr.jarvis {
                if let Some(ref mut stderr) = h.child.stderr.take() {
                    use tokio::io::AsyncReadExt;
                    let mut buf = vec![0u8; 4096];
                    if let Ok(n) = stderr.read(&mut buf).await {
                        stderr_msg = String::from_utf8_lossy(&buf[..n]).to_string();
                    }
                }
            }
        }
        let detail = if stderr_msg.is_empty() {
            format!(
                "Jarvis server did not start. Check that:\n\
                 1. uv is installed ({})\n\
                 2. The OpenJarvis repo is at {}\n\
                 3. Run 'uv sync' in that directory",
                uv_bin,
                project_root.display(),
            )
        } else {
            format!("Server failed to start: {}", stderr_msg.trim())
        };
        status.lock().await.error = Some(detail.clone());
        return Err(detail);
    }

    {
        let mut s = status.lock().await;
        s.server_ready = true;
        s.error = None;
        // phase="ready" is set by the CALLER: boot_backend waits for the
        // background model warmup first (#5); restart_jarvis sets it directly
        // (the model is already present on a restart).
    }
    Ok(())
}

async fn boot_backend(backend: SharedBackend, status: SharedStatus) {
    // Phase 1: Start Ollama
    {
        let mut s = status.lock().await;
        s.phase = "ollama".into();
        s.detail = "Starting inference engine...".into();
    }

    // Try the bundled sidecar first, fall back to system ollama
    let ollama_child = {
        let ollama_bin = resolve_bin("ollama");
        let mut cmd = tokio::process::Command::new(&ollama_bin);
        cmd.arg("serve")
            .env("OLLAMA_HOST", format!("127.0.0.1:{}", OLLAMA_PORT))
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        hide_console(&mut cmd);
        match cmd.spawn() {
            Ok(child) => Some(child),
            Err(_) => None,
        }
    };

    if let Some(child) = ollama_child {
        backend.lock().await.ollama = Some(ChildHandle { child });
    }

    let ollama_url = format!("http://127.0.0.1:{}/api/tags", OLLAMA_PORT);
    let ollama_ok = wait_for_url(&ollama_url, Duration::from_secs(30)).await;

    if !ollama_ok {
        let mut s = status.lock().await;
        s.error = Some("Could not start Ollama. Install it from https://ollama.com".into());
        return;
    }

    {
        let mut s = status.lock().await;
        s.ollama_ready = true;
        s.detail = "Inference engine ready.".into();
    }

    // Phase 2: choose this machine's model (RAM-aware) and start warming it in
    // the BACKGROUND so the download/load overlaps with the server boot below
    // (#5 — overlap warmup with server start). On low-RAM machines (<=14 GB,
    // e.g. 8 GB) preferred_model() is qwen3.5:2b, never 4b — 4b leaves no
    // headroom for Windows + the Chromium webview and swaps.
    {
        let mut s = status.lock().await;
        s.phase = "model".into();
    }
    let server_model: &'static str = if ollama_has_model(preferred_model()).await {
        preferred_model()
    } else {
        startup_model()
    };
    let warmup_handle = {
        let status = status.clone();
        let model = server_model.to_string();
        tauri::async_runtime::spawn(async move {
            {
                let mut s = status.lock().await;
                s.detail = format!("Preparing model {} (downloads in the background)...", model);
            }
            if !ollama_has_model(&model).await {
                if let Err(e) = pull_model(&model).await {
                    // Last resort so the app is still usable on a flaky network.
                    eprintln!("Warning: failed to pull {}: {}", model, e);
                    if !ollama_has_model(FALLBACK_MODEL).await {
                        let _ = pull_model(FALLBACK_MODEL).await;
                    }
                }
            }
            let mut s = status.lock().await;
            s.model_ready = true;
        })
    };

    // Phase 3: Start jarvis serve
    {
        let mut s = status.lock().await;
        s.phase = "server".into();
        s.detail = "Starting API server...".into();
    }

    let uv_bin = resolve_bin("uv");

    // Verify uv is actually installed. Concrete per-OS instructions —
    // the generic "install it from astral.sh" was the #1 source of
    // confusion on the Discord support thread; users couldn't tell whether
    // to use winget, scoop, pip, or the official installer.
    if !std::path::Path::new(&uv_bin).exists() && uv_bin == "uv" {
        let mut s = status.lock().await;
        #[cfg(target_os = "windows")]
        let msg = "Could not find 'uv' (Python package manager). \
                   To install on Windows, open PowerShell and run:\n\n\
                   powershell -ExecutionPolicy Bypass -c \"irm https://astral.sh/uv/install.ps1 | iex\"\n\n\
                   Then close and relaunch this app. \
                   (If the install completes but the app still can't find uv, \
                   you may need to log out and back in so PATH refreshes.)";
        #[cfg(target_os = "macos")]
        let msg = "Could not find 'uv' (Python package manager). \
                   To install on macOS, open Terminal and run:\n\n\
                   curl -LsSf https://astral.sh/uv/install.sh | sh\n\n\
                   Then relaunch this app.";
        #[cfg(target_os = "linux")]
        let msg = "Could not find 'uv' (Python package manager). \
                   To install on Linux, open a terminal and run:\n\n\
                   curl -LsSf https://astral.sh/uv/install.sh | sh\n\n\
                   Then relaunch this app.";
        #[cfg(not(any(target_os = "windows", target_os = "macos", target_os = "linux")))]
        let msg = "Could not find 'uv' (Python package manager). \
                   Install it from https://astral.sh/uv then relaunch.";
        s.error = Some(msg.into());
        return;
    }

    let mut project_root = find_project_root();

    if project_root.is_none() {
        // Auto-clone on first launch
        let git_bin = resolve_bin("git");

        // Check that git is installed
        if !std::path::Path::new(&git_bin).exists() && git_bin == "git" {
            let mut s = status.lock().await;
            s.error = Some(
                "Could not find 'git'. \
                 Install it from https://git-scm.com then relaunch."
                    .into(),
            );
            return;
        }

        let target_path = std::path::PathBuf::from(home_dir()).join("OpenJarvis");
        let clone_target = target_path.display().to_string();

        // If the directory exists but is not a valid project, don't overwrite
        if target_path.exists() && !target_path.join("pyproject.toml").exists() {
            let mut s = status.lock().await;
            s.error = Some(format!(
                "{} exists but is not a valid OpenJarvis project. \
                 Remove it and relaunch, or set OPENJARVIS_ROOT to the correct path.",
                clone_target,
            ));
            return;
        }

        {
            let mut s = status.lock().await;
            s.detail = "Downloading OpenJarvis (first launch)...".into();
        }

        let mut clone_cmd = tokio::process::Command::new(&git_bin);
        clone_cmd
            .args([
                "clone",
                "--depth",
                "1",
                "https://github.com/open-jarvis/OpenJarvis.git",
                &clone_target,
            ])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::piped());
        hide_console(&mut clone_cmd);
        let clone_result = clone_cmd.spawn();

        match clone_result {
            Ok(child) => match child.wait_with_output().await {
                Ok(output) if output.status.success() => {
                    project_root = Some(target_path);
                }
                Ok(output) => {
                    let stderr = String::from_utf8_lossy(&output.stderr);
                    let mut s = status.lock().await;
                    s.error = Some(format!(
                        "Failed to download OpenJarvis: {}. \
                         Clone manually: git clone https://github.com/open-jarvis/OpenJarvis.git {}",
                        stderr.trim(),
                        clone_target,
                    ));
                    return;
                }
                Err(e) => {
                    let mut s = status.lock().await;
                    s.error = Some(format!(
                        "Failed to download OpenJarvis: {}. \
                         Clone manually: git clone https://github.com/open-jarvis/OpenJarvis.git {}",
                        e, clone_target,
                    ));
                    return;
                }
            },
            Err(e) => {
                let mut s = status.lock().await;
                s.error = Some(format!(
                    "Could not run git: {}. \
                     Install git from https://git-scm.com then relaunch.",
                    e,
                ));
                return;
            }
        }
    }

    // Kill any leftover server on our port from a previous run
    free_jarvis_port_if_busy().await;

    // `server_model` was chosen — and started warming — in Phase 2 above.

    let root = project_root.as_ref().unwrap();

    // Install dependencies automatically (handles fresh clones).
    //
    // Previously we ran `uv sync` with both stdout AND stderr piped to
    // /dev/null and discarded the exit code (`let _ = …`). When `uv sync`
    // failed — Windows path issues, network problems, lockfile conflicts —
    // the user saw no error, the boot continued, `uv run jarvis serve`
    // then ran in an under-provisioned venv, and the user waited the full
    // 600s health-check window before getting "Jarvis server did not
    // become healthy in time" with no actionable detail (issue #331).
    //
    // Now: capture stderr, check the exit status, surface a useful error
    // to the user BEFORE the long server-start wait. The status detail
    // message also indicates this can take a couple of minutes on first
    // boot so users don't restart the app thinking it's stuck.
    {
        let mut s = status.lock().await;
        s.detail = "Installing dependencies (uv sync — may take 1-2 min on first boot)...".into();
    }
    let mut sync_cmd = tokio::process::Command::new(&uv_bin);
    sync_cmd
        .args([
            "sync",
            // --inexact: keep packages that aren't in the lockfile instead
            // of pruning them. The maturin-built `openjarvis_rust` extension
            // isn't a declared dependency, so a plain `uv sync` deletes it on
            // every cold boot, breaking web_search/SSRF (no Python fallback).
            "--inexact",
            "--extra", "server",
            "--extra", "inference-cloud",
            "--extra", "inference-google",
        ])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .current_dir(root);
    hide_console(&mut sync_cmd);
    let sync_output = sync_cmd.output().await;
    match sync_output {
        Ok(out) if !out.status.success() => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            let mut s = status.lock().await;
            s.error = Some(format_uv_sync_failure(root, out.status.code(), &stderr));
            return;
        }
        Err(e) => {
            let mut s = status.lock().await;
            s.error = Some(format_uv_sync_spawn_error(root, &uv_bin, &e.to_string()));
            return;
        }
        Ok(_) => {} // success — fall through
    }

    {
        let mut s = status.lock().await;
        s.detail = format!(
            "Starting server with {} from {}...",
            server_model,
            root.display(),
        );
    }

    // Spawn the Python server and wait for /health. The helper records
    // (root, model, uv_bin) in BackendManager.last_boot so the "Restart"
    // button can re-spawn the server without redoing the prelude.
    if spawn_jarvis_server(
        backend.clone(),
        status.clone(),
        root.clone(),
        server_model.to_string(),
        uv_bin.clone(),
    )
    .await
    .is_err()
    {
        // status.error is already populated by spawn_jarvis_server.
        return;
    }

    // #5: the model has been warming in parallel with the server boot above.
    // Wait for it before declaring "ready" so the first inference never races
    // a half-pulled model. On warm boots (model already present) this returns
    // immediately; on first boot it overlaps the multi-minute download with
    // the server start instead of running them back-to-back.
    let _ = warmup_handle.await;
    {
        let mut s = status.lock().await;
        s.model_ready = true;
        s.phase = "ready".into();
        s.detail = "All systems ready.".into();
        s.error = None;
    }

    // Phase 4: in the background, upgrade to the machine's full default model
    // if it is larger than what we opened with (e.g. a 32 GB box opens on 4b
    // then pulls 9b). Capped to ONE model — never the whole "fits" list — so
    // low-RAM machines (8 GB) don't fill the disk or get a model that swaps.
    {
        let pref = preferred_model();
        let opened_with = server_model;
        tauri::async_runtime::spawn(async move {
            if pref != opened_with
                && pref != FALLBACK_MODEL
                && !ollama_has_model(pref).await
            {
                let _ = pull_model(pref).await;
            }
        });
    }
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

fn api_base() -> String {
    format!("http://127.0.0.1:{}", JARVIS_PORT)
}

#[tauri::command]
async fn get_setup_status(state: tauri::State<'_, SharedStatus>) -> Result<SetupStatus, String> {
    Ok(state.lock().await.clone())
}

#[tauri::command]
fn get_api_base() -> String {
    api_base()
}

#[tauri::command]
async fn start_backend(
    backend: tauri::State<'_, SharedBackend>,
    status: tauri::State<'_, SharedStatus>,
) -> Result<(), String> {
    let b = backend.inner().clone();
    let s = status.inner().clone();
    tauri::async_runtime::spawn(boot_backend(b, s));
    Ok(())
}

#[tauri::command]
async fn stop_backend(backend: tauri::State<'_, SharedBackend>) -> Result<(), String> {
    backend.lock().await.stop_all().await;
    Ok(())
}

// ---------------------------------------------------------------------------
// API-server lifecycle (Settings page Start/Stop/Restart buttons)
//
// stop_jarvis / start_jarvis / restart_jarvis only touch the Python server.
// Ollama stays running because the model-pull state is expensive to rebuild
// and almost never the thing the user is trying to bounce.
//
// hard_restart_backend runs the full boot sequence — meant for "I edited
// pyproject.toml and need a fresh uv sync".
// ---------------------------------------------------------------------------

/// Status snapshot for the Settings card. Cheap to compute — `running`
/// reflects whether we hold a child handle; `healthy` is a 1-second probe
/// of /health (a `running: true, healthy: false` combo usually means the
/// server is mid-boot or has wedged).
#[derive(serde::Serialize, Clone)]
struct JarvisStatus {
    running: bool,
    healthy: bool,
    port: u16,
    phase: String,
    detail: String,
    error: Option<String>,
    /// Whether last_boot has remembered parameters — i.e. restart_jarvis
    /// can be used without a full hard_restart. False until the first
    /// successful boot of the current app session.
    can_fast_restart: bool,
}

#[tauri::command]
async fn get_jarvis_status(
    backend: tauri::State<'_, SharedBackend>,
    status: tauri::State<'_, SharedStatus>,
) -> Result<JarvisStatus, String> {
    let (running, can_fast_restart) = {
        let mgr = backend.lock().await;
        (mgr.jarvis.is_some(), mgr.last_boot.project_root.is_some())
    };
    // Quick probe — keep timeout small so the Settings card stays responsive.
    let healthy = {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_millis(800))
            .build()
            .unwrap();
        client
            .get(format!("http://127.0.0.1:{}/health", JARVIS_PORT))
            .send()
            .await
            .map(|r| r.status().is_success())
            .unwrap_or(false)
    };
    let s = status.lock().await;
    Ok(JarvisStatus {
        running,
        healthy,
        port: JARVIS_PORT,
        phase: s.phase.clone(),
        detail: s.detail.clone(),
        error: s.error.clone(),
        can_fast_restart,
    })
}

#[tauri::command]
async fn stop_jarvis(
    backend: tauri::State<'_, SharedBackend>,
    status: tauri::State<'_, SharedStatus>,
) -> Result<(), String> {
    backend.lock().await.stop_jarvis().await;
    let mut s = status.lock().await;
    s.server_ready = false;
    s.phase = "stopped".into();
    s.detail = "API server stopped.".into();
    s.error = None;
    Ok(())
}

/// Fast restart: kill the Python child and re-spawn using the (root, model,
/// uv_bin) recorded by the last successful boot. Requires that we've
/// already booted once this session (otherwise we don't know which Python
/// to run). Falls back with a clear error if the user clicked this without
/// ever having had a successful boot.
#[tauri::command]
async fn restart_jarvis(
    backend: tauri::State<'_, SharedBackend>,
    status: tauri::State<'_, SharedStatus>,
) -> Result<(), String> {
    let last = backend.lock().await.last_boot.clone();
    let (Some(root), Some(model), Some(uv_bin)) =
        (last.project_root, last.model, last.uv_bin)
    else {
        return Err(
            "No previous boot to restart from — use Hard restart to run the full setup."
                .to_string(),
        );
    };

    // Kill the existing child, then take down anything still bound to the port.
    backend.lock().await.stop_jarvis().await;
    free_jarvis_port_if_busy().await;

    {
        let mut s = status.lock().await;
        s.server_ready = false;
        s.phase = "server".into();
        s.detail = "Restarting API server...".into();
        s.error = None;
    }

    let b = backend.inner().clone();
    let s = status.inner().clone();
    // Spawn into the runtime so the call returns immediately — the Settings
    // card polls get_jarvis_status to surface progress.
    tauri::async_runtime::spawn(async move {
        if spawn_jarvis_server(b, s.clone(), root, model, uv_bin)
            .await
            .is_ok()
        {
            let mut st = s.lock().await;
            st.phase = "ready".into();
            st.detail = "All systems ready.".into();
            st.error = None;
        }
    });
    Ok(())
}

/// Hard restart: full boot sequence (Ollama check, model pull, uv sync,
/// server spawn). Use after a `git pull` that changed `pyproject.toml`, or
/// when something feels stuck and you want to start over.
#[tauri::command]
async fn hard_restart_backend(
    backend: tauri::State<'_, SharedBackend>,
    status: tauri::State<'_, SharedStatus>,
) -> Result<(), String> {
    backend.lock().await.stop_all().await;
    free_jarvis_port_if_busy().await;
    {
        let mut s = status.lock().await;
        // Reset everything; boot_backend will repopulate as phases complete.
        *s = SetupStatus::default();
    }
    let b = backend.inner().clone();
    let s = status.inner().clone();
    tauri::async_runtime::spawn(boot_backend(b, s));
    Ok(())
}

// ---------------------------------------------------------------------------
// ngrok tunnel — "silent" public HTTPS tunnel for inbound webhooks (SendBlue).
//
// Inbound webhook channels need a public HTTPS URL that reaches the local
// server. Rather than make the user open a terminal and run `ngrok http 8000`,
// we launch ngrok as a hidden, lifecycle-managed child — exactly like the
// Ollama / Python-server processes — and read the public URL from ngrok's
// local inspector API (http://127.0.0.1:4040/api/tunnels).
//
// On-demand only: started when the user wires up SendBlue, never at boot. An
// always-on public tunnel to localhost is a security footgun, so we don't open
// one unless the user is actively setting up an inbound channel. The child is
// tracked in BackendManager and killed on app exit (stop_all).
// ---------------------------------------------------------------------------

const NGROK_API: &str = "http://127.0.0.1:4040/api/tunnels";

#[derive(serde::Serialize, Clone)]
struct NgrokTunnel {
    public_url: String,
    /// True when we reused a tunnel already up (didn't spawn a new agent).
    already_running: bool,
}

/// Read ngrok's local inspector API and return the public HTTPS URL of the
/// tunnel forwarding to `port` (falling back to the first https tunnel if no
/// upstream addr matches). `None` when ngrok isn't running or has no https
/// tunnel yet.
async fn ngrok_public_url(port: u16) -> Option<String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .ok()?;
    let body = client
        .get(NGROK_API)
        .send()
        .await
        .ok()?
        .json::<serde_json::Value>()
        .await
        .ok()?;
    let tunnels = body.get("tunnels")?.as_array()?;
    let port_needle = port.to_string();
    let mut https: Vec<String> = Vec::new();
    for t in tunnels {
        if t.get("proto").and_then(|p| p.as_str()) != Some("https") {
            continue;
        }
        let Some(url) = t.get("public_url").and_then(|u| u.as_str()) else {
            continue;
        };
        let addr = t
            .get("config")
            .and_then(|c| c.get("addr"))
            .and_then(|a| a.as_str())
            .unwrap_or("");
        if addr.contains(&port_needle) {
            return Some(url.to_string()); // exact upstream match wins
        }
        https.push(url.to_string());
    }
    https.into_iter().next()
}

/// Start (or detect) a hidden ngrok tunnel to the API server's port and return
/// its public HTTPS URL. Idempotent: an existing tunnel (ours from earlier, or
/// one the user started manually) is reused rather than spawning a duplicate.
///
/// Error strings double as machine-readable codes the frontend switches on:
/// `NGROK_NOT_INSTALLED` and `NGROK_NO_AUTHTOKEN`.
#[tauri::command]
async fn ensure_ngrok_tunnel(
    backend: tauri::State<'_, SharedBackend>,
) -> Result<NgrokTunnel, String> {
    let port = JARVIS_PORT;

    // 1. Reuse an existing tunnel — never spawn a duplicate ngrok agent.
    if let Some(url) = ngrok_public_url(port).await {
        return Ok(NgrokTunnel {
            public_url: url,
            already_running: true,
        });
    }

    // 2. Locate the ngrok binary.
    let ngrok_bin = resolve_bin("ngrok");
    if !std::path::Path::new(&ngrok_bin).exists() && ngrok_bin == "ngrok" {
        return Err("NGROK_NOT_INSTALLED".to_string());
    }

    // 3. Launch hidden (no console window), kill-on-drop so a failed attempt
    //    never leaks a process.
    let mut cmd = tokio::process::Command::new(&ngrok_bin);
    cmd.args(["http", &port.to_string()])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    hide_console(&mut cmd);
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Could not start ngrok: {e}"))?;

    // 4. Poll the inspector API until a tunnel opens, ngrok dies, or we time out.
    let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
    loop {
        // ngrok already exited → almost always a missing/invalid authtoken.
        if matches!(child.try_wait(), Ok(Some(_))) {
            let mut stderr_msg = String::new();
            if let Some(mut err) = child.stderr.take() {
                use tokio::io::AsyncReadExt;
                let mut buf = vec![0u8; 4096];
                if let Ok(n) = err.read(&mut buf).await {
                    stderr_msg = String::from_utf8_lossy(&buf[..n]).to_string();
                }
            }
            let low = stderr_msg.to_lowercase();
            if low.contains("authtoken") || low.contains("err_ngrok_4018") {
                return Err("NGROK_NO_AUTHTOKEN".to_string());
            }
            return Err(format!(
                "ngrok exited before opening a tunnel: {}",
                stderr_msg.trim()
            ));
        }

        if let Some(url) = ngrok_public_url(port).await {
            backend.lock().await.ngrok = Some(ChildHandle { child });
            return Ok(NgrokTunnel {
                public_url: url,
                already_running: false,
            });
        }

        if tokio::time::Instant::now() >= deadline {
            let _ = child.kill().await;
            return Err("Timed out waiting for ngrok to open a tunnel. Make sure \
                 ngrok is configured with an authtoken."
                .to_string());
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
}

/// Tear down the ngrok tunnel we started (no-op if we didn't start one).
#[tauri::command]
async fn stop_ngrok_tunnel(backend: tauri::State<'_, SharedBackend>) -> Result<(), String> {
    backend.lock().await.stop_ngrok().await;
    Ok(())
}

/// One-time ngrok setup: persist the user's free authtoken via
/// `ngrok config add-authtoken <token>` so `ngrok http` can open tunnels.
/// The token is a secret — never logged; the frontend sends it from a masked
/// field and ngrok's own error text doesn't echo it back.
#[tauri::command]
async fn configure_ngrok_authtoken(token: String) -> Result<(), String> {
    let token = token.trim().to_string();
    if token.is_empty() {
        return Err("Authtoken is empty.".to_string());
    }
    let ngrok_bin = resolve_bin("ngrok");
    if !std::path::Path::new(&ngrok_bin).exists() && ngrok_bin == "ngrok" {
        return Err("NGROK_NOT_INSTALLED".to_string());
    }
    let mut cmd = tokio::process::Command::new(&ngrok_bin);
    cmd.args(["config", "add-authtoken", &token])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped());
    hide_console(&mut cmd);
    let out = cmd
        .output()
        .await
        .map_err(|e| format!("Could not run ngrok: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "ngrok rejected the authtoken: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(())
}

/// Auto-install ngrok via an available user package manager so the user never
/// has to leave the app. Prefers scoop (no admin, and its shims dir is exactly
/// what `resolve_bin` probes, so the result is found with no PATH refresh),
/// falls back to winget (built into Windows 10 1709+/11). User-initiated from
/// the SendBlue wizard. Returns the manager used ("scoop" | "winget" |
/// "already-installed"); errors `NO_PACKAGE_MANAGER` when neither is available.
#[tauri::command]
async fn install_ngrok() -> Result<String, String> {
    // Already present? (resolve_bin probes scoop/winget shim dirs + PATH.)
    let existing = resolve_bin("ngrok");
    if std::path::Path::new(&existing).exists() && existing != "ngrok" {
        return Ok("already-installed".to_string());
    }

    #[cfg(target_os = "windows")]
    {
        let install_timeout = Duration::from_secs(240);

        // 1. scoop — the user's preferred manager; no admin, and resolve_bin
        //    finds the result at ~/scoop/shims/ngrok.exe immediately.
        let scoop_cmd = format!("{}\\scoop\\shims\\scoop.cmd", home_dir());
        if std::path::Path::new(&scoop_cmd).exists() {
            // .cmd shims aren't directly CreateProcess-able — go through cmd.
            let mut cmd = tokio::process::Command::new("cmd");
            cmd.args(["/C", &scoop_cmd, "install", "ngrok"])
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped());
            hide_console(&mut cmd);
            if let Ok(Ok(out)) = tokio::time::timeout(install_timeout, cmd.output()).await {
                if out.status.success() || resolve_bin("ngrok") != "ngrok" {
                    return Ok("scoop".to_string());
                }
            }
            // scoop failed (e.g. main bucket missing) — fall through to winget.
        }

        // 2. winget — built into modern Windows; no bootstrap. ngrok lands in
        //    %LOCALAPPDATA%\Microsoft\WinGet\Links (probed by resolve_bin).
        {
            let mut cmd = tokio::process::Command::new("winget");
            cmd.args([
                "install",
                "--id",
                "ngrok.ngrok",
                "-e",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--disable-interactivity",
            ])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
            hide_console(&mut cmd);
            if let Ok(Ok(out)) = tokio::time::timeout(install_timeout, cmd.output()).await {
                if out.status.success() || resolve_bin("ngrok") != "ngrok" {
                    return Ok("winget".to_string());
                }
            }
        }
    }

    Err("NO_PACKAGE_MANAGER".to_string())
}

// ---------------------------------------------------------------------------
// OS location — native Windows Location Services for the weather connector.
// No third-party IP lookup; coordinates come straight from the OS.
// ---------------------------------------------------------------------------

/// Latitude/longitude returned to the weather connector's "use my location"
/// button.
#[derive(serde::Serialize, Clone)]
struct LatLon {
    lat: f64,
    lon: f64,
}

/// Read the current position from Windows Location Services via WinRT
/// (`Windows.Devices.Geolocation`). Runs on a dedicated COM-initialized (MTA)
/// thread: WinRT needs an apartment and `IAsyncOperation::get()` blocks. We
/// skip `RequestAccessAsync` (which must run on the UI thread) and let
/// `GetGeopositionAsync` surface a permission error directly if location is off
/// for desktop apps.
#[cfg(target_os = "windows")]
fn os_location_blocking() -> Result<(f64, f64), String> {
    use windows::Devices::Geolocation::Geolocator;
    use windows::Win32::System::Com::{CoInitializeEx, COINIT_MULTITHREADED};

    unsafe {
        // S_FALSE (already initialized on this thread) is fine; ignore it.
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
    }

    let to_msg = |e: windows::core::Error| -> String {
        format!(
            "Couldn't read your location ({}). Make sure Windows location is on \
             (Settings -> Privacy & security -> Location) and allowed for desktop apps.",
            e.message()
        )
    };

    let locator = Geolocator::new().map_err(to_msg)?;
    let pos = locator
        .GetGeopositionAsync()
        .map_err(to_msg)?
        .get()
        .map_err(to_msg)?;
    let basic = pos
        .Coordinate()
        .map_err(to_msg)?
        .Point()
        .map_err(to_msg)?
        .Position()
        .map_err(to_msg)?;
    Ok((basic.Latitude, basic.Longitude))
}

/// Current coordinates from the OS location service (Windows only).
#[tauri::command]
async fn get_os_location() -> Result<LatLon, String> {
    #[cfg(target_os = "windows")]
    {
        let (lat, lon) = tokio::task::spawn_blocking(os_location_blocking)
            .await
            .map_err(|e| e.to_string())??;
        return Ok(LatLon { lat, lon });
    }

    #[allow(unreachable_code)]
    Err("OS location is only available on Windows.".to_string())
}

/// Backup location (opt-in, user-initiated): approximate coordinates from a
/// single reputable HTTPS IP-geolocation provider. Hardened — HTTPS-only
/// client, explicit short timeout, named User-Agent, TLS validated by reqwest,
/// coordinates range-checked, and nothing persisted (the coords are returned
/// to the form only). This reaches a third party (which sees the request's
/// public IP), so the UI calls it only on an explicit click and labels the
/// result approximate. Native reqwest, so not subject to the webview CSP.
#[tauri::command]
async fn get_ip_location() -> Result<LatLon, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(6))
        .user_agent("OpenJarvis")
        .https_only(true)
        .build()
        .map_err(|e| e.to_string())?;

    let resp = client
        .get("https://ipapi.co/json/")
        .send()
        .await
        .map_err(|e| format!("IP geolocation request failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!(
            "IP geolocation service returned {}.",
            resp.status()
        ));
    }

    let body = resp
        .json::<serde_json::Value>()
        .await
        .map_err(|e| format!("IP geolocation returned invalid data: {e}"))?;
    let lat = body.get("latitude").and_then(|v| v.as_f64());
    let lon = body.get("longitude").and_then(|v| v.as_f64());
    match (lat, lon) {
        (Some(lat), Some(lon))
            if lat.is_finite()
                && lon.is_finite()
                && (-90.0..=90.0).contains(&lat)
                && (-180.0..=180.0).contains(&lon) =>
        {
            Ok(LatLon { lat, lon })
        }
        _ => Err("IP geolocation returned no usable coordinates.".to_string()),
    }
}

#[tauri::command]
async fn check_health(api_url: String) -> Result<serde_json::Value, String> {
    let url = format!(
        "{}/health",
        if api_url.is_empty() {
            api_base()
        } else {
            api_url
        }
    );
    let resp = reqwest::get(&url)
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_energy(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/telemetry/energy", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_telemetry(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/telemetry/stats", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_traces(api_url: String, limit: u32) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/traces?limit={}", base, limit))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_trace(api_url: String, trace_id: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/traces/{}", base, trace_id))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_learning_stats(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/learning/stats", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_learning_policy(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/learning/policy", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_memory_stats(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/memory/stats", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn search_memory(
    api_url: String,
    query: String,
    top_k: u32,
) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/v1/memory/search", base))
        .json(&serde_json::json!({"query": query, "top_k": top_k}))
        .send()
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_agents(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/agents", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_models(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/models", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn run_jarvis_command(args: Vec<String>) -> Result<String, String> {
    // --no-sync for the same reason as spawn_jarvis_server: don't let an
    // ad-hoc `uv run` re-prune the maturin-built `openjarvis_rust` extension
    // (the boot prelude already provisioned the env via `uv sync --inexact`).
    let mut cmd_args = vec![
        "run".to_string(),
        "--no-sync".to_string(),
        "jarvis".to_string(),
    ];
    cmd_args.extend(args);
    let uv_bin = resolve_bin("uv");
    let mut cmd = tokio::process::Command::new(&uv_bin);
    cmd.args(&cmd_args);
    hide_console(&mut cmd);
    let output = cmd
        .output()
        .await
        .map_err(|e| format!("Failed to launch jarvis: {}", e))?;

    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).to_string())
    } else {
        Err(String::from_utf8_lossy(&output.stderr).to_string())
    }
}

#[tauri::command]
async fn fetch_savings(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/savings", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

/// Transcribe audio via the speech API endpoint.
#[tauri::command]
async fn transcribe_audio(
    api_url: String,
    audio_data: Vec<u8>,
    filename: String,
) -> Result<serde_json::Value, String> {
    let url = format!("{}/v1/speech/transcribe", api_url);
    let client = reqwest::Client::new();

    let part = reqwest::multipart::Part::bytes(audio_data)
        .file_name(filename)
        .mime_str("audio/webm")
        .map_err(|e| format!("Failed to create multipart: {}", e))?;

    let form = reqwest::multipart::Form::new().part("file", part);

    let resp = client
        .post(&url)
        .multipart(form)
        .send()
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))?;
    Ok(body)
}

/// Submit savings to Supabase leaderboard.
#[tauri::command]
async fn submit_savings(
    supabase_url: String,
    supabase_key: String,
    payload: serde_json::Value,
) -> Result<bool, String> {
    if supabase_url.is_empty() || supabase_key.is_empty() {
        return Ok(false);
    }
    let client = reqwest::Client::new();
    let resp = client
        .post(format!(
            "{}/rest/v1/savings_entries?on_conflict=anon_id",
            supabase_url
        ))
        .header("Content-Type", "application/json")
        .header("apikey", &supabase_key)
        .header("Authorization", format!("Bearer {}", supabase_key))
        .header("Prefer", "resolution=merge-duplicates")
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("Supabase POST failed: {}", e))?;
    Ok(resp.status().is_success())
}

// ---------------------------------------------------------------------------
// Cloud API key management
// ---------------------------------------------------------------------------

/// Path to the cloud keys file (~/.openjarvis/cloud-keys.env).
fn cloud_keys_path() -> std::path::PathBuf {
    let home = home_dir();
    std::path::PathBuf::from(home)
        .join(".openjarvis")
        .join("cloud-keys.env")
}

/// Read cloud keys from disk and return as key=value pairs.
fn read_cloud_keys() -> Vec<(String, String)> {
    let path = cloud_keys_path();
    let mut keys = Vec::new();
    if let Ok(contents) = std::fs::read_to_string(&path) {
        for line in contents.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            if let Some((k, v)) = line.split_once('=') {
                keys.push((k.trim().to_string(), v.trim().to_string()));
            }
        }
    }
    keys
}

/// Save a single cloud API key to the keys file.
#[tauri::command]
async fn save_cloud_key(key_name: String, key_value: String) -> Result<(), String> {
    let path = cloud_keys_path();
    // Ensure directory exists
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    // Read existing keys, update/add the one being saved
    let mut keys: Vec<(String, String)> = read_cloud_keys()
        .into_iter()
        .filter(|(k, _)| k != &key_name)
        .collect();
    if !key_value.is_empty() {
        keys.push((key_name, key_value));
    }

    // Write back
    let content: String = keys
        .iter()
        .map(|(k, v)| format!("{}={}", k, v))
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(&path, content + "\n").map_err(|e| format!("Failed to save key: {}", e))?;

    // Set permissions to owner-only (chmod 600)
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }

    // Tell the running server to hot-reload its cloud engine so the user
    // doesn't need to restart the app after entering an API key.
    let reload_url = format!("http://127.0.0.1:{}/v1/cloud/reload", JARVIS_PORT);
    let _ = reqwest::Client::new()
        .post(&reload_url)
        .timeout(std::time::Duration::from_secs(10))
        .send()
        .await;

    Ok(())
}

/// Get which cloud providers have keys configured (without exposing values).
#[tauri::command]
async fn get_cloud_key_status() -> Result<serde_json::Value, String> {
    let keys = read_cloud_keys();
    let status: Vec<serde_json::Value> = keys
        .iter()
        .map(|(k, v)| serde_json::json!({ "key": k, "set": !v.is_empty() }))
        .collect();
    Ok(serde_json::json!(status))
}

/// Pull a model via Ollama (called from frontend download button).
#[tauri::command]
async fn pull_ollama_model(model_name: String) -> Result<serde_json::Value, String> {
    pull_model(&model_name)
        .await
        .map_err(|e| format!("Failed to pull {}: {}", model_name, e))?;
    Ok(serde_json::json!({"status": "ok", "model": model_name}))
}

/// Delete a model from Ollama.
#[tauri::command]
async fn delete_ollama_model(model_name: String) -> Result<serde_json::Value, String> {
    let url = format!("http://127.0.0.1:{}/api/delete", OLLAMA_PORT);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .delete(&url)
        .json(&serde_json::json!({"name": model_name}))
        .send()
        .await
        .map_err(|e| format!("Delete failed: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("Delete returned status {}", resp.status()));
    }
    Ok(serde_json::json!({"status": "deleted", "model": model_name}))
}

/// Check speech backend health.
#[tauri::command]
async fn speech_health(api_url: String) -> Result<serde_json::Value, String> {
    let url = format!("{}/v1/speech/health", api_url);
    let resp = reqwest::get(&url)
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))?;
    Ok(body)
}

// ---------------------------------------------------------------------------
// Native macOS overlay — NSPanel + WKWebView, entirely bypassing Tauri's
// window management so we get proper always-on-top, transparency, non-
// activating panel behaviour and cross-Space support.
// ---------------------------------------------------------------------------

#[cfg(target_os = "macos")]
mod native_overlay {
    use objc::declare::ClassDecl;
    use objc::runtime::{Class, Object, Sel, BOOL, NO, YES};
    use objc::{class, msg_send, sel, sel_impl};
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Raw pointer to the NSPanel, stored as usize for atomicity.
    static PANEL_PTR: AtomicUsize = AtomicUsize::new(0);
    /// Raw pointer to the WKWebView inside the panel.
    static WEBVIEW_PTR: AtomicUsize = AtomicUsize::new(0);
    /// Raw pointer to the previously-frontmost NSRunningApplication.
    static PREV_APP: AtomicUsize = AtomicUsize::new(0);

    // CoreGraphics geometry types expected by AppKit.
    #[repr(C)]
    #[derive(Copy, Clone)]
    struct CGPoint {
        x: f64,
        y: f64,
    }
    #[repr(C)]
    #[derive(Copy, Clone)]
    struct CGSize {
        width: f64,
        height: f64,
    }
    #[repr(C)]
    #[derive(Copy, Clone)]
    struct CGRect {
        origin: CGPoint,
        size: CGSize,
    }

    /// Create an autoreleased NSString from a Rust &str.
    unsafe fn nsstring(s: &str) -> *mut Object {
        let obj: *mut Object = msg_send![class!(NSString), alloc];
        msg_send![obj,
            initWithBytes: s.as_ptr()
            length: s.len()
            encoding: 4usize  // NSUTF8StringEncoding
        ]
    }

    // ------------------------------------------------------------------
    // Conversation persistence
    // ------------------------------------------------------------------

    fn conversation_path() -> std::path::PathBuf {
        std::path::PathBuf::from(super::home_dir())
            .join(".openjarvis")
            .join("overlay-conversation.json")
    }

    pub fn load_conversation() -> String {
        std::fs::read_to_string(conversation_path()).unwrap_or_else(|_| "[]".into())
    }

    /// Read cloud API keys and return a JSON array of model IDs
    /// whose provider has a key configured.
    fn cloud_models_json() -> String {
        let keys = super::read_cloud_keys();
        let mut models: Vec<&str> = Vec::new();
        for (name, value) in &keys {
            if value.is_empty() {
                continue;
            }
            match name.as_str() {
                "OPENAI_API_KEY" => models.extend(["gpt-4o", "gpt-4o-mini"]),
                "ANTHROPIC_API_KEY" => {
                    models.extend(["claude-sonnet-4-20250514", "claude-haiku-4-20250414"])
                }
                "GEMINI_API_KEY" | "GOOGLE_API_KEY" => {
                    models.extend(["gemini-2.5-flash", "gemini-2.5-pro"])
                }
                _ => {}
            }
        }
        serde_json::to_string(&models).unwrap_or_else(|_| "[]".into())
    }

    fn save_conversation(json: &str) {
        let path = conversation_path();
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::write(&path, json);
    }

    /// Apply every transparency trick to the WKWebView.
    /// Called once at creation and again after the page finishes loading.
    unsafe fn force_transparent(wv: *mut Object) {
        let clear: *mut Object = msg_send![class!(NSColor), clearColor];
        let _: () = msg_send![wv, _setDrawsBackground: NO];
        let no_num: *mut Object = msg_send![class!(NSNumber), numberWithBool: NO];
        let _: () = msg_send![wv, setValue: no_num forKey: nsstring("drawsBackground")];
        let _: () = msg_send![wv, setUnderPageBackgroundColor: clear];
        // Also inject CSS to nuke any remaining background
        let js = nsstring(
            "document.documentElement.style.background='transparent';\
             document.body.style.background='transparent';"
        );
        let nil: *mut Object = std::ptr::null_mut();
        let _: () = msg_send![wv, evaluateJavaScript: js completionHandler: nil];
    }

    // ------------------------------------------------------------------
    // Public API (must be called on the main thread)
    // ------------------------------------------------------------------

    /// Build the native overlay panel.  Call once during app setup.
    pub unsafe fn create(html: &str, api_port: u16) {
        // --- Custom NSPanel subclass that accepts keyboard input ------
        if Class::get("JarvisOverlayPanel").is_none() {
            let sup = Class::get("NSPanel").unwrap();
            let mut decl = ClassDecl::new("JarvisOverlayPanel", sup).unwrap();
            extern "C" fn yes(_: &Object, _: Sel) -> BOOL {
                YES
            }
            decl.add_method(
                sel!(canBecomeKeyWindow),
                yes as extern "C" fn(&Object, Sel) -> BOOL,
            );
            decl.register();
        }

        // --- WKNavigationDelegate — re-apply transparency after load --
        if Class::get("JarvisOverlayNavDelegate").is_none() {
            let sup = Class::get("NSObject").unwrap();
            let mut decl = ClassDecl::new("JarvisOverlayNavDelegate", sup).unwrap();
            extern "C" fn did_finish(_: &Object, _: Sel, wv: *mut Object, _nav: *mut Object) {
                unsafe { force_transparent(wv); }
            }
            decl.add_method(
                sel!(webView:didFinishNavigation:),
                did_finish as extern "C" fn(&Object, Sel, *mut Object, *mut Object),
            );
            decl.register();
        }

        // --- WKScriptMessageHandler so JS can call hide() ------------
        if Class::get("JarvisOverlayMsgHandler").is_none() {
            let sup = Class::get("NSObject").unwrap();
            let mut decl = ClassDecl::new("JarvisOverlayMsgHandler", sup).unwrap();
            extern "C" fn on_msg(_: &Object, _: Sel, _ctrl: *mut Object, msg: *mut Object) {
                unsafe {
                    let body: *mut Object = msg_send![msg, body];
                    if body.is_null() {
                        return;
                    }
                    let c: *const std::os::raw::c_char = msg_send![body, UTF8String];
                    if c.is_null() {
                        return;
                    }
                    if let Ok(s) = std::ffi::CStr::from_ptr(c).to_str() {
                        if s == "hide" {
                            hide();
                        } else if let Some(json) = s.strip_prefix("save:") {
                            save_conversation(json);
                        } else if let Some(coords) = s.strip_prefix("drag:") {
                            drag(coords);
                        }
                    }
                }
            }
            decl.add_method(
                sel!(userContentController:didReceiveScriptMessage:),
                on_msg as extern "C" fn(&Object, Sel, *mut Object, *mut Object),
            );
            decl.register();
        }

        // --- Create the NSPanel --------------------------------------
        let frame = CGRect {
            origin: CGPoint { x: 0.0, y: 0.0 },
            size: CGSize {
                width: 560.0,
                height: 400.0,
            },
        };
        // NSWindowStyleMaskNonactivatingPanel = 1 << 7
        let style: u64 = 1 << 7;

        let cls = Class::get("JarvisOverlayPanel").unwrap();
        let panel: *mut Object = msg_send![cls, alloc];
        let panel: *mut Object = msg_send![panel,
            initWithContentRect: frame
            styleMask: style
            backing: 2u64       // NSBackingStoreBuffered
            defer: NO
        ];

        // Window level — NSFloatingWindowLevel (3).
        let _: () = msg_send![panel, setLevel: 3_i64];
        // canJoinAllSpaces (1) | fullScreenAuxiliary (1<<8)
        let _: () = msg_send![panel, setCollectionBehavior: 257_u64];
        let _: () = msg_send![panel, setHidesOnDeactivate: NO];
        let _: () = msg_send![panel, setOpaque: NO];
        let _: () = msg_send![panel, setHasShadow: NO];
        let _: () = msg_send![panel, setMovableByWindowBackground: YES];

        let clear: *mut Object = msg_send![class!(NSColor), clearColor];
        let _: () = msg_send![panel, setBackgroundColor: clear];
        let _: () = msg_send![panel, center];

        // --- WKWebView -----------------------------------------------
        let cfg: *mut Object = msg_send![class!(WKWebViewConfiguration), alloc];
        let cfg: *mut Object = msg_send![cfg, init];

        // Attach message handler ("overlay" channel)
        let hcls = Class::get("JarvisOverlayMsgHandler").unwrap();
        let handler: *mut Object = msg_send![hcls, alloc];
        let handler: *mut Object = msg_send![handler, init];
        let uc: *mut Object = msg_send![cfg, userContentController];
        let _: () = msg_send![uc,
            addScriptMessageHandler: handler
            name: nsstring("overlay")
        ];

        let wv: *mut Object = msg_send![class!(WKWebView), alloc];
        let wv: *mut Object = msg_send![wv,
            initWithFrame: frame
            configuration: cfg
        ];

        // ---- Make the webview fully transparent ----
        force_transparent(wv);

        // Set navigation delegate so we re-apply after page loads
        let nav_cls = Class::get("JarvisOverlayNavDelegate").unwrap();
        let nav_del: *mut Object = msg_send![nav_cls, alloc];
        let nav_del: *mut Object = msg_send![nav_del, init];
        let _: () = msg_send![wv, setNavigationDelegate: nav_del];

        let _: () = msg_send![panel, setContentView: wv];
        WEBVIEW_PTR.store(wv as usize, Ordering::SeqCst);

        // Inject saved conversation into the HTML template, then load it.
        // Use the API server as the base URL so fetch() is same-origin.
        // Escape "</" so the JSON can't prematurely close the <script> tag.
        // ("\/" is valid JSON — resolves back to "/" when parsed.)
        let saved = load_conversation().replace("</", "<\\/");
        let cloud = cloud_models_json();
        let filled = html
            .replace("__SAVED_MESSAGES__", &saved)
            .replace("__CLOUD_MODELS__", &cloud);
        let base_str = nsstring(&format!("http://127.0.0.1:{}", api_port));
        let base_url: *mut Object = msg_send![class!(NSURL), URLWithString: base_str];
        let _: () = msg_send![wv,
            loadHTMLString: nsstring(&filled)
            baseURL: base_url
        ];

        PANEL_PTR.store(panel as usize, Ordering::SeqCst);
    }

    pub unsafe fn toggle() {
        let ptr = PANEL_PTR.load(Ordering::SeqCst);
        if ptr == 0 {
            return;
        }
        let panel = ptr as *mut Object;
        let vis: BOOL = msg_send![panel, isVisible];
        if vis != NO {
            hide();
        } else {
            show();
        }
    }

    pub unsafe fn show() {
        let ptr = PANEL_PTR.load(Ordering::SeqCst);
        if ptr == 0 {
            return;
        }
        let panel = ptr as *mut Object;

        // Re-apply transparency every time (the webview can reset it)
        let wv_ptr = WEBVIEW_PTR.load(Ordering::SeqCst);
        if wv_ptr != 0 {
            force_transparent(wv_ptr as *mut Object);
        }

        // Remember the currently-frontmost app so we can restore it.
        let ws: *mut Object = msg_send![class!(NSWorkspace), sharedWorkspace];
        let front: *mut Object = msg_send![ws, frontmostApplication];
        if !front.is_null() {
            let _: () = msg_send![front, retain];
            let old = PREV_APP.swap(front as usize, Ordering::SeqCst);
            if old != 0 {
                let _: () = msg_send![(old as *mut Object), release];
            }
        }

        // Activate our process so the panel receives keyboard input.
        let app: *mut Object = msg_send![class!(NSApplication), sharedApplication];
        let _: () = msg_send![app, activateIgnoringOtherApps: YES];
        let nil: *mut Object = std::ptr::null_mut();
        let _: () = msg_send![panel, makeKeyAndOrderFront: nil];

        // Focus the text field inside the webview.
        let wv: *mut Object = msg_send![panel, contentView];
        let js = nsstring("document.getElementById('input').focus()");
        let _: () = msg_send![wv, evaluateJavaScript: js completionHandler: nil];
    }

    /// Move the panel by a screen-space delta (called from JS drag handler).
    unsafe fn drag(coords: &str) {
        let ptr = PANEL_PTR.load(Ordering::SeqCst);
        if ptr == 0 {
            return;
        }
        let panel = ptr as *mut Object;
        let Some((dxs, dys)) = coords.split_once(',') else {
            return;
        };
        let Ok(dx) = dxs.parse::<f64>() else { return };
        let Ok(dy) = dys.parse::<f64>() else { return };
        // NSWindow frame origin is bottom-left; screen Y increases upward,
        // but mouse screenY increases downward, so invert dy.
        let frame: CGRect = msg_send![panel, frame];
        let origin = CGPoint {
            x: frame.origin.x + dx,
            y: frame.origin.y - dy,
        };
        let _: () = msg_send![panel, setFrameOrigin: origin];
    }

    pub unsafe fn hide() {
        let ptr = PANEL_PTR.load(Ordering::SeqCst);
        if ptr == 0 {
            return;
        }
        let panel = ptr as *mut Object;
        let nil: *mut Object = std::ptr::null_mut();
        let _: () = msg_send![panel, orderOut: nil];

        // Give focus back to whatever app was frontmost before.
        let prev = PREV_APP.swap(0, Ordering::SeqCst);
        if prev != 0 {
            let prev_app = prev as *mut Object;
            let _: BOOL = msg_send![prev_app, activateWithOptions: 2_u64];
            let _: () = msg_send![prev_app, release];
        }
    }
}

/// Dispatch a closure onto the main thread via GCD.
#[cfg(target_os = "macos")]
fn on_main_thread(f: impl FnOnce() + Send + 'static) {
    dispatch::Queue::main().exec_async(f);
}

// ---------------------------------------------------------------------------
// Overlay Tauri commands (thin wrappers that dispatch to the main thread)
// ---------------------------------------------------------------------------

#[tauri::command]
async fn get_overlay_conversation() -> Result<String, String> {
    #[cfg(target_os = "macos")]
    {
        return Ok(native_overlay::load_conversation());
    }
    #[cfg(not(target_os = "macos"))]
    Ok("[]".into())
}

#[tauri::command]
async fn toggle_overlay() -> Result<(), String> {
    #[cfg(target_os = "macos")]
    on_main_thread(|| unsafe { native_overlay::toggle() });
    Ok(())
}

#[tauri::command]
async fn hide_overlay() -> Result<(), String> {
    #[cfg(target_os = "macos")]
    on_main_thread(|| unsafe { native_overlay::hide() });
    Ok(())
}

// ---------------------------------------------------------------------------
// App entry point
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let backend: SharedBackend = Arc::new(Mutex::new(BackendManager::default()));
    let status: SharedStatus = Arc::new(Mutex::new(SetupStatus::default()));

    let boot_backend_ref = backend.clone();
    let boot_status_ref = status.clone();

    tauri::Builder::default()
        .manage(backend.clone())
        .manage(status.clone())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--hidden"]),
        ))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_focus();
            }
        }))
        .setup(move |app| {
            // System tray
            let show = MenuItemBuilder::with_id("show", "Show / Hide").build(app)?;
            let health = MenuItemBuilder::with_id("health", "Health: starting...")
                .enabled(false)
                .build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "Quit OpenJarvis").build(app)?;

            let menu = MenuBuilder::new(app)
                .item(&show)
                .separator()
                .item(&health)
                .separator()
                .item(&quit)
                .build()?;

            let _tray = TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("OpenJarvis")
                .menu(&menu)
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            if window.is_visible().unwrap_or(false) {
                                let _ = window.hide();
                            } else {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
                        }
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            // Create native macOS overlay panel
            #[cfg(target_os = "macos")]
            unsafe {
                native_overlay::create(include_str!("overlay.html"), JARVIS_PORT);
            }

            // Register Cmd+Shift+Space to toggle the overlay
            {
                use tauri_plugin_global_shortcut::{
                    Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState,
                };
                let sc = Shortcut::new(Some(Modifiers::META | Modifiers::SHIFT), Code::Space);
                if let Err(e) = app.global_shortcut().on_shortcut(sc, |_app, _sc, ev| {
                    if ev.state == ShortcutState::Pressed {
                        #[cfg(target_os = "macos")]
                        unsafe {
                            native_overlay::toggle();
                        }
                    }
                }) {
                    eprintln!("Warning: could not register Cmd+Shift+Space: {e}");
                }
            }

            // Auto-start backend services on launch
            tauri::async_runtime::spawn(boot_backend(boot_backend_ref, boot_status_ref));

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_setup_status,
            get_api_base,
            start_backend,
            stop_backend,
            stop_jarvis,
            restart_jarvis,
            hard_restart_backend,
            ensure_ngrok_tunnel,
            stop_ngrok_tunnel,
            configure_ngrok_authtoken,
            install_ngrok,
            get_os_location,
            get_ip_location,
            get_jarvis_status,
            check_health,
            fetch_energy,
            fetch_telemetry,
            fetch_traces,
            fetch_trace,
            fetch_learning_stats,
            fetch_learning_policy,
            fetch_memory_stats,
            search_memory,
            fetch_agents,
            fetch_models,
            run_jarvis_command,
            fetch_savings,
            submit_savings,
            transcribe_audio,
            speech_health,
            pull_ollama_model,
            delete_ollama_model,
            save_cloud_key,
            get_cloud_key_status,
            toggle_overlay,
            hide_overlay,
            get_overlay_conversation,
        ])
        .build(tauri::generate_context!())
        .expect("error while building OpenJarvis Desktop")
        .run(move |_app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                let b = backend.clone();
                tauri::async_runtime::spawn(async move {
                    b.lock().await.stop_all().await;
                });
            }
        });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::{format_uv_sync_failure, format_uv_sync_spawn_error, uv_sync_stderr_tail};
    use std::path::Path;

    #[test]
    fn tail_returns_whole_string_when_shorter_than_limit() {
        assert_eq!(uv_sync_stderr_tail("short error", 800), "short error");
    }

    #[test]
    fn tail_keeps_the_end_not_the_beginning() {
        // uv's actionable line is at the end; the spinner noise is at the start.
        let s = format!("{}ACTUAL ERROR HERE", "spinner-noise ".repeat(200));
        let tail = uv_sync_stderr_tail(&s, 40);
        assert!(tail.ends_with("ACTUAL ERROR HERE"), "tail was: {tail:?}");
        assert!(!tail.contains("spinner-noise spinner-noise spinner-noise"));
        assert!(tail.chars().count() <= 40);
    }

    #[test]
    fn tail_trims_surrounding_whitespace() {
        assert_eq!(uv_sync_stderr_tail("  \n padded \n  ", 800), "padded");
    }

    #[test]
    fn tail_never_splits_a_multibyte_codepoint() {
        // Each "é" is 2 bytes / 1 char. A byte-based slice could panic or
        // produce invalid UTF-8; the char-based tail must not.
        let s = "é".repeat(500);
        let tail = uv_sync_stderr_tail(&s, 100);
        assert_eq!(tail.chars().count(), 100);
        assert!(tail.chars().all(|c| c == 'é'));
    }

    #[test]
    fn failure_message_includes_exit_code_and_tail_and_hint() {
        let msg = format_uv_sync_failure(
            Path::new("/home/u/.openjarvis/src"),
            Some(2),
            "error: failed to resolve numpy==2.1.3",
        );
        assert!(msg.contains("exit 2"));
        assert!(msg.contains("/home/u/.openjarvis/src"));
        assert!(msg.contains("failed to resolve numpy==2.1.3"));
        assert!(msg.contains("uv sync --extra server")); // actionable next step
    }

    #[test]
    fn failure_message_renders_missing_exit_code_as_unknown() {
        // Process killed by signal → no exit code. Must not show a misleading -1.
        let msg = format_uv_sync_failure(Path::new("/x"), None, "boom");
        assert!(msg.contains("exit unknown"));
        assert!(!msg.contains("exit -1"));
    }

    #[test]
    fn spawn_error_names_the_binary_and_root() {
        let msg = format_uv_sync_spawn_error(
            Path::new("/repo"),
            "C:\\Users\\me\\.local\\bin\\uv.exe",
            "No such file or directory (os error 2)",
        );
        assert!(msg.contains("C:\\Users\\me\\.local\\bin\\uv.exe"));
        assert!(msg.contains("/repo"));
        assert!(msg.contains("No such file or directory"));
    }
}
