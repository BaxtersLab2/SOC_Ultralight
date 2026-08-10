//! CLI entry point.
//!
//! Emits exactly one JSON object on stdout per invocation, so a caller (SOC /
//! Python) can `subprocess.run(...)` and parse the result. Non-zero exit code
//! signals failure; `ok:false` in the JSON carries the reason.
//!
//! Subcommands:
//!   vi_minimizer version
//!   vi_minimizer self-test
//!   vi_minimizer run <name> [--wait] [--timeout <ms>] -- <cmd> [args...]
//!   vi_minimizer switch <name>
//!   vi_minimizer switch-back
//!   vi_minimizer help

#[cfg(windows)]
use vi_minimizer::{
    desktop::switch_to_default, launch_on_desktop, list_windows, shutdown_desktop, terminate_pid,
    VirtualDesktop, ViError, WaitOutcome,
};

#[cfg(windows)]
fn main() {
    let code = match run() {
        Ok(c) => c,
        Err(e) => {
            println!("{{\"ok\":false,\"error\":\"{}\"}}", jesc(&e.to_string()));
            1
        }
    };
    std::process::exit(code);
}

#[cfg(not(windows))]
fn main() {
    eprintln!("vi_minimizer requires Windows (Win32 desktop objects).");
    std::process::exit(2);
}

#[cfg(windows)]
fn run() -> vi_minimizer::Result<i32> {
    let args: Vec<String> = std::env::args().collect();
    let verb = args.get(1).map(|s| s.as_str()).unwrap_or("help");
    match verb {
        "version" | "--version" | "-V" => {
            println!(
                "{{\"ok\":true,\"action\":\"version\",\"name\":\"vi_minimizer\",\"version\":\"{}\"}}",
                env!("CARGO_PKG_VERSION")
            );
            Ok(0)
        }
        "help" | "--help" | "-h" => {
            usage();
            Ok(0)
        }
        "self-test" => self_test(),
        "switch" => {
            let name = args
                .get(2)
                .ok_or_else(|| ViError::Invalid("usage: switch <name>".into()))?;
            let d = VirtualDesktop::open(name)?;
            d.switch()?;
            println!(
                "{{\"ok\":true,\"action\":\"switch\",\"desktop\":\"{}\"}}",
                jesc(name)
            );
            Ok(0)
        }
        "switch-back" => {
            switch_to_default()?;
            println!("{{\"ok\":true,\"action\":\"switch-back\",\"desktop\":\"Default\"}}");
            Ok(0)
        }
        "list" => {
            let name = args
                .get(2)
                .ok_or_else(|| ViError::Invalid("usage: list <name>".into()))?;
            let windows = list_windows(name)?;
            let mut items = String::new();
            for (i, w) in windows.iter().enumerate() {
                if i > 0 {
                    items.push(',');
                }
                items.push_str(&format!(
                    "{{\"hwnd\":{},\"pid\":{},\"visible\":{},\"title\":\"{}\"}}",
                    w.hwnd,
                    w.pid,
                    w.visible,
                    jesc(&w.title)
                ));
            }
            println!(
                "{{\"ok\":true,\"action\":\"list\",\"desktop\":\"{}\",\"count\":{},\"windows\":[{}]}}",
                jesc(name),
                windows.len(),
                items
            );
            Ok(0)
        }
        "shutdown" => {
            let name = args
                .get(2)
                .ok_or_else(|| ViError::Invalid("usage: shutdown <name>".into()))?;
            let r = shutdown_desktop(name)?;
            let term = r
                .terminated
                .iter()
                .map(|p| p.to_string())
                .collect::<Vec<_>>()
                .join(",");
            let mut failed = String::new();
            for (i, (pid, err)) in r.failed.iter().enumerate() {
                if i > 0 {
                    failed.push(',');
                }
                failed.push_str(&format!("{{\"pid\":{},\"error\":\"{}\"}}", pid, jesc(err)));
            }
            println!(
                "{{\"ok\":true,\"action\":\"shutdown\",\"desktop\":\"{}\",\"terminated\":[{}],\"failed\":[{}]}}",
                jesc(&r.desktop),
                term,
                failed
            );
            Ok(0)
        }
        "kill" => {
            let pid_s = args
                .get(2)
                .ok_or_else(|| ViError::Invalid("usage: kill <pid>".into()))?;
            let pid: u32 = pid_s
                .parse()
                .map_err(|_| ViError::Invalid("pid must be a number".into()))?;
            terminate_pid(pid, 1)?;
            println!("{{\"ok\":true,\"action\":\"kill\",\"pid\":{pid}}}");
            Ok(0)
        }
        "run" => run_cmd(&args),
        "host" => host_cmd(&args),
        other => {
            usage();
            Err(ViError::Invalid(format!("unknown subcommand '{other}'")))
        }
    }
}

