//! Enumerate the windows (and owning processes) on a desktop, and tear a
//! desktop's processes down.
//!
//! `EnumDesktopWindows` walks the top-level windows of a desktop — including a
//! *background* one — which is how SOC health-checks its swarm and how
//! [`shutdown_desktop`] finds the processes to kill when a job is done.

use std::collections::BTreeSet;

use crate::desktop::{VirtualDesktop, DEFAULT_DESKTOP};
use crate::error::{Result, ViError};
use crate::process::terminate_pid;

use windows::Win32::Foundation::{BOOL, HWND, LPARAM, TRUE};
use windows::Win32::System::StationsAndDesktops::EnumDesktopWindows;
use windows::Win32::UI::WindowsAndMessaging::{
    GetWindowTextW, GetWindowThreadProcessId, IsWindowVisible,
};

/// A top-level window found on a desktop.
#[derive(Debug, Clone)]
pub struct WindowInfo {
    /// Raw HWND value (for JSON / cross-process reference).
    pub hwnd: isize,
    /// Owning process id.
    pub pid: u32,
    /// Window title (may be empty).
    pub title: String,
    /// Whether the window is currently visible.
    pub visible: bool,
}

/// List the top-level windows on the named desktop.
pub fn list_windows(desktop: &str) -> Result<Vec<WindowInfo>> {
    let d = VirtualDesktop::open(desktop)?;
    let mut out: Vec<WindowInfo> = Vec::new();
    let ptr = &mut out as *mut Vec<WindowInfo> as isize;

    // The desktop handle was already validated by `open()` above, so we trust
    // whatever the callback collected. We deliberately ignore the return value:
    // enumerating a desktop with no top-level windows returns FALSE with a
    // spurious ERROR_SEM_NOT_FOUND, which is not a real failure.
    let _ = unsafe { EnumDesktopWindows(d.handle(), Some(enum_proc), LPARAM(ptr)) };
    Ok(out)
}

unsafe extern "system" fn enum_proc(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let out = &mut *(lparam.0 as *mut Vec<WindowInfo>);

    let mut pid: u32 = 0;
    GetWindowThreadProcessId(hwnd, Some(&mut pid));

    let mut buf = [0u16; 512];
    let len = GetWindowTextW(hwnd, &mut buf);
    let title = if len > 0 {
        String::from_utf16_lossy(&buf[..len as usize])
    } else {
        String::new()
    };

    let visible = IsWindowVisible(hwnd).as_bool();

    out.push(WindowInfo {
        hwnd: hwnd.0 as isize,
        pid,
        title,
        visible,
    });

    TRUE
}

/// Summary of a [`shutdown_desktop`] call.
#[derive(Debug, Default)]
pub struct ShutdownReport {
    pub desktop: String,
    /// Pids we successfully terminated.
    pub terminated: Vec<u32>,
    /// Pids we failed to terminate, with the reason.
    pub failed: Vec<(u32, String)>,
}

/// Terminate every process that owns a top-level window on `desktop`.
///
/// Refuses the operator's `Default` desktop as a safety rail — this must never
/// be pointed at the real desktop and nuke the operator's own apps.
pub fn shutdown_desktop(desktop: &str) -> Result<ShutdownReport> {
    if desktop.eq_ignore_ascii_case(DEFAULT_DESKTOP) {
        return Err(ViError::Invalid(
            "refusing to shut down the Default (operator) desktop".into(),
        ));
    }

    let windows = list_windows(desktop)?;
    let mut pids: BTreeSet<u32> = BTreeSet::new();
    for w in &windows {
        if w.pid != 0 {
            pids.insert(w.pid);
        }
    }

    let mut report = ShutdownReport {
        desktop: desktop.to_string(),
        ..Default::default()
    };
    for pid in pids {
        match terminate_pid(pid, 1) {
            Ok(()) => report.terminated.push(pid),
            Err(e) => report.failed.push((pid, e.to_string())),
        }
    }
    Ok(report)
}
