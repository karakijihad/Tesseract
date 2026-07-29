//! Shell self-update (2026-07-29). The UI and this Rust shell ship inside
//! the installer, so git updates can never deliver them — this module
//! closes that gap: check the private repo's GitHub Releases for a newer
//! installer, download it (sha256-verified against the hash our release
//! notes always carry), and hand off to a silent NSIS upgrade + relaunch.
//!
//! Deliberately NOT tauri-plugin-updater: that plugin wants a public
//! static endpoint serving its own manifest schema. Our releases live in a
//! private repo behind a token, and the plugin forwards auth headers into
//! GitHub's S3 redirect (which rejects them). We already own token
//! handling for git updates; this reuses it, and handles the redirect hop
//! manually so the token never reaches the asset host.
//!
//! Operator-prompted, never silent: `exe_update_apply` only runs from an
//! explicit click, mirroring `update_apply`'s contract.

use std::io::Read;
use std::path::{Path, PathBuf};

use serde::Serialize;
use sha2::{Digest, Sha256};
use tauri::State;

use crate::{repo, shell_log, TesseractHome};

/// GitHub caps a REST asset redirect chain at one hop in practice; a tiny
/// fixed budget guards against a loop without a dependency on redirect
/// policy internals.
const MAX_REDIRECTS: usize = 3;
/// Installer downloads are ~30 MB today; anything past this is wrong.
const MAX_DOWNLOAD_BYTES: u64 = 512 * 1024 * 1024;

#[derive(Serialize, Clone)]
pub struct ExeUpdateStatus {
    pub available: bool,
    /// Latest release version, e.g. "1.0.5" (tag with the `v` stripped).
    pub version: String,
    pub notes: String,
}

/// `https://github.com/{owner}/{repo}.git` → `("{owner}", "{repo}")`.
fn owner_repo(url: &str) -> Option<(String, String)> {
    let rest = url.strip_prefix("https://github.com/")?;
    let rest = rest.strip_suffix(".git").unwrap_or(rest);
    let mut parts = rest.splitn(2, '/');
    let owner = parts.next()?.to_string();
    let repo = parts.next()?.trim_end_matches('/').to_string();
    if owner.is_empty() || repo.is_empty() {
        return None;
    }
    Some((owner, repo))
}

/// `"v1.2.3"` / `"1.2.3"` → `(1, 2, 3)`.
fn parse_semver(tag: &str) -> Option<(u64, u64, u64)> {
    let tag = tag.trim().trim_start_matches('v');
    let mut parts = tag.splitn(3, '.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    // Tolerate a suffix like "3-beta" by taking leading digits only.
    let patch_raw = parts.next()?;
    let digits: String = patch_raw
        .chars()
        .take_while(|c| c.is_ascii_digit())
        .collect();
    let patch = digits.parse().ok()?;
    Some((major, minor, patch))
}

fn is_newer(current: &str, latest: &str) -> bool {
    match (parse_semver(current), parse_semver(latest)) {
        (Some(c), Some(l)) => l > c,
        // Unparseable versions must never trigger an install loop.
        _ => false,
    }
}

/// First hex run of EXACTLY 64 chars in the release notes — our release
/// pipeline always embeds the installer's SHA-256 there. Longer runs are
/// rejected rather than truncated (a truncated hash can't verify anything).
fn parse_sha256(notes: &str) -> Option<String> {
    let bytes = notes.as_bytes();
    let mut start = 0usize;
    let mut i = 0usize;
    // One past the end acts as a terminator so a trailing run is measured.
    while i <= bytes.len() {
        if i < bytes.len() && bytes[i].is_ascii_hexdigit() {
            i += 1;
            continue;
        }
        if i - start == 64 {
            return Some(notes[start..i].to_ascii_lowercase());
        }
        i += 1;
        start = i;
    }
    None
}

struct LatestRelease {
    version: String,
    notes: String,
    asset_api_url: String,
    asset_name: String,
}

fn fetch_latest(home: &Path) -> Result<LatestRelease, String> {
    let (owner, repo_name) =
        owner_repo(&repo::repo_url()).ok_or("release check: unsupported repo URL")?;
    let token = repo::github_token(home)
        .ok_or("release check needs the saved GitHub token — it was not found")?;
    let url = format!("https://api.github.com/repos/{owner}/{repo_name}/releases/latest");

    let resp = ureq::get(&url)
        .set("Authorization", &format!("Bearer {token}"))
        .set("Accept", "application/vnd.github+json")
        .set("User-Agent", "tesseract-shell")
        .call()
        .map_err(|e| scrub(&format!("release check failed: {e}")))?;
    let body: serde_json::Value = resp
        .into_json()
        .map_err(|e| format!("release check: bad response: {e}"))?;

    let tag = body["tag_name"].as_str().unwrap_or_default();
    let version = tag.trim_start_matches('v').to_string();
    if version.is_empty() {
        return Err("release check: latest release has no tag".into());
    }
    let notes = body["body"].as_str().unwrap_or_default().to_string();

    let assets = body["assets"].as_array().cloned().unwrap_or_default();
    let setup = assets
        .iter()
        .find(|a| {
            a["name"]
                .as_str()
                .map(|n| n.ends_with("-setup.exe"))
                .unwrap_or(false)
        })
        .ok_or("release check: latest release has no installer asset")?;
    let asset_api_url = setup["url"]
        .as_str()
        .ok_or("release check: asset has no API url")?
        .to_string();
    let asset_name = setup["name"].as_str().unwrap_or("setup.exe").to_string();

    Ok(LatestRelease {
        version,
        notes,
        asset_api_url,
        asset_name,
    })
}

/// Never let a transport error echo a token back into the UI.
fn scrub(msg: &str) -> String {
    crate::provision::scrub_credentials(msg)
}

/// Downloads a private-repo release asset. GitHub's asset endpoint answers
/// a 302 to a pre-signed asset-host URL; the token goes ONLY to
/// api.github.com and the redirect is followed with a clean request.
///
/// Redirect-following is explicitly DISABLED on the agent so this hop is
/// ours to make. ureq's default agent would auto-follow (and happens to
/// strip auth headers itself), but a security property this central must
/// not ride on an unstated library default that a version bump could
/// change.
fn download_asset(api_url: &str, token: &str, dest: &Path) -> Result<(), String> {
    let agent = ureq::AgentBuilder::new().redirects(0).build();
    let mut url = api_url.to_string();
    let mut authorized = true;
    for _ in 0..MAX_REDIRECTS {
        let mut req = agent
            .get(&url)
            .set("Accept", "application/octet-stream")
            .set("User-Agent", "tesseract-shell");
        if authorized {
            req = req.set("Authorization", &format!("Bearer {token}"));
        }
        let resp = match req.call() {
            Ok(r) => r,
            Err(e) => return Err(scrub(&format!("download failed: {e}"))),
        };
        if (300..400).contains(&resp.status()) {
            url = resp
                .header("Location")
                .ok_or_else(|| format!("download: redirect ({}) without Location", resp.status()))?
                .to_string();
            authorized = false;
            continue;
        }
        let mut reader = resp.into_reader().take(MAX_DOWNLOAD_BYTES);
        let mut out = std::fs::File::create(dest).map_err(|e| format!("download: {e}"))?;
        std::io::copy(&mut reader, &mut out).map_err(|e| format!("download: {e}"))?;
        return Ok(());
    }
    Err("download: too many redirects".into())
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file = std::fs::File::open(path).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut file, &mut hasher).map_err(|e| e.to_string())?;
    let digest = hasher.finalize();
    Ok(digest.iter().map(|b| format!("{b:02x}")).collect())
}