/// Persistent desktop keeper. Creates `<name>` and holds the handle open until
/// the parent closes our stdin (EOF), so the desktop survives across many
/// `launch`/`run` calls onto it. This is how SOC keeps a stable isolated
/// instance for a whole session: start `host` as a child, keep it alive, close
/// its stdin to tear the session down.
#[cfg(windows)]
fn host_cmd(args: &[String]) -> vi_minimizer::Result<i32> {
    use std::io::{Read, Write};

    let name = args
        .get(2)
        .ok_or_else(|| ViError::Invalid("usage: host <name> [--shutdown-on-exit]".into()))?
        .clone();
    let shutdown_on_exit = args.iter().skip(3).any(|a| a == "--shutdown-on-exit");

    let desk = VirtualDesktop::create(&name)?;
    println!(
        "{{\"ok\":true,\"action\":\"host\",\"desktop\":\"{}\",\"pid\":{},\"status\":\"holding\"}}",
        jesc(&name),
        std::process::id()
    );
    // The parent watches for this line to know the desktop is ready.
    std::io::stdout().flush().ok();

    // Block until the parent closes our stdin — that is the "release" signal.
    let mut sink = String::new();
    let _ = std::io::stdin().read_to_string(&mut sink);

    if shutdown_on_exit {
        let _ = shutdown_desktop(&name);
    }
    drop(desk);
    println!(
        "{{\"ok\":true,\"action\":\"host\",\"desktop\":\"{}\",\"status\":\"released\"}}",
        jesc(&name)
    );
    Ok(0)
}

#[cfg(windows)]
fn run_cmd(args: &[String]) -> vi_minimizer::Result<i32> {
    // run <name> [--wait] [--timeout <ms>] -- <cmd> [args...]
    let mut idx = 2;
    let name = args
        .get(idx)
        .ok_or_else(|| {
            ViError::Invalid("usage: run <name> [--wait] [--timeout ms] -- <cmd>...".into())
        })?
        .clone();
    idx += 1;

    let mut wait = false;
    let mut timeout_ms: u32 = 30_000;
    let mut cwd: Option<String> = None;
    while idx < args.len() {
        match args[idx].as_str() {
            "--wait" => {
                wait = true;
                idx += 1;
            }
            "--timeout" => {
                let v = args
                    .get(idx + 1)
                    .ok_or_else(|| ViError::Invalid("--timeout needs a value (ms)".into()))?;
                timeout_ms = v
                    .parse()
                    .map_err(|_| ViError::Invalid("--timeout must be milliseconds".into()))?;
                idx += 2;
            }
            "--cwd" => {
                let v = args
                    .get(idx + 1)
                    .ok_or_else(|| ViError::Invalid("--cwd needs a directory".into()))?;
                cwd = Some(v.clone());
                idx += 2;
            }
            "--" => {
                idx += 1;
                break;
            }
            other => {
                return Err(ViError::Invalid(format!(
                    "unexpected argument '{other}' (put the command after '--')"
                )))
            }
        }
    }

    let cmd: Vec<String> = args[idx..].to_vec();
    if cmd.is_empty() {
        return Err(ViError::Invalid("no command after '--'".into()));
    }

    let desk = VirtualDesktop::create(&name)?;
    let child = launch_on_desktop(&name, &cmd, cwd.as_deref())?;
    let pid = child.pid;

    if wait {
        match child.wait(timeout_ms)? {
            WaitOutcome::Exited(code) => {
                println!(
                    "{{\"ok\":true,\"action\":\"run\",\"desktop\":\"{}\",\"pid\":{},\"waited\":true,\"exit_code\":{}}}",
                    jesc(&name), pid, code
                );
            }
            WaitOutcome::Timeout => {
                println!(
                    "{{\"ok\":true,\"action\":\"run\",\"desktop\":\"{}\",\"pid\":{},\"waited\":true,\"timeout\":true}}",
                    jesc(&name), pid
                );
            }
        }
        drop(desk);
        return Ok(0);
    }

    // Fire-and-hold: we exit, but the child keeps the desktop alive. Wait for
    // the child to attach to the desktop first, or releasing our handle would
    // race it into destruction (see LaunchedProcess::wait_for_ready).
    child.wait_for_ready(5_000);
    println!(
        "{{\"ok\":true,\"action\":\"run\",\"desktop\":\"{}\",\"pid\":{},\"waited\":false}}",
        jesc(&name),
        pid
    );
    drop(desk);
    Ok(0)
}

