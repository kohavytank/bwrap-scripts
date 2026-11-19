import os
import random
import re
import shlex
import shutil
import string
import sys
from glob import glob
from os import PathLike
from pathlib import Path
from subprocess import Popen
from typing import Never, override

HOME = Path.home()
XDG_CACHE_HOME = Path(os.getenv("XDG_CACHE_HOME", HOME / ".cache"))
XDG_CONFIG_HOME = Path(os.getenv("XDG_CONFIG_HOME", HOME / ".config"))
XDG_DATA_HOME = Path(os.getenv("XDG_DATA_HOME", HOME / ".local" / "share"))
XDG_RUNTIME_DIR = Path(
    os.getenv("XDG_RUNTIME_DIR", Path("/run/user") / str(os.getuid()))
)
# Sandbox-private replacements for host caches that host programs also read
SANDBOX_CACHE_HOME = XDG_CACHE_HOME / "bwrap-sandbox"


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


class CmdBuilder:
    _program: str | PathLike[str]
    _args: list[str | PathLike[str]]

    def __init__(self, program: str | PathLike[str]) -> None:
        self._program = program
        self._args = []

    def __repr__(self) -> str:
        return shlex.join(map(str, self.cmd))

    def args(self, *args: str | PathLike[str]) -> None:
        self._args += args

    def opt(self, key: str, *values: str | PathLike[str]) -> None:
        self.args(f"--{key}", *values)

    @property
    def cmd(self) -> list[str | PathLike[str]]:
        return [self._program, *self._args]

    def exec(self) -> Never:
        print(str(self), flush=True)
        argv = [str(a) for a in self.cmd]
        # argv[0] must be passed too, otherwise the first option is swallowed
        os.execvp(argv[0], argv)


class DbusProxy(CmdBuilder):
    _read_fd: int
    _write_fd: int
    session_bus: Path
    system_bus: Path
    _session_args: list[str | PathLike[str]]
    _system_args: list[str | PathLike[str]]
    _popen: Popen | None = None

    def __init__(self) -> None:
        super().__init__("xdg-dbus-proxy")
        self._read_fd, self._write_fd = os.pipe()
        DBUS_PROXY_DIR = XDG_RUNTIME_DIR / ".dbus-proxy"
        os.makedirs(DBUS_PROXY_DIR, exist_ok=True)
        random_name = "".join(random.sample(string.hexdigits, 5))
        self.session_bus = DBUS_PROXY_DIR / f"{random_name}.session.bus"
        self.system_bus = DBUS_PROXY_DIR / f"{random_name}.system.bus"
        self._session_args = []
        self._system_args = []
        # Base options
        self.opt("fd", str(self._write_fd))
        self.session_args(
            os.getenv(
                "DBUS_SESSION_BUS_ADDRESS",
                f"unix:path={XDG_RUNTIME_DIR / 'bus'}",
            ),
            self.session_bus,
        )
        self.session_opt("log")
        self.session_opt("filter")
        self.system_args(
            "unix:path=/run/dbus/system_bus_socket",
            self.system_bus,
        )
        self.system_opt("log")
        self.system_opt("filter")

    @staticmethod
    def _opt(key: str, value: str | PathLike[str] | None = None) -> str:
        return f"--{key}" + (f"={value}" if value is not None else "")

    @override
    def opt(self, key: str, *values: str | PathLike[str]) -> None:
        assert len(values) <= 1
        self.args(self._opt(key, values[0] if len(values) > 0 else None))

    def session_args(self, *args: str | PathLike[str]) -> None:
        self._session_args += args

    def session_opt(self, key: str, value: str | PathLike[str] | None = None) -> None:
        self.session_args(self._opt(key, value))

    def system_args(self, *args: str | PathLike[str]) -> None:
        self._system_args += args

    def system_opt(self, key: str, value: str | PathLike[str] | None = None) -> None:
        self.system_args(self._opt(key, value))

    @override
    @property
    def cmd(self) -> list[str | PathLike[str]]:
        return super().cmd + self._session_args + self._system_args

    @property
    def read_fd(self) -> int:
        os.set_inheritable(self._read_fd, True)
        return self._read_fd

    def spawn(self) -> Popen:
        assert self._popen is None, "xdg-dbus-proxy has already been spawned"
        print(str(self), flush=True)
        self._popen = Popen(
            self.cmd, pass_fds=[self._write_fd], stdout=sys.stdout, stderr=sys.stderr
        )
        # Wait until ready
        os.read(self._read_fd, 1)
        return self._popen


# Network namespace layout used with pasta. 198.18.0.0/15 is reserved for
# benchmarking, so it never collides with a real LAN.
_NET_ADDR = "198.18.0.2"
_NET_GW = "198.18.0.1"
_NET_DNS = "198.18.0.3"
# Private, link-local, CGNAT (Tailscale) and multicast ranges
_LAN_RANGES = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "224.0.0.0/4",
)


