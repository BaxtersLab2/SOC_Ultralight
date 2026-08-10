//! Isolated Win32 desktop objects.
//!
//! A "desktop" is a securable object inside a window station. Every thread is
//! bound to one desktop; windows, hooks and synthetic input (`SendInput`,
//! pyautogui) act on the *thread's* desktop. Only one desktop at a time is the
//! **active input desktop** — the one the monitor shows and the physical
//! mouse/keyboard drive.
//!
//! By launching the SOC swarm on a private desktop created here, its clicks and
//! keystrokes land on that desktop instead of the operator's real one. The
//! operator's desktop ("Default") stays interactive and free.
//!
//! Note (see `README.md`): DWM composition and `DwmRegisterThumbnail` only
//! apply to the *active* desktop, so a live thumbnail of a background desktop
//! is a separate, unsolved problem — tackled in a later milestone.

use crate::error::{win_err, Result};
use crate::util::{validate_desktop_name, wide};

use windows::core::PCWSTR;
use windows::Win32::Foundation::FALSE;
use windows::Win32::System::StationsAndDesktops::{
    CloseDesktop, CreateDesktopW, OpenDesktopW, SwitchDesktop, DESKTOP_CONTROL_FLAGS, HDESK,
};

/// Full access to the desktop object (`GENERIC_ALL`).
const DESKTOP_ALL_ACCESS: u32 = 0x1000_0000;

/// The operator's normal interactive desktop.
pub const DEFAULT_DESKTOP: &str = "Default";

/// An owned handle to a Win32 desktop object. Closes the handle on drop.
///
/// Closing our handle does **not** destroy the desktop while a process is still
/// running on it — the OS reference-counts desktops, so a launched child keeps
/// it alive after we let go.
pub struct VirtualDesktop {
    handle: HDESK,
    name: String,
}

impl VirtualDesktop {
    /// Create a brand-new desktop within the current window station.
    pub fn create(name: &str) -> Result<Self> {
        validate_desktop_name(name)?;
        let wname = wide(name);
        let handle = unsafe {
            CreateDesktopW(
                PCWSTR(wname.as_ptr()),
                PCWSTR::null(),
                None,
                DESKTOP_CONTROL_FLAGS(0),
                DESKTOP_ALL_ACCESS,
                None,
            )
        }
        .map_err(|e| win_err("CreateDesktopW", e))?;
        Ok(Self {
            handle,
            name: name.to_string(),
        })
    }

    /// Open an existing desktop by name.
    pub fn open(name: &str) -> Result<Self> {
        validate_desktop_name(name)?;
        let wname = wide(name);
        let handle = unsafe {
            OpenDesktopW(
                PCWSTR(wname.as_ptr()),
                DESKTOP_CONTROL_FLAGS(0),
                FALSE,
                DESKTOP_ALL_ACCESS,
            )
        }
        .map_err(|e| win_err("OpenDesktopW", e))?;
        Ok(Self {
            handle,
            name: name.to_string(),
        })
    }

    /// Make this desktop the active input desktop (what the monitor shows and
    /// the physical keyboard/mouse drive). Requires `DESKTOP_SWITCHDESKTOP`.
    pub fn switch(&self) -> Result<()> {
        unsafe { SwitchDesktop(self.handle) }.map_err(|e| win_err("SwitchDesktop", e))
    }

    /// The desktop's name.
    pub fn name(&self) -> &str {
        &self.name
    }

    /// The raw handle (for later capture / enumeration modules).
    pub fn handle(&self) -> HDESK {
        self.handle
    }
}

impl Drop for VirtualDesktop {
    fn drop(&mut self) {
        // Ignore errors on close: nothing actionable during drop.
        unsafe {
            let _ = CloseDesktop(self.handle);
        }
    }
}

/// Switch the active input desktop back to the operator's "Default" desktop.
pub fn switch_to_default() -> Result<()> {
    let d = VirtualDesktop::open(DEFAULT_DESKTOP)?;
    d.switch()
}