/// Live end-to-end check of the isolation plumbing: create a scratch desktop,
/// run `cmd /c exit 7` on it, and confirm we read back exit code 7.
#[cfg(windows)]
fn self_test() -> vi_minimizer::Result<i32> {
    let name = "vi_selftest";
    let desk = VirtualDesktop::create(name)?;
    let child = launch_on_desktop(
        name,
        &[
            "cmd.exe".to_string(),
            "/c".to_string(),
            "exit".to_string(),
            "7".to_string(),
        ],
        None,
    )?;
    let pid = child.pid;
    let outcome = child.wait(15_000)?;
    drop(child);
    drop(desk);

    match outcome {
        WaitOutcome::Exited(7) => {
            println!(
                "{{\"ok\":true,\"action\":\"self-test\",\"desktop\":\"{name}\",\"pid\":{pid},\"result\":\"PASS\",\"detail\":\"child ran on isolated desktop and returned 7\"}}"
            );
            Ok(0)
        }
        WaitOutcome::Exited(c) => {
            println!(
                "{{\"ok\":false,\"action\":\"self-test\",\"result\":\"FAIL\",\"detail\":\"expected exit 7, got {c}\"}}"
            );
            Ok(1)
        }
        WaitOutcome::Timeout => {
            println!(
                "{{\"ok\":false,\"action\":\"self-test\",\"result\":\"FAIL\",\"detail\":\"child timed out\"}}"
            );
            Ok(1)
        }
    }
}

#[cfg(windows)]
fn usage() {
    eprintln!(
        "vi_minimizer — isolated Win32 virtual-desktop host for SOC Ultralight

USAGE:
    vi_minimizer <SUBCOMMAND>

SUBCOMMANDS:
    version                                 Print version as JSON
    self-test                               Create a scratch desktop, run a child, verify isolation
    run <name> [--wait] [--timeout ms] [--cwd dir] -- <cmd>...
                                            Create desktop <name> and launch <cmd> on it
    host <name> [--shutdown-on-exit]        Create <name> and hold it until stdin closes (session keeper)
    list <name>                             List top-level windows (hwnd/pid/title) on <name>
    shutdown <name>                         Terminate every process with a window on <name>
    kill <pid>                              Terminate a single process by pid
    switch <name>                           Make <name> the active input desktop (peek)
    switch-back                             Return to the operator's Default desktop
    help                                    Show this help

Each invocation prints one JSON object on stdout."
    );
}

/// Minimal JSON string escaper (values in this CLI are short and controlled).
#[cfg(windows)]
fn jesc(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o
}
