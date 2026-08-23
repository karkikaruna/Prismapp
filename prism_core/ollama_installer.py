"""OllamaInstaller - detect, install, and start Ollama on the local device.

Cross-platform (Windows / macOS / Linux) helper used by the startup screen
so a user with a completely clean machine never has to leave the app to get
running. Pure Python + stdlib (+ ``requests``, already a hard dependency) -
no Qt here; the GUI wraps :func:`ensure_ollama` in a worker thread and turns
its ``InstallProgress`` callbacks into Qt signals.

Design:
  - ``detect()`` distinguishes three states: RUNNING (the service answers on
    the configured base URL), INSTALLED_NOT_RUNNING (the ``ollama`` binary
    is on PATH / in a known install location but the server isn't up yet),
    and NOT_INSTALLED.
  - ``install()`` uses the officially supported install path for each OS:
      * Linux  -> the official install script (``curl -fsSL https://ollama.com/install.sh | sh``),
        the same one-liner ollama.com documents; always installs the latest
        stable release.
      * macOS  -> Homebrew (``brew install ollama``) if Homebrew is present,
        otherwise falls back to downloading the official .zip app bundle
        from ollama.com and unpacking it into /Applications - no
        Xcode/build toolchain required either way.
      * Windows -> ``winget install --id Ollama.Ollama`` if winget is
        available (default on Windows 10 21H2+/11), otherwise downloads the
        official ``OllamaSetup.exe`` and runs it silently.
  - ``start()`` launches the local server (``ollama serve``, or the platform
    app bundle) as a detached background process and polls ``/api/tags``
    until it answers or a timeout elapses.
  - Every step raises :class:`InstallerError` with a human-readable message
    on failure instead of letting a raw ``subprocess``/network exception
    escape - the GUI shows that message verbatim in a blocking error dialog.
"""
from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

from prism_core import config, ollama

INSTALL_SCRIPT_URL = "https://ollama.com/install.sh"
WINDOWS_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
MACOS_APP_ZIP_URL = "https://ollama.com/download/Ollama-darwin.zip"

START_POLL_TIMEOUT_S = 45
START_POLL_INTERVAL_S = 1.0
INSTALL_SUBPROCESS_TIMEOUT_S = 900  # 15 min - large installer/first-run download