/// The hand-off batch script. A generated `.bat` rather than an inline
/// `cmd /c <string>`: Rust's Windows arg-quoting and cmd.exe's `/C`
/// reparsing disagree once a command line carries multiple quoted paths
/// (the documented `raw_arg` footgun), while a script file is parsed by
/// cmd alone — quoting is ours, byte for byte.
///
/// Sequencing: our own exit path (`RunEvent::Exit` → graceful supervisor
/// stop) can take up to ~30s, and the single-instance plugin means a
/// relaunch while the old process lives would just focus the dying window.
/// So the script POLLS for this PID to disappear (capped at ~120s — a
/// wedged exit still gets an install attempt rather than a silent no-op),
/// then runs the NSIS silent upgrade, relaunches, and deletes itself.
fn handoff_script(pid: u32, setup: &Path, current_exe: &Path) -> String {
    format!(
        "@echo off\r\n\
         set tries=0\r\n\
         :wait\r\n\
         set /a tries+=1\r\n\
         if %tries% gtr 60 goto install\r\n\
         tasklist /FI \"PID eq {pid}\" | find \" {pid} \" >nul\r\n\
         if not errorlevel 1 (\r\n\
         ping -n 3 127.0.0.1 >nul\r\n\
         goto wait\r\n\
         )\r\n\
         :install\r\n\
         \"{setup}\" /S\r\n\
         start \"\" \"{exe}\"\r\n\
         del \"%~f0\"\r\n",
        pid = pid,
        setup = setup.display(),
        exe = current_exe.display()
    )
}

/// Writes the hand-off script, spawns it detached, and exits the app so
/// the installer finds nothing running.
fn spawn_installer_and_exit(app: &tauri::AppHandle, setup: &Path) -> Result<(), String> {
    let current_exe =
        std::env::current_exe().map_err(|e| format!("cannot resolve own path: {e}"))?;
    let script = handoff_script(std::process::id(), setup, &current_exe);
    let bat = std::env::temp_dir().join("tesseract-self-update.bat");
    std::fs::write(&bat, script).map_err(|e| format!("handoff script write failed: {e}"))?;

    let mut cmd = std::process::Command::new("cmd");
    cmd.arg("/c").arg(&bat);
    crate::provision::hide_console(&mut cmd);
    cmd.spawn()
        .map_err(|e| format!("installer spawn failed: {e}"))?;
    shell_log::log("self-update: installer handed off — exiting for upgrade");
    app.exit(0);
    Ok(())
}

