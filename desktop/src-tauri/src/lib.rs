use keyring::Entry;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::Command;
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Emitter, Manager,
};

const KEYRING_SERVICE: &str = "com.aria.companion.desktop";
const KEYRING_ACCOUNT: &str = "device-access-token";
const KEYRING_HUB_ACCOUNT: &str = "device-hub-url";
const CLIENT_CAPABILITIES: [&str; 3] = ["device.ping", "notification.show", "avatar.render"];

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

#[derive(Serialize)]
struct ScreenPermissionStatus {
    supported: bool,
    granted: bool,
}

#[derive(Serialize)]
struct ScreenCaptureEnvironment {
    supported: bool,
    granted: bool,
    locked: bool,
}

#[derive(Deserialize, Serialize)]
struct DeviceAssetUpload {
    asset_id: String,
    command_id: String,
    media_type: String,
    bytes: usize,
    sha256: String,
    expires_at: String,
}

/// 截图命令失败的结构化错误码，Desktop 前端原样作为 command.result 的 reason_code 回传 Hub。
#[derive(Serialize)]
struct CaptureError {
    code: &'static str,
    message: String,
}

impl CaptureError {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

/// 交互式选择器等待用户完成框选/选窗的最长时间；超时后终止截图进程并回传 picker_timeout。
const PICKER_TIMEOUT_SECS: u64 = 100;

struct TemporaryCapture(PathBuf);

impl Drop for TemporaryCapture {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

fn keyring_entry() -> Result<Entry, String> {
    Entry::new(KEYRING_SERVICE, KEYRING_ACCOUNT)
        .map_err(|error| format!("无法打开系统凭据库：{error}"))
}

fn hub_keyring_entry() -> Result<Entry, String> {
    Entry::new(KEYRING_SERVICE, KEYRING_HUB_ACCOUNT)
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
    if let Err(error) = hub_keyring_entry()?.set_password(&normalized_hub) {
        let _ = keyring_entry()?.delete_credential();
        return Err(format!("设备已配对，但写入 Hub 绑定失败：{error}"));
    }
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
    for entry in [keyring_entry()?, hub_keyring_entry()?] {
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => {}
            Err(error) => return Err(format!("清除系统凭据失败：{error}")),
        }
    }
    Ok(())
}

#[tauri::command]
fn screen_capture_permission(request: bool) -> ScreenPermissionStatus {
    screen_permission_status(request)
}

#[tauri::command]
fn screen_capture_environment() -> ScreenCaptureEnvironment {
    let permission = screen_permission_status(false);
    ScreenCaptureEnvironment {
        supported: permission.supported,
        granted: permission.granted,
        locked: screen_is_locked(),
    }
}

#[tauri::command]
fn show_notification(title: String, body: String) -> Result<(), String> {
    if title.trim().is_empty() || title.chars().count() > 80 {
        return Err("通知标题无效".to_string());
    }
    if body.trim().is_empty() || body.chars().count() > 1000 {
        return Err("通知正文无效".to_string());
    }
    if screen_is_locked() {
        return Err("设备已锁屏，拒绝显示主动通知".to_string());
    }
    show_native_notification(title.trim(), body.trim())
}

#[cfg(target_os = "macos")]
fn show_native_notification(title: &str, body: &str) -> Result<(), String> {
    let status = Command::new("/usr/bin/osascript")
        .args([
            "-e",
            "on run argv",
            "-e",
            "set notificationBody to item 1 of argv",
            "-e",
            "set notificationTitle to item 2 of argv",
            "-e",
            "display notification notificationBody with title notificationTitle",
            "-e",
            "end run",
            "--",
            body,
            title,
        ])
        .status()
        .map_err(|error| format!("无法调用系统通知：{error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err("系统通知命令执行失败".to_string())
    }
}

#[cfg(not(target_os = "macos"))]
fn show_native_notification(_title: &str, _body: &str) -> Result<(), String> {
    Err("当前平台暂不支持系统通知".to_string())
}

#[tauri::command]
async fn capture_and_upload(
    command_id: String,
    target: String,
    display_index: Option<u8>,
) -> Result<DeviceAssetUpload, CaptureError> {
    if !screen_permission_status(false).granted {
        return Err(CaptureError::new(
            "screen_capture_not_granted",
            "尚未获得屏幕录制权限",
        ));
    }
    if screen_is_locked() {
        return Err(CaptureError::new("screen_locked", "设备已锁屏，拒绝截图"));
    }
    let interactive = target == "interactive";
    let mut capture_args: Vec<String> = Vec::new();
    match (target.as_str(), display_index) {
        ("main_display", None) => capture_args.push("-m".to_string()),
        ("display", Some(index @ 1..=32)) => capture_args.push(format!("-D{index}")),
        ("active_window", None) => {
            let window_id = active_window_id()
                .map_err(|error| CaptureError::new("active_window_unavailable", error))?;
            capture_args.push("-o".to_string());
            capture_args.push(format!("-l{window_id}"));
        }
        // 交互式选择器：用户当场框选区域，空格切换选窗，Esc 取消。
        ("interactive", None) => {
            capture_args.push("-i".to_string());
            capture_args.push("-o".to_string());
        }
        _ => {
            return Err(CaptureError::new(
                "invalid_capture_target",
                "截图目标参数无效",
            ))
        }
    }
    let normalized_hub = hub_keyring_entry()
        .map_err(|error| CaptureError::new("credential_store_unavailable", error))?
        .get_password()
        .map_err(|error| {
            CaptureError::new("credential_store_unavailable", format!("读取 Hub 绑定失败：{error}"))
        })?;
    let parsed = reqwest::Url::parse(&normalized_hub)
        .map_err(|_| CaptureError::new("hub_url_invalid", "Hub 地址不是有效 URL".to_string()))?;
    if parsed.scheme() != "http" && parsed.scheme() != "https" {
        return Err(CaptureError::new(
            "hub_url_invalid",
            "Hub 地址只允许 http:// 或 https://",
        ));
    }
    let capture = TemporaryCapture(std::env::temp_dir().join(format!(
        "aria-screen-{}.png",
        uuid::Uuid::new_v4()
    )));
    let capture_path = capture.0.clone();
    let timed_out_code: &'static str = if interactive {
        "picker_timeout"
    } else {
        "capture_timeout"
    };
    let outcome = tauri::async_runtime::spawn_blocking(move || {
        let mut child = Command::new("/usr/sbin/screencapture")
            .arg("-x")
            .args(capture_args)
            .arg("-tpng")
            .arg(&capture_path)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .map_err(|error| {
                CaptureError::new("capture_failed", format!("无法启动系统截图工具：{error}"))
            })?;
        let deadline =
            std::time::Instant::now() + std::time::Duration::from_secs(PICKER_TIMEOUT_SECS);
        loop {
            match child.try_wait() {
                Ok(Some(status)) => return Ok(Some(status)),
                Ok(None) => {
                    if std::time::Instant::now() >= deadline {
                        let _ = child.kill();
                        let _ = child.wait();
                        return Ok(None);
                    }
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
                Err(error) => {
                    return Err(CaptureError::new(
                        "capture_failed",
                        format!("等待截图进程失败：{error}"),
                    ))
                }
            }
        }
    })
    .await
    .map_err(|error| CaptureError::new("capture_failed", format!("截图任务失败：{error}")))?
    .map_err(|error| error)?;
    let status = match outcome {
        Some(status) => status,
        None => {
            return Err(CaptureError::new(
                timed_out_code,
                if interactive {
                    "等待用户完成屏幕选择超时"
                } else {
                    "系统截图超时"
                },
            ))
        }
    };
    // 交互模式下用户按 Esc 取消时不会写出任何文件；以此与真正的截图失败区分。
    if interactive && !capture.0.exists() {
        return Err(CaptureError::new("picker_cancelled", "用户已取消选择"));
    }
    if !status.success() {
        return Err(CaptureError::new(
            "capture_failed",
            "系统截图失败；请检查屏幕录制权限",
        ));
    }
    let bytes = std::fs::read(&capture.0)
        .map_err(|error| CaptureError::new("capture_read_failed", format!("读取截图失败：{error}")))?;
    if bytes.len() > 8 * 1024 * 1024 {
        return Err(CaptureError::new("capture_too_large", "截图超过 8 MiB 上限"));
    }
    let access_token = load_device_credential()
        .map_err(|error| CaptureError::new("credential_store_unavailable", error))?
        .ok_or_else(|| CaptureError::new("credential_missing", "系统凭据库中没有设备凭据"))?;
    let endpoint = format!("{normalized_hub}/api/v1/devices/commands/{command_id}/asset");
    let response = reqwest::Client::new()
        .post(endpoint)
        .timeout(std::time::Duration::from_secs(20))
        .bearer_auth(access_token)
        .header(reqwest::header::CONTENT_TYPE, "image/png")
        .body(bytes)
        .send()
        .await
        .map_err(|error| CaptureError::new("upload_failed", format!("上传截图失败：{error}")))?;
    if !response.status().is_success() {
        return Err(CaptureError::new(
            "upload_failed",
            format!("Hub 拒绝截图上传：{}", response.status()),
        ));
    }
    response
        .json::<DeviceAssetUpload>()
        .await
        .map_err(|error| CaptureError::new("upload_failed", format!("Hub 截图响应无效：{error}")))
}

#[cfg(target_os = "macos")]
fn active_window_id() -> Result<u32, String> {
    use std::ffi::{c_char, c_void, CString};

    type ObjcObject = *mut c_void;
    type ObjcSelector = *mut c_void;

    #[link(name = "objc")]
    extern "C" {
        fn objc_getClass(name: *const c_char) -> ObjcObject;
        fn sel_registerName(name: *const c_char) -> ObjcSelector;
        #[link_name = "objc_msgSend"]
        fn objc_msg_send_object(receiver: ObjcObject, selector: ObjcSelector) -> ObjcObject;
    }
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGWindowListCopyWindowInfo(option: u32, relative_to_window: u32) -> *const c_void;
        static kCGWindowLayer: *const c_void;
        static kCGWindowNumber: *const c_void;
        static kCGWindowOwnerPID: *const c_void;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFArrayGetCount(array: *const c_void) -> isize;
        fn CFArrayGetValueAtIndex(array: *const c_void, index: isize) -> *const c_void;
        fn CFDictionaryGetValue(dictionary: *const c_void, key: *const c_void) -> *const c_void;
        fn CFNumberGetValue(number: *const c_void, number_type: i32, value: *mut c_void) -> bool;
        fn CFRelease(value: *const c_void);
    }

    fn selector(name: &str) -> Result<ObjcSelector, String> {
        let name = CString::new(name).map_err(|_| "Objective-C selector 无效".to_string())?;
        let value = unsafe { sel_registerName(name.as_ptr()) };
        if value.is_null() {
            Err("无法创建 Objective-C selector".to_string())
        } else {
            Ok(value)
        }
    }

    unsafe fn dictionary_i32(
        dictionary: *const c_void,
        key: *const c_void,
    ) -> Option<i32> {
        const CF_NUMBER_SINT32_TYPE: i32 = 3;
        let number = CFDictionaryGetValue(dictionary, key);
        if number.is_null() {
            return None;
        }
        let mut value = 0_i32;
        CFNumberGetValue(
            number,
            CF_NUMBER_SINT32_TYPE,
            (&mut value as *mut i32).cast::<c_void>(),
        )
        .then_some(value)
    }

    let workspace_class_name = CString::new("NSWorkspace").expect("static class name is valid");
    let workspace_class = unsafe { objc_getClass(workspace_class_name.as_ptr()) };
    if workspace_class.is_null() {
        return Err("无法读取 macOS 前台应用".to_string());
    }
    let workspace = unsafe { objc_msg_send_object(workspace_class, selector("sharedWorkspace")?) };
    let application = unsafe { objc_msg_send_object(workspace, selector("frontmostApplication")?) };
    if application.is_null() {
        return Err("当前没有可截取的前台应用".to_string());
    }
    let send_pid: unsafe extern "C" fn(ObjcObject, ObjcSelector) -> i32 = unsafe {
        std::mem::transmute(
            objc_msg_send_object as unsafe extern "C" fn(ObjcObject, ObjcSelector) -> ObjcObject,
        )
    };
    let frontmost_pid = unsafe { send_pid(application, selector("processIdentifier")?) };
    if frontmost_pid <= 0 {
        return Err("前台应用进程无效".to_string());
    }

    const WINDOW_LIST_ON_SCREEN_ONLY: u32 = 1;
    const WINDOW_LIST_EXCLUDE_DESKTOP_ELEMENTS: u32 = 1 << 4;
    let windows = unsafe {
        CGWindowListCopyWindowInfo(
            WINDOW_LIST_ON_SCREEN_ONLY | WINDOW_LIST_EXCLUDE_DESKTOP_ELEMENTS,
            0,
        )
    };
    if windows.is_null() {
        return Err("无法读取 macOS 窗口列表".to_string());
    }
    let count = unsafe { CFArrayGetCount(windows) };
    let mut matched = None;
    for index in 0..count {
        let dictionary = unsafe { CFArrayGetValueAtIndex(windows, index) };
        if dictionary.is_null() {
            continue;
        }
        let owner_pid = unsafe { dictionary_i32(dictionary, kCGWindowOwnerPID) };
        let layer = unsafe { dictionary_i32(dictionary, kCGWindowLayer) };
        let window_id = unsafe { dictionary_i32(dictionary, kCGWindowNumber) };
        if owner_pid == Some(frontmost_pid) && layer == Some(0) {
            if let Some(value) = window_id.filter(|value| *value > 0) {
                matched = u32::try_from(value).ok();
                if matched.is_some() {
                    break;
                }
            }
        }
    }
    unsafe { CFRelease(windows) };
    matched.ok_or_else(|| "前台应用没有可截取的活动窗口".to_string())
}

#[cfg(not(target_os = "macos"))]
fn active_window_id() -> Result<u32, String> {
    Err("当前平台不支持活动窗口截图".to_string())
}

#[cfg(target_os = "macos")]
fn screen_permission_status(request: bool) -> ScreenPermissionStatus {
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGPreflightScreenCaptureAccess() -> bool;
        fn CGRequestScreenCaptureAccess() -> bool;
    }
    let granted = unsafe {
        if request {
            CGRequestScreenCaptureAccess()
        } else {
            CGPreflightScreenCaptureAccess()
        }
    };
    ScreenPermissionStatus {
        supported: true,
        granted,
    }
}