def _udev_props(dev: str) -> set[str]:
    try:
        rdev = os.stat(dev).st_rdev
        data = Path(f"/run/udev/data/c{os.major(rdev)}:{os.minor(rdev)}")
        return set(data.read_text().splitlines())
    except OSError:
        return set()


class Bwrap(CmdBuilder):
    dbus_proxy: DbusProxy
    _wrapper: list[str | PathLike[str]]

    def __init__(self, dbus_proxy: DbusProxy) -> None:
        super().__init__("bwrap")
        self.dbus_proxy = dbus_proxy
        self._wrapper = []
        # Base options
        self.opt("die-with-parent")
        self.opt("new-session")
        self.opt("clearenv")
        # Namespaces. IPC is left shared because X11 MIT-SHM needs it.
        # User namespaces stay enabled because pressure-vessel (used by UMU)
        # runs its own bwrap inside this sandbox.
        self.opt("unshare-user")
        self.opt("uid", str(os.getuid()))
        self.opt("gid", str(os.getgid()))
        self.opt("unshare-pid")
        self.opt("unshare-uts")
        self.opt("unshare-cgroup-try")
        # Base filesystem
        self.opt("proc", "/proc")
        self.opt("tmpfs", "/tmp")
        self.opt("dev", "/dev")
        self.ro("/usr", required=True)
        self.ro("/bin", required=True)
        self.ro("/lib", required=True)
        self.ro("/lib64", required=True)
        self.ro("/sbin")
        self.dir(XDG_RUNTIME_DIR, mode=0o0700)
        # Basic environment
        self.env("PATH", "/usr/bin:/usr/sbin")
        self.env("HOME")
        self.env("XDG_CACHE_HOME")
        self.env("XDG_CONFIG_HOME")
        self.env("XDG_DATA_HOME")
        self.env("XDG_RUNTIME_DIR", XDG_RUNTIME_DIR)
        # D-Bus
        # REF: https://wiki.archlinux.org/title/Bubblewrap/Examples#Steam
        self.opt("sync-fd", str(self.dbus_proxy.read_fd))
        DBUS_SESSION_BUS_ADDRESS = XDG_RUNTIME_DIR / "bus"
        self.rw(self.dbus_proxy.session_bus, DBUS_SESSION_BUS_ADDRESS, required=True)
        self.env("DBUS_SESSION_BUS_ADDRESS", DBUS_SESSION_BUS_ADDRESS)
        self.rw(
            self.dbus_proxy.system_bus,
            "/run/dbus/system_bus_socket",
            required=True,
        )

    @override
    @property
    def cmd(self) -> list[str | PathLike[str]]:
        return self._wrapper + super().cmd

    def _perms(self, mode: int) -> None:
        self.opt("perms", f"{mode:04o}")

    def dir(self, dest: str | PathLike[str] | None, mode: int | None = None) -> None:
        if dest:
            if mode is not None:
                self._perms(mode)
            self.opt("dir", dest)

    def _bind(
        self,
        opt: str,
        src: str | PathLike[str] | None,
        dest: str | PathLike[str] | None = None,
        create_dir: bool = False,
        required: bool = False,
    ) -> None:
        if dest is None:
            dest = src
        if src and dest:
            if create_dir:
                if not os.path.exists(src):
                    os.makedirs(src)
                required = True
            self.opt(opt if required else f"{opt}-try", src, dest)

    def dev(
        self,
        src: str | PathLike[str] | None,
        dest: str | PathLike[str] | None = None,
        required: bool = False,
    ) -> None:
        self._bind("dev-bind", src, dest, required=required)

    def rw(
        self,
        src: str | PathLike[str] | None,
        dest: str | PathLike[str] | None = None,
        create_dir: bool = False,
        required: bool = False,
    ) -> None:
        self._bind("bind", src, dest, create_dir, required)

    def ro(
        self,
        src: str | PathLike[str] | None,
        dest: str | PathLike[str] | None = None,
        create_dir: bool = False,
        required: bool = False,
    ) -> None:
        self._bind("ro-bind", src, dest, create_dir, required)

    def ro_data(self, name: str, data: str, dest: str | PathLike[str]) -> None:
        # A real file rather than --ro-bind-data: pressure-vessel re-binds it
        # in its nested container, which fails for bwrap's unlinked temp file
        DATA_DIR = XDG_RUNTIME_DIR / ".bwrap-sandbox"
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        path = DATA_DIR / name
        if not path.exists() or path.read_text() != data:
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(data)
            tmp.replace(path)
        self.ro(path, dest, required=True)

    def private_cache(self, name: str) -> None:
        """Give the sandbox its own copy of a cache dir shared with host apps"""
        self.rw(SANDBOX_CACHE_HOME / name, XDG_CACHE_HOME / name, create_dir=True)

    def env(self, key: str, value: str | PathLike[str] | None = None) -> None:
        if value is None:
            value = os.getenv(key)
        if value:
            self.opt("setenv", key, value)

    def network(self) -> None:
        """
        BWRAP_NETWORK selects the network access:
          internet (default): internet only; host services and LAN blocked
          lan: internet and LAN; host services blocked
          none: no network at all
          host: share the host network (insecure, exposes host X11/services)
        """
        mode = os.getenv("BWRAP_NETWORK", "internet")
        if mode == "host":
            warn(
                "BWRAP_NETWORK=host: the sandbox can reach host services, "
                "the LAN and abstract sockets like the host X server"
            )
            self.ro("/etc/resolv.conf")
            return
        if mode == "none":
            self.opt("unshare-net")
            return
        # pasta runs bwrap inside its own network namespace
        if mode not in ("internet", "lan"):
            sys.exit(f"Unknown BWRAP_NETWORK={mode!r}")
        if shutil.which("pasta") is None:
            sys.exit(
                "pasta (from passt) is required for network access; "
                "install it or set BWRAP_NETWORK=none"
            )
        self._wrapper = [
            "pasta",
            "--config-net",
            "--quiet",
            "--no-map-gw",
            # No port forwarding in either direction
            *("-t", "none", "-u", "none", "-T", "none", "-U", "none"),
            *("-a", _NET_ADDR, "-n", "24", "-g", _NET_GW),
            *("--dns-forward", _NET_DNS),
        ]
        routes = ""
        if mode == "internet":
            # IPv6 LAN addresses are global, so they can't be blackholed
            self._wrapper.append("-4")
            routes = " ".join(_LAN_RANGES)
        # Runs inside pasta's namespaces, before bwrap drops capabilities
        self._wrapper += [
            "--",
            "sh",
            "-c",
            'set -e; for r in $0; do ip route add blackhole "$r"; done; exec "$@"',
            routes,
        ]
        self.ro_data("resolv.conf", f"nameserver {_NET_DNS}\n", "/etc/resolv.conf")

    def display(self) -> None:
        gamescope = os.getenv("GAMESCOPE_WAYLAND_DISPLAY")
        # X11: only the socket of $DISPLAY, never the whole directory
        DISPLAY = os.getenv("DISPLAY")
        if DISPLAY:
            m = re.fullmatch(r"(?:unix)?:(\d+)(?:\.\d+)?", DISPLAY)
            if m is None:
                warn(f"Unsupported DISPLAY={DISPLAY!r}, X11 not exposed")
            else:
                self.rw(f"/tmp/.X11-unix/X{m[1]}")
                self.env("DISPLAY")
                if gamescope is None:
                    warn(
                        "Exposing the host X server: the sandboxed app can log "
                        "keys, read the screen and type into other windows. "
                        "Run it nested in gamescope to prevent this."
                    )
                    _XAUTHORITY = "XAUTHORITY"
                    self.ro(os.getenv(_XAUTHORITY, HOME / ".Xauthority"))
                    self.env(_XAUTHORITY)
        # Wayland: under gamescope, only gamescope's own compositor
        WAYLAND_DISPLAY = os.getenv("WAYLAND_DISPLAY")
        if WAYLAND_DISPLAY and gamescope in (None, WAYLAND_DISPLAY):
            self.rw(XDG_RUNTIME_DIR / WAYLAND_DISPLAY)
            self.env("WAYLAND_DISPLAY")

    def gpus(self) -> None:
        self.dev("/dev/dri")
        for d in glob("/dev/nvidia*"):
            self.dev(d)
        # Shader caches (kept apart from the host's, so poisoned entries
        # cannot reach host GL/Vulkan apps)
        self.private_cache("mesa_shader_cache")
        self.private_cache("radv_builtin_shaders")
        self.private_cache("nvidia")

    def audio(self) -> None:
        # ALSA
        self.dev("/dev/snd")
        # PulseAudio
        self.rw(XDG_RUNTIME_DIR / "pulse")
        # PipeWire (not the privileged pipewire-0-manager socket)
        self.rw(XDG_RUNTIME_DIR / os.getenv("PIPEWIRE_REMOTE", "pipewire-0"))

    def input(self) -> None:
        events = glob("/dev/input/event*")
        keyboard_readable = any(
            "E:ID_INPUT_KEYBOARD=1" in _udev_props(d) and os.access(d, os.R_OK)
            for d in events
        )
        if not keyboard_readable:
            # Nothing sensitive is accessible: bind the dir to keep hotplug
            self.dev("/dev/input")
            return
        # Keyboards are readable (e.g. member of "input"): joysticks only
        warn("Keyboards are readable by this user; exposing joysticks only")
        for d in glob("/dev/input/js*"):
            self.dev(d)
        for d in events:
            if "E:ID_INPUT_JOYSTICK=1" in _udev_props(d):
                self.dev(d)

    @override
    def exec(self) -> Never:
        self.dbus_proxy.spawn()
        super().exec()


def main(bwrap: Bwrap, command: list[str]) -> Never:
    bwrap.args(*command)
    bwrap.exec()
