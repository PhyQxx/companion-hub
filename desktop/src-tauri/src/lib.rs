use keyring::Entry;
use serde::{Deserialize, Serialize};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager,
};

const KEYRING_SERVICE: &str = "com.aria.companion.desktop";
const KEYRING_ACCOUNT: &str = "device-access-token";
const CLIENT_CAPABILITIES: [&str; 1] = ["device.ping"];

#[derive(Serialize)]
struct PairResult {
    device_id: String,
    owner_user_id: String,
    name: String,
    alias: Option<String>,
    hub_url: String,
}

#[derive(Serialize)]
struct PairRequest<'a> {
    pairing_code: &'a str,
    name: &'a str,
    alias: Option<&'a str>,
    client_type: &'static str,
    capabilities: &'static [&'static str],
}

#[derive(Deserialize)]
struct PairResponse {
    access_token: String,
    device: DeviceResponse,
}

#[derive(Deserialize)]
struct DeviceResponse {
    id: String,
    owner_user_id: String,
    name: String,
    alias: Option<String>,
}

#[derive(Deserialize)]
struct ApiErrorBody {
    detail: Option<serde_json::Value>,
}

fn keyring_entry() -> Result<Entry, String> {
    Entry::new(KEYRING_SERVICE, KEYRING_ACCOUNT)
        .map_err(|error| format!("无法打开系统凭据库：{error}"))
}

#[tauri::command]
async fn pair_device(
    hub_url: String,
    pairing_code: String,
    name: String,
    alias: Option<String>,
) -> Result<PairResult, String> {
    let normalized_hub = hub_url.trim().trim_end_matches('/').to_string();
    let parsed = reqwest::Url::parse(&normalized_hub)
        .map_err(|_| "Hub 地址不是有效 URL".to_string())?;
    if parsed.scheme() != "http" && parsed.scheme() != "https" {
        return Err("Hub 地址只允许 http:// 或 https://".to_string());
    }
    let endpoint = format!("{normalized_hub}/api/v1/devices/pair");
    let response = reqwest::Client::new()
        .post(endpoint)
        .timeout(std::time::Duration::from_secs(10))
        .json(&PairRequest {
            pairing_code: pairing_code.trim(),
            name: name.trim(),
            alias: alias.as_deref().map(str::trim).filter(|value| !value.is_empty()),
            client_type: "desktop",
            capabilities: &CLIENT_CAPABILITIES,
        })
        .send()
        .await
        .map_err(|error| format!("无法连接 Hub：{error}"))?;
    if !response.status().is_success() {
        let status = response.status();
        let detail = response
            .json::<ApiErrorBody>()
            .await
            .ok()
            .and_then(|body| body.detail)
            .map(|value| value.to_string())
            .unwrap_or_else(|| "配对请求被拒绝".to_string());
        return Err(format!("Hub 返回 {status}：{detail}"));
    }
    let paired = response
        .json::<PairResponse>()
        .await
        .map_err(|error| format!("Hub 配对响应无效：{error}"))?;
    keyring_entry()?
        .set_password(&paired.access_token)
        .map_err(|error| format!("设备已配对，但写入系统凭据库失败：{error}"))?;
    Ok(PairResult {
        device_id: paired.device.id,
        owner_user_id: paired.device.owner_user_id,
        name: paired.device.name,
        alias: paired.device.alias,
        hub_url: normalized_hub,
    })
}

#[tauri::command]
fn load_device_credential() -> Result<Option<String>, String> {
    match keyring_entry()?.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(error) => Err(format!("读取系统凭据库失败：{error}")),
    }
}

#[tauri::command]
fn forget_device_credential() -> Result<(), String> {
    match keyring_entry()?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(error) => Err(format!("清除系统凭据失败：{error}")),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .setup(|app| {
            let show = MenuItem::with_id(app, "show", "显示 Aria Desktop", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            TrayIconBuilder::new()
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .invoke_handler(tauri::generate_handler![
            pair_device,
            load_device_credential,
            forget_device_credential
        ])
        .run(tauri::generate_context!())
        .expect("error while running Aria Desktop");
}