#[cfg(target_os = "macos")]
fn screen_is_locked() -> bool {
    use std::ffi::{c_char, c_void, CString};

    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn CGSessionCopyCurrentDictionary() -> *const c_void;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFStringCreateWithCString(
            allocator: *const c_void,
            value: *const c_char,
            encoding: u32,
        ) -> *const c_void;
        fn CFDictionaryGetValue(dictionary: *const c_void, key: *const c_void) -> *const c_void;
        fn CFBooleanGetValue(value: *const c_void) -> bool;
        fn CFRelease(value: *const c_void);
    }

    const UTF8_ENCODING: u32 = 0x0800_0100;
    unsafe {
        let dictionary = CGSessionCopyCurrentDictionary();
        if dictionary.is_null() {
            return true;
        }
        let key_name = CString::new("CGSSessionScreenIsLocked").expect("static key is valid");
        let key = CFStringCreateWithCString(std::ptr::null(), key_name.as_ptr(), UTF8_ENCODING);
        if key.is_null() {
            CFRelease(dictionary);
            return true;
        }
        let value = CFDictionaryGetValue(dictionary, key);
        let locked = !value.is_null() && CFBooleanGetValue(value);
        CFRelease(key);
        CFRelease(dictionary);
        locked
    }
}

#[cfg(not(target_os = "macos"))]
fn screen_permission_status(_request: bool) -> ScreenPermissionStatus {
    ScreenPermissionStatus {
        supported: false,
        granted: false,
    }
}