class InstallerError(RuntimeError):
    """Any failure detecting, installing, or starting Ollama on this device.

    ``.detail`` carries the raw underlying error (subprocess stderr, HTTP
    exception, etc.) for logs; the exception's string form is always a
    plain-language sentence safe to show directly in a dialog.
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class InstallProgress:
    stage: str     # "detecting" | "downloading" | "installing" | "starting" | "done"
    message: str
    # 0-100 when a concrete download/step percentage is known; None when the
    # stage's duration can't be estimated (e.g. running the install script)
    # so the GUI should show an indeterminate/busy progress bar instead.
    percent: Optional[float] = None


ProgressCB = Optional[Callable[[InstallProgress], None]]

# Called (with no arguments, from whatever thread the installer is running
# on) only if the Linux install actually needs an administrator password -
# i.e. the user isn't root and passwordless sudo isn't already configured.
# Should return the password string, or None/"" if the user cancelled.
SudoPasswordProvider = Optional[Callable[[], Optional[str]]]


def _emit(cb: ProgressCB, stage: str, message: str, percent: Optional[float] = None) -> None:
    if cb is not None:
        cb(InstallProgress(stage, message, percent))


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def is_running(*, timeout: float = 3.0) -> bool:
    return ollama.is_available(timeout=timeout)


def find_binary() -> Optional[str]:
    """Locate the ``ollama`` executable, checking PATH first, then the
    common per-OS install locations a fresh install/session might not
    have on PATH yet (e.g. right after a Homebrew or winget install,
    before the shell has re-sourced its environment)."""
    on_path = shutil.which("ollama")
    if on_path:
        return on_path

    system = platform.system()
    candidates: list[Path] = []
    if system == "Darwin":
        candidates += [
            Path("/usr/local/bin/ollama"),
            Path("/opt/homebrew/bin/ollama"),
            Path("/Applications/Ollama.app/Contents/Resources/ollama"),
        ]
    elif system == "Linux":
        candidates += [
            Path("/usr/local/bin/ollama"),
            Path("/usr/bin/ollama"),
            Path("/snap/bin/ollama"),
            Path.home() / ".local" / "bin" / "ollama",
            Path.home() / "bin" / "ollama",
        ]
    elif system == "Windows":
        local = Path.home() / "AppData" / "Local" / "Programs" / "Ollama"
        candidates += [local / "ollama.exe"]

    for path in candidates:
        if path.exists():
            return str(path)
    return None


def is_installed() -> bool:
    return find_binary() is not None


@dataclass(frozen=True)
class DetectResult:
    running: bool
    installed: bool
    binary_path: Optional[str]

    @property
    def needs_install(self) -> bool:
        return not self.installed

    @property
    def needs_start(self) -> bool:
        return self.installed and not self.running


def detect() -> DetectResult:
    running = is_running()
    if running:
        return DetectResult(True, True, find_binary())
    binary = find_binary()
    return DetectResult(False, binary is not None, binary)


# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------

def _run(cmd: list[str], *, timeout: float, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, cwd=cwd, timeout=timeout,
            capture_output=True, text=True,
        )
    except FileNotFoundError as exc:
        raise InstallerError(f"Couldn't run {cmd[0]!r} - it isn't on this device.", str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallerError(
            f"{cmd[0]} took too long to finish (over {int(timeout // 60)} minutes).",
            str(exc),
        ) from exc


def _download(url: str, dest: Path, cb: ProgressCB, *, label: str = "Ollama") -> None:
    _emit(cb, "downloading", f"Downloading {url.rsplit('/', 1)[-1]}\u2026", percent=0)
    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done / total * 100
                        _emit(cb, "downloading", f"Downloading {label}\u2026 {int(pct)}%", percent=pct)
                    else:
                        mb_done = done / (1024 * 1024)
                        _emit(cb, "downloading", f"Downloading {label}\u2026 {mb_done:,.0f} MB", percent=None)
    except requests.RequestException as exc:
        raise InstallerError(
            "Couldn't download the Ollama installer. Check your internet "
            "connection and try again.",
            str(exc),
        ) from exc


# --------------------------------------------------------------------------
# Linux sudo handling
# --------------------------------------------------------------------------

def linux_sudo_password_required() -> bool:
    """True if the official install script will need to prompt for a sudo
    password on this machine - i.e. we're not already root and passwordless
    sudo (``NOPASSWD`` or a still-live credential cache) isn't available."""
    if platform.system() != "Linux":
        return False
    try:
        if os.geteuid() == 0:  # already root
            return False
    except AttributeError:
        pass
    if shutil.which("sudo") is None:
        # No sudo at all - the install script's own `$SUDO` calls will
        # simply be skipped/no-ops in that case, nothing for us to supply.
        return False
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, timeout=5,
        )
        return result.returncode != 0
    except Exception:
        return True