#[tauri::command]
pub fn exe_update_check(
    app: tauri::AppHandle,
    home: State<TesseractHome>,
) -> Result<ExeUpdateStatus, String> {
    let latest = fetch_latest(&home.0)?;
    let current = app.package_info().version.to_string();
    Ok(ExeUpdateStatus {
        available: is_newer(&current, &latest.version),
        version: latest.version,
        notes: latest.notes,
    })
}

#[tauri::command]
pub fn exe_update_apply(app: tauri::AppHandle, home: State<TesseractHome>) -> Result<(), String> {
    shell_log::log("exe_update_apply: invoked");
    let latest = fetch_latest(&home.0)?;
    let current = app.package_info().version.to_string();
    if !is_newer(&current, &latest.version) {
        return Err(format!("already on the latest version ({current})"));
    }
    let expected = parse_sha256(&latest.notes)
        .ok_or("release notes carry no SHA-256 — refusing to install an unverifiable installer")?;
    let token = repo::github_token(&home.0)
        .ok_or("self-update needs the saved GitHub token — it was not found")?;

    let dest = std::env::temp_dir().join(&latest.asset_name);
    download_asset(&latest.asset_api_url, &token, &dest)?;
    let actual = sha256_file(&dest)?;
    if actual != expected {
        let _ = std::fs::remove_file(&dest);
        return Err(format!(
            "installer hash mismatch (expected {expected}, got {actual}) — download discarded"
        ));
    }
    shell_log::log(&format!(
        "self-update: verified {} (sha256 {}) — restarting to install",
        latest.asset_name, actual
    ));
    spawn_installer_and_exit(&app, &dest)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owner_repo_parses_the_default_url_shape() {
        assert_eq!(
            owner_repo("https://github.com/karakijihad/Tesseract.git"),
            Some(("karakijihad".into(), "Tesseract".into()))
        );
        assert_eq!(
            owner_repo("https://github.com/a/b"),
            Some(("a".into(), "b".into()))
        );
        assert_eq!(owner_repo("https://example.com/a/b.git"), None);
        assert_eq!(owner_repo("https://github.com/only-owner"), None);
    }

    #[test]
    fn semver_compare_orders_releases() {
        assert!(is_newer("1.0.4", "1.0.5"));
        assert!(is_newer("1.0.4", "1.1.0"));
        assert!(is_newer("1.9.9", "2.0.0"));
        assert!(!is_newer("1.0.5", "1.0.5"));
        assert!(!is_newer("1.0.5", "1.0.4"));
        // Unparseable → never "newer": no install loops on garbage tags.
        assert!(!is_newer("1.0.4", "nightly"));
        assert!(!is_newer("garbage", "1.0.5"));
    }

    #[test]
    fn parse_semver_tolerates_v_prefix_and_suffix() {
        assert_eq!(parse_semver("v1.2.3"), Some((1, 2, 3)));
        assert_eq!(parse_semver("1.2.3-beta"), Some((1, 2, 3)));
        assert_eq!(parse_semver("1.2"), None);
    }

    #[test]
    fn sha256_is_found_in_release_notes() {
        let notes = "Fixes things.\n\nSHA-256 (TESSERACT_1.0.4_x64-setup.exe): \
                     6a516f9e746faa147455d6aa945fc110e6681e3a1b6f16b76a5fa33207eb66c6";
        assert_eq!(
            parse_sha256(notes).as_deref(),
            Some("6a516f9e746faa147455d6aa945fc110e6681e3a1b6f16b76a5fa33207eb66c6")
        );
        assert_eq!(parse_sha256("no hash here"), None);
        // 63 hex chars — not a sha256.
        assert_eq!(
            parse_sha256("6a516f9e746faa147455d6aa945fc110e6681e3a1b6f16b76a5fa33207eb66c"),
            None
        );
    }

    #[test]
    fn handoff_script_quotes_paths_and_waits_for_the_pid() {
        let script = handoff_script(
            4242,
            Path::new(r"C:\Temp Dir\TESSERACT_1.0.5_x64-setup.exe"),
            Path::new(r"C:\App Dir\TESSERACT\TESSERACT.exe"),
        );
        // Paths with spaces stay wrapped in plain double quotes — no
        // backslash-escaping (this is cmd.exe, not a CRT argv parser).
        assert!(script.contains("\"C:\\Temp Dir\\TESSERACT_1.0.5_x64-setup.exe\" /S"));
        assert!(script.contains("start \"\" \"C:\\App Dir\\TESSERACT\\TESSERACT.exe\""));
        // No CRT-style escaped quotes anywhere — cmd would read them literally.
        assert!(!script.contains("\\\""));
        // Waits for OUR pid, bounded, and cleans up after itself.
        assert!(script.contains("PID eq 4242"));
        assert!(script.contains("find \" 4242 \""));
        assert!(script.contains("if %tries% gtr 60 goto install"));
        assert!(script.contains("del \"%~f0\""));
    }

    #[test]
    fn sha256_file_digest_matches_known_vector() {
        let dir = crate::test_support::TempDir::new("sha-check");
        let p = dir.path().join("f.bin");
        std::fs::write(&p, b"abc").unwrap();
        assert_eq!(
            sha256_file(&p).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