#[cfg(not(target_os = "macos"))]
fn screen_is_locked() -> bool {
    true
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
            let show_pet = MenuItem::with_id(app, "show_pet", "显示桌宠", true, None::<&str>)?;
            let hide_pet = MenuItem::with_id(app, "hide_pet", "隐藏桌宠", true, None::<&str>)?;
            let interact_pet =
                MenuItem::with_id(app, "interact_pet", "恢复桌宠交互", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(
                app,
                &[&show, &show_pet, &hide_pet, &interact_pet, &quit],
            )?;
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
                    "show_pet" => {
                        if let Some(window) = app.get_webview_window("pet") {
                            let _ = window.show();
                            let _ = app.emit("pet-ensure-visible", ());
                            let _ = app.emit("pet-visibility-changed", true);
                        }
                    }
                    "hide_pet" => {
                        if let Some(window) = app.get_webview_window("pet") {
                            let _ = window.hide();
                            let _ = app.emit("pet-visibility-changed", false);
                        }
                    }
                    "interact_pet" => {
                        if let Some(window) = app.get_webview_window("pet") {
                            let _ = window.set_ignore_cursor_events(false);
                            let _ = window.show();
                            let _ = app.emit("pet-ensure-visible", ());
                            let _ = app.emit("pet-interaction-restored", ());
                            let _ = app.emit("pet-visibility-changed", true);
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
            forget_device_credential,
            screen_capture_permission,
            screen_capture_environment,
            show_notification,
            capture_and_upload
        ])
        .run(tauri::generate_context!())
        .expect("error while running Aria Desktop");
}