def _fetch_install_script() -> str:
    """Download the official install script, tolerating transient network
    blips instead of failing on the first hiccup. Tries ``requests`` a few
    times with backoff, then falls back to the system ``curl`` (which on
    Linux often picks up proxy/CA config - env proxies, corporate root
    certs installed system-wide, etc. - that Python's ``requests`` may not
    see), before giving up entirely.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = requests.get(INSTALL_SCRIPT_URL, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

    curl = shutil.which("curl")
    if curl:
        try:
            result = subprocess.run(
                [curl, "-fsSL", INSTALL_SCRIPT_URL],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except Exception:
            pass

    raise InstallerError(
        "Couldn't reach ollama.com to download the installer. Check your "
        "internet connection (and any firewall/proxy) and try again.",
        str(last_exc) if last_exc else "curl fallback also failed",
    )


_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def _run_install_script_streaming(
    script: str, env: Optional[dict], cb: ProgressCB
) -> tuple[int, str]:
    """Run the official install.sh with live progress instead of blocking
    silently for minutes. Previously this used ``subprocess.run(...,
    capture_output=True)``, which sends exactly one progress update before
    the install script starts and then nothing else until it finishes (up
    to 15 minutes later) - the UI wasn't broken, it was accurately showing
    that no updates existed. install.sh actually prints its own step
    markers (">>> Downloading ollama...", ">>> Installing ollama to
    /usr/local/bin...", etc.) plus a live ``curl --progress-bar`` percentage
    while it downloads the ~700MB+ binary - both of those are captured and
    forwarded here as real progress instead of being silently swallowed.

    Returns ``(returncode, output_tail)`` - ``output_tail`` is the last
    ~2000 chars of combined output, for error messages on failure.
    """
    proc = subprocess.Popen(
        ["sh", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    all_output: list[str] = []
    buf = ""
    deadline = time.monotonic() + INSTALL_SUBPROCESS_TIMEOUT_S
    last_emit = 0.0

    def _flush(chunk: str) -> None:
        nonlocal last_emit
        chunk = chunk.strip()
        if not chunk:
            return
        all_output.append(chunk)
        # curl's --progress-bar writes a live "###   42.0%" style line via
        # \r - surface that as a real percentage instead of just text.
        m = _PERCENT_RE.search(chunk)
        now = time.monotonic()
        if m:
            _emit(cb, "installing", f"Downloading Ollama\u2026 {float(m.group(1)):.0f}%",
                  percent=float(m.group(1)))
            last_emit = now
        elif now - last_emit > 0.5:  # throttle plain status lines a bit
            _emit(cb, "installing", chunk[:160])
            last_emit = now

    try:
        assert proc.stdout is not None
        while True:
            ch = proc.stdout.read(1)
            if ch == "":
                if proc.poll() is not None:
                    break
                continue
            if ch in ("\n", "\r"):
                _flush(buf)
                buf = ""
            else:
                buf += ch
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                raise InstallerError(
                    "sh took too long to finish (over "
                    f"{int(INSTALL_SUBPROCESS_TIMEOUT_S // 60)} minutes).",
                    "\n".join(all_output[-50:]),
                )
        _flush(buf)
        returncode = proc.wait()
    except FileNotFoundError as exc:
        raise InstallerError("Couldn't run 'sh' - it isn't on this device.", str(exc)) from exc

    return returncode, "\n".join(all_output)[-2000:]


def _install_linux(cb: ProgressCB, sudo_password_provider: SudoPasswordProvider = None) -> None:
    """Official install script - always fetches the current stable build,
    same command ollama.com documents for every distro/architecture.

    The script shells out to ``sudo`` internally (to write to /usr/bin,
    install the systemd service, etc). Since it runs headless as a
    subprocess with no attached terminal, plain ``sudo`` can't prompt for a
    password there - so when a password is actually needed we ask the GUI
    for one up front (via ``sudo_password_provider``) and feed it to sudo
    through a throwaway ``SUDO_ASKPASS`` helper instead of ever touching a
    terminal.
    """
    _emit(cb, "installing", "Preparing the Ollama installer\u2026")
    script = _fetch_install_script()

    needs_password = linux_sudo_password_required()
    env = None
    askpass_dir: Optional[str] = None
    try:
        if needs_password:
            if sudo_password_provider is None:
                raise InstallerError(
                    "Installing Ollama needs administrator (sudo) access on "
                    "Linux, but no password prompt is available here. "
                    "Please install it manually from https://ollama.com/download."
                )
            password = sudo_password_provider()
            if not password:
                raise InstallerError(
                    "Installation cancelled - Ollama needs administrator "
                    "access to install on Linux."
                )

            # A tiny SUDO_ASKPASS helper that just hands sudo the password,
            # plus a `sudo` shim ahead of the real one on PATH so the
            # install script's own bare `sudo ...` calls automatically pick
            # up `-A` (ask via helper) instead of trying to read a tty.
            askpass_dir = tempfile.mkdtemp(prefix="prism_ollama_install_")
            askpass_path = Path(askpass_dir) / "askpass.sh"
            askpass_path.write_text(f"#!/bin/sh\necho {shlex.quote(password)}\n")
            askpass_path.chmod(0o700)
            password = None  # don't keep the plaintext around longer than needed

            real_sudo = shutil.which("sudo") or "/usr/bin/sudo"
            sudo_shim_path = Path(askpass_dir) / "sudo"
            sudo_shim_path.write_text(f"#!/bin/sh\nexec {shlex.quote(real_sudo)} -A \"$@\"\n")
            sudo_shim_path.chmod(0o700)

            env = os.environ.copy()
            env["SUDO_ASKPASS"] = str(askpass_path)
            env["PATH"] = f"{askpass_dir}:{env.get('PATH', '')}"
            _emit(cb, "installing", "Installing Ollama with the password you provided\u2026")
        else:
            _emit(cb, "installing", "Installing Ollama\u2026")

        returncode, output_tail = _run_install_script_streaming(script, env, cb)
    finally:
        if askpass_dir:
            shutil.rmtree(askpass_dir, ignore_errors=True)

    if returncode != 0:
        stderr_tail = output_tail
        lowered = stderr_tail.lower()
        if needs_password and (
            "incorrect password" in lowered
            or "sorry, try again" in lowered
            or "no password was provided" in lowered
            or "authentication failure" in lowered
        ):
            raise InstallerError(
                "That administrator password wasn't accepted. Please try again.",
                stderr_tail,
            )
        if "command not found" in lowered and ("curl" in lowered or "tar" in lowered or "gzip" in lowered):
            raise InstallerError(
                "This device is missing a tool the Ollama installer needs "
                "(curl, tar, or gzip). Install it with your package manager "
                "(e.g. `sudo apt install curl tar gzip` on Ubuntu/Debian) "
                "and try again.",
                stderr_tail,
            )
        if "unsupported" in lowered and ("arch" in lowered or "distro" in lowered or "os" in lowered):
            raise InstallerError(
                "This Linux distribution or architecture isn't supported by "
                "the official Ollama installer. Check "
                "https://ollama.com/download for supported options.",
                stderr_tail,
            )
        if "permission denied" in lowered:
            raise InstallerError(
                "The installer hit a permissions error writing system "
                "files. Make sure this user has sudo access, then try again.",
                stderr_tail,
            )
        raise InstallerError(
            "The Ollama install script failed. You may need to install it "
            "manually from https://ollama.com/download.",
            stderr_tail,
        )


def _install_macos(cb: ProgressCB, sudo_password_provider: SudoPasswordProvider = None) -> None:
    brew = shutil.which("brew")
    if brew:
        _emit(cb, "installing", "Installing Ollama via Homebrew\u2026")
        result = _run([brew, "install", "ollama"], timeout=INSTALL_SUBPROCESS_TIMEOUT_S)
        if result.returncode != 0:
            raise InstallerError(
                "`brew install ollama` failed. You can install it manually "
                "from https://ollama.com/download.",
                result.stderr[-2000:],
            )
        return

    # No Homebrew: download the official app bundle and unpack it directly,
    # matching what a user dragging Ollama.app into /Applications would do.
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "Ollama-darwin.zip"
        _download(MACOS_APP_ZIP_URL, zip_path, cb)
        _emit(cb, "installing", "Installing Ollama.app to /Applications\u2026")
        result = _run(
            ["ditto", "-x", "-k", str(zip_path), "/Applications"],
            timeout=180,
        )
        if result.returncode != 0:
            raise InstallerError(
                "Couldn't unpack Ollama.app into /Applications. Try "
                "installing it manually from https://ollama.com/download.",
                result.stderr[-2000:],
            )


def _install_windows(cb: ProgressCB, sudo_password_provider: SudoPasswordProvider = None) -> None:
    winget = shutil.which("winget")
    if winget:
        _emit(cb, "installing", "Installing Ollama via winget\u2026")
        result = _run(
            [winget, "install", "--id", "Ollama.Ollama", "-e",
             "--accept-source-agreements", "--accept-package-agreements",
             "--silent"],
            timeout=INSTALL_SUBPROCESS_TIMEOUT_S,
        )
        if result.returncode == 0:
            return
        # Fall through to the direct installer if winget itself failed
        # (e.g. no matching source configured) rather than giving up.

    with tempfile.TemporaryDirectory() as tmp:
        exe_path = Path(tmp) / "OllamaSetup.exe"
        _download(WINDOWS_INSTALLER_URL, exe_path, cb)
        _emit(cb, "installing", "Running the Ollama installer silently\u2026")
        result = _run([str(exe_path), "/VERYSILENT", "/NORESTART"], timeout=INSTALL_SUBPROCESS_TIMEOUT_S)
        if result.returncode != 0:
            raise InstallerError(
                "The Ollama installer failed to complete. Try running it "
                "manually from https://ollama.com/download.",
                result.stderr[-2000:],
            )


_INSTALLERS = {"Linux": _install_linux, "Darwin": _install_macos, "Windows": _install_windows}


def install(cb: ProgressCB = None, sudo_password_provider: SudoPasswordProvider = None) -> None:
    """Install the latest stable Ollama release for the current OS.

    Raises :class:`InstallerError` with a plain-language message on any
    failure (unsupported OS, network failure, installer exit code != 0, or
    an incorrect/cancelled sudo password on Linux).
    """
    system = platform.system()
    installer = _INSTALLERS.get(system)
    if installer is None:
        raise InstallerError(
            f"PRISM doesn't know how to auto-install Ollama on {system!r}. "
            "Please install it manually from https://ollama.com/download, "
            "then click Refresh Status."
        )
    installer(cb, sudo_password_provider)
    if find_binary() is None:
        raise InstallerError(
            "The installer finished, but Ollama still couldn't be found on "
            "this device. Please install it manually from "
            "https://ollama.com/download."
        )


# --------------------------------------------------------------------------
# Start
# --------------------------------------------------------------------------

def _running_server_pids(port: int = 11434) -> list[int]:
    """Best-effort PIDs of whatever's bound to Ollama's port, without
    killing anything - used to check the *existing* server's environment
    before deciding whether it needs a restart."""
    system = platform.system()
    if system == "Windows":
        return []
    lsof = shutil.which("lsof")
    if not lsof:
        return []
    try:
        result = subprocess.run(
            [lsof, "-ti", f":{port}"], capture_output=True, text=True, timeout=5,
        )
        return [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    except Exception:
        return []


def _server_has_no_cloud_env(port: int = 11434) -> Optional[bool]:
    """Best-effort check of whether an *already-running* ``ollama serve``
    has ``OLLAMA_NO_CLOUD`` set in its own environment (Linux only, via
    ``/proc/<pid>/environ`` - macOS/Windows have no equivalent unprivileged
    way to read another process's environment, so this returns None there
    and the caller treats that as "unknown, leave it alone").

    Returns True/False when it can tell, None when it genuinely can't.
    """
    if platform.system() != "Linux":
        return None
    for pid in _running_server_pids(port):
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except (OSError, PermissionError):
            continue
        entries = raw.split(b"\x00")
        for entry in entries:
            if entry.startswith(b"OLLAMA_NO_CLOUD="):
                value = entry.split(b"=", 1)[1].decode("utf-8", "replace")
                return value not in ("", "0", "false", "False")
        # Found the process's environ but no OLLAMA_NO_CLOUD key at all.
        return False
    return None


def _linux_systemd_ollama_active() -> bool:
    """True if ``ollama.service`` (system-level or --user) is currently
    active via systemd. On Ubuntu, the official install script sets this
    up by default with ``Restart=always`` (or similar) - which silently
    respawns ``ollama serve`` any time it's killed, *without* whatever
    environment (like OLLAMA_NO_CLOUD) the app tried to launch it with.
    Killing the PID directly (see _kill_stale_server_on_port) looks like
    it worked, but systemd just brings the same misconfigured server back
    within moments - this is the Linux equivalent of the Windows tray
    app respawn problem (ollama/ollama#14761), just via a different
    supervisor.
    """
    for args in (["systemctl", "is-active", "--quiet", "ollama"],
                 ["systemctl", "--user", "is-active", "--quiet", "ollama"]):
        try:
            if subprocess.run(args, capture_output=True, timeout=5).returncode == 0:
                return True
        except Exception:
            continue
    return False


def _stop_linux_systemd_ollama_noninteractive() -> bool:
    """Best-effort, silent stop+disable of the systemd unit using only
    passwordless sudo (``sudo -n``) - never prompts, since this is used
    from stop() during app shutdown where there's no GUI to ask. Safe to
    call even if the unit isn't active or passwordless sudo isn't
    configured; just does nothing and returns False in that case."""
    ok = False
    for args in (
        ["sudo", "-n", "systemctl", "disable", "--now", "ollama"],
        ["systemctl", "--user", "disable", "--now", "ollama"],
    ):
        try:
            if subprocess.run(args, capture_output=True, timeout=5).returncode == 0:
                ok = True
        except Exception:
            continue
    return ok


def _stop_linux_systemd_ollama_with_password(
    sudo_password_provider: "SudoPasswordProvider",
) -> bool:
    """Stop+disable the systemd unit using a GUI-supplied sudo password,
    via the same disposable SUDO_ASKPASS helper pattern _install_linux
    uses - so this never touches a terminal. Used from ensure_ollama()'s
    repair path, where a password prompt is possible."""
    if sudo_password_provider is None:
        return False
    password = sudo_password_provider()
    if not password:
        return False
    askpass_dir = tempfile.mkdtemp(prefix="prism_ollama_stop_")
    try:
        askpass_path = Path(askpass_dir) / "askpass.sh"
        askpass_path.write_text(f"#!/bin/sh\necho {shlex.quote(password)}\n")
        askpass_path.chmod(0o700)
        password = None
        env = os.environ.copy()
        env["SUDO_ASKPASS"] = str(askpass_path)
        try:
            result = subprocess.run(
                ["sudo", "-A", "systemctl", "disable", "--now", "ollama"],
                capture_output=True, timeout=15, env=env,
            )
            return result.returncode == 0
        except Exception:
            return False
    finally:
        shutil.rmtree(askpass_dir, ignore_errors=True)


def _kill_stale_server_on_port(port: int = 11434) -> bool:
    """Best-effort: find whatever process is bound to Ollama's port and
    kill it. Used when we're about to (re)start Ollama but something is
    already squatting on the port without actually serving requests
    properly (a wedged/zombie ``ollama serve`` - it can still answer
    ``/api/tags`` while pulls or generates hang forever). Returns True if
    something was found and killed.

    Windows: previously always returned False here without even trying,
    which meant ensure_ollama()'s auto-repair (restart a running-but-
    missing-OLLAMA_NO_CLOUD server) was a silent no-op on Windows - it
    would report "Ollama is already running" and leave the broken
    instance in place, so a pull stuck on "pulling manifest" could never
    self-heal via Start Ollama. Also kills `ollama app.exe` (the tray
    supervisor) first, not just `ollama.exe` (the server) - Ollama's
    Windows tray app silently respawns the server on its own if only the
    server process is killed (confirmed upstream, ollama/ollama#14761),
    which was undoing this fix within moments of it running.
    """
    system = platform.system()
    if system == "Windows":
        killed_anything = False
        try:
            r1 = subprocess.run(
                ["taskkill", "/IM", "ollama app.exe", "/F", "/T"],
                capture_output=True, timeout=5,
            )
            if r1.returncode == 0:
                killed_anything = True
        except Exception:
            pass
        try:
            r2 = subprocess.run(
                ["taskkill", "/IM", "ollama.exe", "/F", "/T"],
                capture_output=True, timeout=5,
            )
            if r2.returncode == 0:
                killed_anything = True
        except Exception:
            pass
        if killed_anything:
            time.sleep(1.0)
        return killed_anything
    pids = _running_server_pids(port)
    if not pids:
        return False
    for pid in pids:
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    time.sleep(1.0)
    return True


def _serve_env() -> dict:
    """Environment for launching ``ollama serve``, with ``OLLAMA_NO_CLOUD=1``
    set unless the person has already configured it explicitly themselves.

    Without this, Ollama tries to reach its cloud-model integration during
    a pull, which on some networks (proxies, firewalls, no outbound access
    to the cloud auth endpoint) hangs the manifest fetch indefinitely with
    no error - it just sits on "pulling manifest" forever, since the
    cloud handshake never times out on its own. Running ``ollama serve``
    with this set is the documented workaround, and confirmed to fix
    exactly that symptom - so the app does it automatically instead of
    requiring the person to notice and set it by hand every launch.
    """
    env = os.environ.copy()
    env.setdefault("OLLAMA_NO_CLOUD", "1")
    return env


def start(cb: ProgressCB = None) -> None:
    """Launch the local Ollama server in the background and wait for it to
    answer, raising :class:`InstallerError` if it never comes up."""
    _emit(cb, "starting", "Starting Ollama\u2026")
    system = platform.system()

    if system == "Darwin" and Path("/Applications/Ollama.app").exists():
        # The macOS app bundle runs its own menu-bar server; launching it
        # is the supported way to start the service (vs. invoking the CLI
        # server subprocess directly, which the app bundle install doesn't
        # expose the same way the Linux/Windows CLI installs do). `open`
        # launches it via LaunchServices, which does NOT inherit this
        # process's environment - `launchctl setenv` is the supported way
        # to get OLLAMA_NO_CLOUD into a GUI app's environment on macOS.
        # Best-effort: if it fails, we still launch the app rather than
        # blocking startup on it.
        try:
            subprocess.run(["launchctl", "setenv", "OLLAMA_NO_CLOUD", "1"], timeout=5)
        except Exception:
            pass
        _run(["open", "-a", "Ollama"], timeout=15)
    else:
        binary = find_binary()
        if binary is None:
            try:
                result = subprocess.run(
                    ["sh", "-lc", "command -v ollama"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    binary = result.stdout.strip().splitlines()[0]
            except Exception:
                pass
        if binary is None:
            raise InstallerError("Ollama isn't installed on this device yet.")
        creationflags = 0
        kwargs: dict = {}
        if system == "Windows":
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            kwargs["creationflags"] = creationflags
        try:
            subprocess.Popen(
                [binary, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=(system != "Windows"),
                env=_serve_env(),
                **kwargs,
            )
        except OSError as exc:
            raise InstallerError(f"Couldn't start Ollama: {exc}", str(exc)) from exc

    deadline = time.monotonic() + START_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        if is_running(timeout=2.0):
            _emit(cb, "done", "Ollama is running.")
            return
        time.sleep(START_POLL_INTERVAL_S)

    # Didn't come up in time - often means something is already bound to
    # the port but wedged (answers nothing, or answers /api/tags but not
    # real requests). Kill it and give the real server one more shot
    # before giving up, rather than leaving the person to find and kill
    # the stale process by hand.
    if _kill_stale_server_on_port():
        _emit(cb, "starting", "Clearing a stuck Ollama process and retrying\u2026")
        try:
            subprocess.Popen(
                [binary, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=(system != "Windows"),
                env=_serve_env(),
                **kwargs,
            )
        except OSError as exc:
            raise InstallerError(f"Couldn't start Ollama: {exc}", str(exc)) from exc

        retry_deadline = time.monotonic() + START_POLL_TIMEOUT_S
        while time.monotonic() < retry_deadline:
            if is_running(timeout=2.0):
                _emit(cb, "done", "Ollama is running.")
                return
            time.sleep(START_POLL_INTERVAL_S)

    raise InstallerError(
        "Ollama was installed but didn't start responding within "
        f"{START_POLL_TIMEOUT_S} seconds. Try launching it manually, then "
        "click Refresh Status."
    )


def stop() -> bool:
    """Best-effort stop of any locally running Ollama server, so the app
    never leaves one running in the background after it closes (and never
    hands the next launch a leftover process that may be missing
    OLLAMA_NO_CLOUD - see _serve_env). Returns True if something was found
    and asked to stop.

    Deliberately best-effort and silent on failure: this typically runs
    during app shutdown, where raising would just produce a confusing
    error on the way out rather than anything the person could act on.
    """
    system = platform.system()
    stopped = False

    if system == "Windows":
        # Ollama's Windows tray app (`ollama app.exe`, note the space in the
        # process name) supervises the server and silently respawns
        # `ollama.exe serve` if it's killed on its own - confirmed upstream
        # (ollama/ollama#14761). Killing only ollama.exe looked like it
        # worked but the respawned process came back with the same
        # missing OLLAMA_NO_CLOUD, undoing the fix within moments. Kill the
        # supervisor first so nothing relaunches the server out from under
        # us, then clean up the server process itself.
        try:
            r1 = subprocess.run(
                ["taskkill", "/IM", "ollama app.exe", "/F", "/T"],
                capture_output=True, timeout=5,
            )
            if r1.returncode == 0:
                stopped = True
        except Exception:
            pass
        try:
            r2 = subprocess.run(
                ["taskkill", "/IM", "ollama.exe", "/F", "/T"],
                capture_output=True, timeout=5,
            )
            if r2.returncode == 0:
                stopped = True
        except Exception:
            pass
        return stopped

    if system == "Darwin":
        # Covers both the CLI `ollama serve` process and the menu-bar app
        # bundle (started via `open -a Ollama` in start() above) - only
        # one of the two will actually be running, so try both.
        try:
            subprocess.run(["pkill", "-x", "ollama"], capture_output=True, timeout=5)
            stopped = True
        except Exception:
            pass
        try:
            subprocess.run(
                ["osascript", "-e", 'quit app "Ollama"'],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        return stopped

    # Linux
    stopped = False
    # If the official Ubuntu install set this up as a systemd service, a
    # bare process kill isn't enough - systemd will just respawn it
    # (typically with Restart=always), silently undoing the stop. Try a
    # passwordless-sudo disable first (this is app shutdown, so no GUI is
    # available to ask for a password); falls through to a plain PID kill
    # either way, since that's still needed if it's a plain `ollama serve`
    # with no systemd unit at all.
    if _linux_systemd_ollama_active():
        stopped = _stop_linux_systemd_ollama_noninteractive() or stopped
    pids = _running_server_pids()
    for pid in pids:
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError, ValueError):
            continue
        stopped = True
    return stopped


# --------------------------------------------------------------------------
# One-shot orchestration used by the GUI's "Install Ollama" button
# --------------------------------------------------------------------------

def ensure_ollama(cb: ProgressCB = None, sudo_password_provider: SudoPasswordProvider = None) -> None:
    """Detect -> install (if needed) -> start (if needed). Raises
    :class:`InstallerError` on any unrecoverable failure; returns normally
    only once ``/api/tags`` is confirmed reachable.

    ``sudo_password_provider``, if given, is called (with no arguments)
    only if a Linux install actually needs an administrator password - it
    should return the password to use, or None/"" to cancel.
    """
    _emit(cb, "detecting", "Checking for Ollama\u2026")
    state = detect()
    if state.running:
        # Already running doesn't necessarily mean *correctly* running - if
        # this instance was started outside the app (a login/system service,
        # a plain `ollama serve` in some other terminal, etc.) it may be
        # missing OLLAMA_NO_CLOUD, which is what makes model pulls hang
        # forever on "pulling manifest" on networks that can't reach
        # Ollama's cloud endpoint (see _serve_env() above) - a symptom
        # that's otherwise indistinguishable from the app itself being
        # broken. Restart it under our own controlled environment whenever
        # we can positively confirm it's missing that setting, so "Ollama
        # is already running" never quietly leaves a pull-breaking server
        # in place. If we can't tell (non-Linux, or the check itself
        # fails), leave it alone rather than restarting a server that may
        # be fine.
        if _server_has_no_cloud_env() is not True:
            # On Ubuntu (and other systemd distros), the official install
            # sets this up as a systemd service that auto-restarts itself
            # if just killed - so _kill_stale_server_on_port()'s plain PID
            # kill would appear to work but the service manager brings the
            # same misconfigured server straight back, and the pull would
            # go right back to hanging. Disable the unit first (needs
            # sudo - try silently, then ask via the GUI if that fails) so
            # the kill+restart below actually sticks.
            if _linux_systemd_ollama_active():
                _emit(cb, "starting", "Stopping the Ollama background service\u2026")
                if not _stop_linux_systemd_ollama_noninteractive():
                    if not _stop_linux_systemd_ollama_with_password(sudo_password_provider):
                        raise InstallerError(
                            "Ollama is running as a systemd service, and stopping "
                            "it needs administrator (sudo) access that wasn't "
                            "available here. Run "
                            "'sudo systemctl disable --now ollama' in a terminal, "
                            "then click Refresh Status."
                        )
            _emit(cb, "starting", "Restarting Ollama with cloud pulls disabled\u2026")
            if _kill_stale_server_on_port():
                start(cb)
                return
        _emit(cb, "done", "Ollama is already running.")
        return
    if not state.installed:
        # Belt-and-suspenders: find_binary()'s candidate list can miss a
        # real install (unusual install path, PATH not visible to this
        # process, etc). Before treating this as NOT_INSTALLED and running
        # the installer, do one more direct check via a shell so PATH is
        # resolved the same way a terminal would.
        try:
            result = subprocess.run(
                ["sh", "-lc", "command -v ollama"],
                capture_output=True, text=True, timeout=5,
            )
            shell_found = result.returncode == 0 and result.stdout.strip()
        except Exception:
            shell_found = False
        if not shell_found:
            install(cb, sudo_password_provider)
    start(cb)


def supported_platforms() -> tuple[str, ...]:
    return tuple(_INSTALLERS.keys())


def is_platform_supported() -> bool:
    return platform.system() in _INSTALLERS