//! # vi_minimizer
//!
//! Isolated Win32 virtual-desktop host for the SOC Ultralight agent swarm.
//!
//! The SOC orchestrator drives GUI apps with synthetic mouse/keyboard input and
//! screen OCR. Run directly, that commandeers the operator's real desktop. This
//! crate carves out a **private desktop object** (`CreateDesktopW`), launches
//! the swarm onto it, and lets the operator keep using their machine while the
//! agents work in the background — the foundation for turning SOC from an
//! operator-attended tool into a headless service driven by external triggers.
//!
//! ## Status
//! Milestone 1 — desktop isolation core: create / launch-on / switch / destroy,
//! plus a CLI ([`bin/vi_minimizer`]) for SOC to drive via subprocess. The live
//! "thumbnail" of the background desktop is a later milestone (see `README.md`
//! for the DWM/DXGI constraint that makes it non-trivial).
//!
//! ## Example
//! ```no_run
//! use vi_minimizer::{VirtualDesktop, launch_on_desktop};
//! # fn main() -> vi_minimizer::Result<()> {
//! let desk = VirtualDesktop::create("soc_vi")?;
//! let child = launch_on_desktop("soc_vi", &["notepad.exe".to_string()], None)?;
//! println!("launched pid {}", child.pid);
//! # Ok(())
//! # }
//! ```

pub mod error;
pub mod util;

#[cfg(windows)]
pub mod desktop;
#[cfg(windows)]
pub mod enumerate;
#[cfg(windows)]
pub mod process;

pub use error::{Result, ViError};

#[cfg(windows)]
pub use desktop::{switch_to_default, VirtualDesktop, DEFAULT_DESKTOP};
#[cfg(windows)]
pub use enumerate::{list_windows, shutdown_desktop, ShutdownReport, WindowInfo};
#[cfg(windows)]
pub use process::{launch_on_desktop, terminate_pid, LaunchedProcess, WaitOutcome};
