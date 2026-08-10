//! Launch processes onto a named desktop.
//!
//! `CreateProcessW` places the new process on the desktop named in
//! `STARTUPINFO.lpDesktop`; every window it opens is therefore created on that
//! desktop, not the operator's. This is how the SOC swarm is bootstrapped into
//! the isolated instance created by [`crate::desktop`].

use crate::error::{win_err, Result, ViError};
use crate::util::{build_command_line, wide};

use windows::core::{PCWSTR, PWSTR};
use windows::Win32::Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT};
use windows::Win32::System::Threading::{
    CreateProcessW, GetExitCodeProcess, OpenProcess, TerminateProcess, WaitForInputIdle,
    WaitForSingleObject, PROCESS_CREATION_FLAGS, PROCESS_INFORMATION, PROCESS_TERMINATE,
    STARTUPINFOW,
};

/// Result of waiting on a launched process.
pub enum WaitOutcome {
    /// The process exited with this code.
    Exited(u32),
    /// The wait timed out; the process is still running.
    Timeout,
}

/// A process launched onto a desktop. Closes its handles on drop (closing the
/// handles does not terminate the process).
pub struct LaunchedProcess {
    process: HANDLE,
    thread: HANDLE,
    /// The OS process id.
    pub pid: u32,
}

/// Launch `args[0]` (with `args[1..]` as arguments) on the desktop named
/// `desktop`. The desktop must already exist (create it via
/// [`crate::desktop::VirtualDesktop::create`]).
///
/// `cwd` sets the child's working directory (needed for apps that resolve
/// resources relative to it, like SOC's outbox / button database); `None`
/// inherits our own.
pub fn launch_on_desktop(
    desktop: &str,
    args: &[String],
    cwd: Option<&str>,
) -> Result<LaunchedProcess> {
    if args.is_empty() {
        return Err(ViError::Invalid("no command specified".into()));
    }

    // CreateProcessW may write to the command-line buffer, so it must be mutable.
    let mut cmd_wide: Vec<u16> = wide(&build_command_line(args));
    let mut desk_wide: Vec<u16> = wide(desktop);
    let cwd_wide: Option<Vec<u16>> = cwd.map(wide);
    let cwd_ptr = match &cwd_wide {
        Some(w) => PCWSTR(w.as_ptr()),
        None => PCWSTR::null(),
    };

    let mut si = STARTUPINFOW::default();
    si.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
    si.lpDesktop = PWSTR(desk_wide.as_mut_ptr());

    let mut pi = PROCESS_INFORMATION::default();

    unsafe {
        CreateProcessW(
            PCWSTR::null(),
            PWSTR(cmd_wide.as_mut_ptr()),
            None,
            None,
            false,
            PROCESS_CREATION_FLAGS(0),
            None,
            cwd_ptr,
            &si,
            &mut pi,
        )
    }
    .map_err(|e| win_err("CreateProcessW", e))?;

    Ok(LaunchedProcess {
        process: pi.hProcess,
        thread: pi.hThread,
        pid: pi.dwProcessId,
    })
}

impl LaunchedProcess {
    /// Wait up to `timeout_ms` for the process to exit.
    pub fn wait(&self, timeout_ms: u32) -> Result<WaitOutcome> {
        let r = unsafe { WaitForSingleObject(self.process, timeout_ms) };
        if r == WAIT_OBJECT_0 {
            let mut code: u32 = 0;
            unsafe { GetExitCodeProcess(self.process, &mut code) }
                .map_err(|e| win_err("GetExitCodeProcess", e))?;
            Ok(WaitOutcome::Exited(code))
        } else if r == WAIT_TIMEOUT {
            Ok(WaitOutcome::Timeout)
        } else {
            Err(ViError::Win32 {
                context: "WaitForSingleObject".into(),
                code: r.0 as i32,
                message: "unexpected wait result".into(),
            })
        }
    }

    /// Forcibly terminate the process with the given exit code.
    pub fn terminate(&self, exit_code: u32) -> Result<()> {
        unsafe { TerminateProcess(self.process, exit_code) }
            .map_err(|e| win_err("TerminateProcess", e))
    }

    /// Best-effort: block until a GUI child has finished initialising (created
    /// its message queue and connected to its desktop), so the desktop survives
    /// after we release our handle in fire-and-hold mode.
    ///
    /// Without this there is a race: `CreateProcessW` returns before the child
    /// attaches to the desktop, and if we `CloseDesktop` our only handle in that
    /// window the desktop is destroyed and the child then fails to attach.
    /// Non-GUI processes make `WaitForInputIdle` return an error, which we
    /// intentionally ignore (those callers should use [`Self::wait`] instead).
    pub fn wait_for_ready(&self, timeout_ms: u32) {
        unsafe {
            WaitForInputIdle(self.process, timeout_ms);
        }
    }
}

impl Drop for LaunchedProcess {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.process);
            let _ = CloseHandle(self.thread);
        }
    }
}

/// Forcibly terminate a process by pid. Used to tear down swarm members we
/// discovered by enumeration rather than launched ourselves (so we have no
/// [`LaunchedProcess`] handle for them).
pub fn terminate_pid(pid: u32, exit_code: u32) -> Result<()> {
    unsafe {
        let handle = OpenProcess(PROCESS_TERMINATE, false, pid)
            .map_err(|e| win_err("OpenProcess", e))?;
        let result =
            TerminateProcess(handle, exit_code).map_err(|e| win_err("TerminateProcess", e));
        let _ = CloseHandle(handle);
        result
    }
}
