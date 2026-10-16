#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bash.py —— 在 Alpine Linux 沙箱里执行命令的 Python 示例（Windows + WSL2）

它做什么
--------
1. 首次运行时自动下载 Alpine minirootfs（最小根文件系统，约 4 MB）。
2. 把它解压成项目里的一个**普通目录** ``sandbox/data/`` —— 这个目录就是沙箱的 ``/``。
   没有 ext4 镜像、没有 loop 挂载：你在资源管理器里看到的文件，就是沙箱里的文件；
   反过来沙箱里跑命令产生的改动，也立刻出现在这个目录里。
3. 每次执行命令时：在 WSL2 里进一个**私有 mount + PID 命名空间**
   （``unshare -mpf --mount-proc``），挂好 proc/sys/dev，再用 ``pivot_root`` 把
   沙箱根变成真正的 ``/`` 并卸载旧根，然后执行命令。
   namespace 退出时挂载自动回收，宿主 WSL 不留任何痕迹。
4. ``sandbox/data/`` 是持久的：沙箱里 apk 装的包、写的文件，下次运行依然在。
   初始化时还会在沙箱内实测各 apk 镜像站的下载速度，选最快的那个；
   本机实测官方 CDN 只有 ~11KB/s（apk add curl 要 607 秒），换国内镜像后只要 1.25 秒。

用法
----
    python bash.py --init                         # 只初始化沙箱环境（幂等）
    python bash.py "cat /etc/alpine-release"       # 执行一条命令并拿到输出
    python bash.py "apk add --no-cache curl && curl -sI https://example.com"
    python bash.py --shell                         # 进交互式 Alpine shell
    python bash.py "echo hi > /root/a.txt"         # 写进沙箱（持久）
    python bash.py "cat /root/a.txt"               # 下次运行依然能读到
    python bash.py --info                          # 查看沙箱环境信息
    python bash.py --reset --yes                   # 清空沙箱（重新初始化）

目录结构
--------
    alpine-sandbox/
    ├─ bash.py
    └─ sandbox/
       ├─ data/            ← 沙箱根文件系统（就是沙箱里的 "/"，可以直接编辑）
       ├─ downloads/       ← 缓存的 alpine-minirootfs tarball
       ├─ staging/         ← 每次运行生成的控制脚本
       ├─ runs/*.log       ← 每次运行的完整记录
       ├─ sandbox.json     ← 版本 / 镜像站 / 创建时间
       └─ .provisioned     ← 供应完成标记

依赖：Windows + WSL2（任一发行版作为宿主，需带 tar / unshare / mount / chroot，
Debian 自带）。不再需要 e2fsprogs。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- 配置

HERE = Path(__file__).resolve().parent
SANDBOX_DIR = HERE / "sandbox"
ROOT_DIR = SANDBOX_DIR / "data"                  # ← 沙箱根：这个目录就是沙箱里的 /
LEGACY_IMAGE = SANDBOX_DIR / "alpine-rootfs.img"  # 旧版 ext4 镜像（已不再使用）
DOWNLOAD_DIR = SANDBOX_DIR / "downloads"
STAGING_DIR = SANDBOX_DIR / "staging"
LOG_DIR = SANDBOX_DIR / "runs"
META_PATH = SANDBOX_DIR / "sandbox.json"
MARKER_PATH = SANDBOX_DIR / ".provisioned"

ALPINE_VERSION = os.environ.get("ALPINE_VERSION", "3.24.2")
ALPINE_BRANCH = ".".join(ALPINE_VERSION.split(".")[:2])          # 3.24.2 -> 3.24
MIRROR = os.environ.get("ALPINE_MIRROR",
                        "https://dl-cdn.alpinelinux.org/alpine").rstrip("/")
# 初始化时会在沙箱内实测这些镜像站的下载速度，选最快的写入 apk 配置。
# 官方 CDN 放在首位（海外网络下通常是它最快），后面是国内常用镜像。
MIRROR_CANDIDATES = [
    "https://dl-cdn.alpinelinux.org/alpine",
    "https://mirror.nju.edu.cn/alpine",
    "https://mirrors.ustc.edu.cn/alpine",
    "https://mirrors.tuna.tsinghua.edu.cn/alpine",
    "https://mirrors.aliyun.com/alpine",
]
INTERNAL_EXIT = 90      # 控制脚本自身失败时使用的退出码


class SandboxError(RuntimeError):
    """沙箱自身出错（区别于用户命令的失败）。"""


# --------------------------------------------------------------------------- 小工具

def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def sh_quote(value: str) -> str:
    """把字符串安全地嵌进 POSIX shell 单引号里。"""
    return "'" + str(value).replace("'", "'\\''") + "'"


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def dir_usage(path: Path) -> tuple[int, int]:
    """返回 (总字节数, 文件数)。"""
    total = files = 0
    for base, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += (Path(base) / name).lstat().st_size
                files += 1
            except OSError:
                continue
    return total, files


def wsl_exe() -> str:
    found = shutil.which("wsl") or shutil.which("wsl.exe")
    if found:
        return found
    candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe"
    if candidate.exists():
        return str(candidate)
    raise SandboxError(
        "找不到 wsl.exe。本示例需要 Windows + WSL2 作为宿主：\n"
        "  1) 以管理员身份运行: wsl --install -d Debian\n"
        "  2) 确认可用: wsl -u root -- echo ok"
    )


def to_wsl_path(path: Path) -> str:
    """D:\\a\\b  ->  /mnt/d/a/b"""
    resolved = Path(path).resolve()
    drive = resolved.drive
    if len(drive) != 2 or drive[1] != ":":
        raise SandboxError(f"无法把路径转换成 WSL 路径（需要盘符）: {resolved}")
    tail = str(resolved)[2:].replace("\\", "/")
    return f"/mnt/{drive[0].lower()}{tail}"


def wsl_argv(distro: str, args: list[str]) -> list[str]:
    argv = [wsl_exe()]
    if distro:
        argv += ["-d", distro]
    argv += ["-u", "root", "--", *args]
    return argv


def wsl_run(distro: str, args: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(wsl_argv(distro, args), **kwargs)
    except FileNotFoundError as exc:                      # pragma: no cover
        raise SandboxError(f"无法启动 WSL: {exc}") from exc
    except subprocess.TimeoutExpired:
        raise


class SandboxLock:
    """同一时刻只允许一个进程动这个沙箱，避免并发互相干扰。"""

    STALE_SECONDS = 30 * 60

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self) -> "SandboxLock":
        for attempt in (1, 2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                age = time.time() - self.path.stat().st_mtime
                if attempt == 1 and age > self.STALE_SECONDS:
                    eprint("[bash.py] 发现超过 30 分钟未更新的锁文件，视为残留并接管")
                    self.path.unlink(missing_ok=True)
                    continue
                raise SandboxError(
                    f"另一个 bash.py 正在使用沙箱（{self.path.name}，"
                    f"{age:.0f} 秒前创建）。\n"
                    "等它结束后重试；若确认没有其它进程，删除该文件即可。")
            else:
                os.write(fd, f"pid={os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
                os.close(fd)
                self.acquired = True
                return self
        raise SandboxError("无法获取沙箱锁")

    def __exit__(self, *exc_info: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
        self.acquired = False


# --------------------------------------------------------------------------- 控制脚本

_CHECK_SCRIPT = r"""
missing=""
for t in unshare mount chroot tar mktemp; do
  command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
command -v pivot_root >/dev/null 2>&1 || [ -x /usr/sbin/pivot_root ] || missing="$missing pivot_root"
echo "OS=$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
echo "ID=$(. /etc/os-release 2>/dev/null; echo "${ID:-unknown}")"
echo "MISSING=$missing"
"""

# 供应沙箱：$1 = 沙箱根目录, $2 = minirootfs tarball, $3 = repositories 内容文件
_PROVISION_SCRIPT = r"""
ROOT="$1"; TAR="$2"; REPO="$3"
set -e
mkdir -p "$ROOT"
if [ ! -x "$ROOT/bin/busybox" ]; then
  echo "[bash.py] 解压 Alpine minirootfs -> $ROOT"
  tar -xzpf "$TAR" -C "$ROOT"
fi
mkdir -p "$ROOT/proc" "$ROOT/sys" "$ROOT/dev/pts" "$ROOT/dev/shm" \
         "$ROOT/tmp" "$ROOT/root" "$ROOT/etc/apk" "$ROOT/var/tmp"
chmod 1777 "$ROOT/tmp" "$ROOT/var/tmp" 2>/dev/null || true
cp -f "$REPO" "$ROOT/etc/apk/repositories"
[ -f "$ROOT/etc/apk/world" ] || : > "$ROOT/etc/apk/world"
if [ -s /etc/resolv.conf ]; then cp -f /etc/resolv.conf "$ROOT/etc/resolv.conf"; fi
grep -q nameserver "$ROOT/etc/resolv.conf" 2>/dev/null || \
  printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$ROOT/etc/resolv.conf"
printf '127.0.0.1 localhost alpine-sandbox\n::1 localhost\n' > "$ROOT/etc/hosts"
echo alpine-sandbox > "$ROOT/etc/hostname"
echo "[bash.py] busybox 自检: $(chroot "$ROOT" /bin/busybox echo ok 2>&1)"
"""

# 执行命令的runner：$1 = 沙箱根目录, $2 = 入口脚本文件名（位于沙箱 /tmp 下）, 之后是挂载卷
_RUN_SCRIPT = r"""
if [ -z "${DSH_NS:-}" ]; then
  command -v unshare >/dev/null 2>&1 || { echo "[bash.py] 宿主缺少 unshare" >&2; exit 90; }
  export DSH_NS=1                  # 必须 export，否则重新执行的自己看不到会无限递归
  if unshare -mpf --mount-proc --propagation private -- true 2>/dev/null; then
    exec unshare -mpf --mount-proc --propagation private -- sh "$0" "$@"
  fi
  exec unshare -m --propagation private -- sh "$0" "$@"
fi
set -u
ROOT="$1"; ENTRY="$2"; shift 2 || true
die() { echo "[bash.py] $*" >&2; exit 90; }
[ -d "$ROOT" ] || die "沙箱根目录不存在: $ROOT"
[ -x "$ROOT/bin/busybox" ] || die "沙箱未初始化: $ROOT（先运行 python bash.py --init）"

mkdir -p "$ROOT/proc" "$ROOT/sys" "$ROOT/dev" "$ROOT/.oldroot" 2>/dev/null || true

# 1) 先把沙箱根做成独立挂载点。必须用 --bind（不是 --rbind），而且要在挂
#    proc/sys/dev **之前**做 —— 否则普通 --bind 会把这几个子挂载遮在下面看不见。
BOUND=0
mount --bind "$ROOT" "$ROOT" 2>/dev/null && BOUND=1

mount -t proc proc "$ROOT/proc" 2>/dev/null || true
mount -t sysfs sysfs "$ROOT/sys" 2>/dev/null || true
mount --rbind /dev "$ROOT/dev" 2>/dev/null || true
mount --make-rslave "$ROOT/dev" 2>/dev/null || true

# 2) 用户用 -v 指定的卷（宿主目录 -> 沙箱内路径）
__VOLUMES__

ENVV="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root USER=root SHELL=/bin/sh LANG=C.UTF-8 TERM=${TERM:-xterm}"

# 3) 优先 pivot_root：让沙箱根真正成为 "/"（df、/proc/mounts 才正常），
#    顺便卸载旧根，堵掉 chroot 逃逸。必须在当前 shell 里做。
PIVOT=$(command -v pivot_root 2>/dev/null || echo /usr/sbin/pivot_root)
if [ "$BOUND" = 1 ]; then
  cd "$ROOT" || die "无法进入沙箱根目录: $ROOT"
  if "$PIVOT" . .oldroot 2>/dev/null; then
    umount -l /.oldroot 2>/dev/null || true
    __EXEC__
  fi
  cd / 2>/dev/null || true
fi

# 4) pivot_root 不可用时退回 chroot
chroot "$ROOT" /usr/bin/env -i $ENVV /bin/sh "/tmp/$ENTRY"
"""

_EXEC_CAPTURE = r"""exec /usr/bin/env -i $ENVV /bin/sh /tmp/$ENTRY"""
_EXEC_SHELL = r"""exec /usr/bin/env -i $ENVV /bin/sh -c '. /tmp/$ENTRY; exec /bin/sh -i'"""


def run_control_script(name: str, content: str, opts: argparse.Namespace,
                       extra_args: list[str] = (), interactive: bool = False,
                       capture: bool = True, timeout: float | None = None,
                       ) -> subprocess.CompletedProcess:
    """把控制脚本写到 sandbox/staging 下，再交给 WSL 执行（避免任何引号转义问题）。"""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    script = STAGING_DIR / name
    # 一律写成 LF 换行：sh 遇到 CR 会报 "$'\r': command not found"
    script.write_bytes(content.replace("\r\n", "\n").encode("utf-8"))
    args = ["sh", to_wsl_path(script), *[str(a) for a in extra_args]]
    kwargs: dict = {}
    if interactive:
        kwargs["capture_output"] = False
    elif capture:
        kwargs["capture_output"] = True
    if timeout is not None:
        kwargs["timeout"] = timeout
    return wsl_run(opts.distro, args, **kwargs)


def decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- 环境准备

def check_host_tools(opts: argparse.Namespace) -> str:
    proc = run_control_script("_check.sh", _CHECK_SCRIPT, opts, timeout=180)
    out = decode(proc.stdout)
    if proc.returncode != 0:
        raise SandboxError(
            "宿主 WSL 里无法运行检查脚本：\n" + (out + decode(proc.stderr)).strip())
    missing = ""
    host = "unknown"
    for line in out.splitlines():
        if line.startswith("MISSING="):
            missing = line.split("=", 1)[1].strip()
        elif line.startswith("OS="):
            host = line.split("=", 1)[1].strip()
    if missing:
        raise SandboxError(
            f"宿主 WSL（{host}）缺少工具: {missing}\n"
            "请在有这些工具的发行版里初始化，例如：\n"
            "  1) wsl -l -v                      # 查看已有发行版\n"
            "  2) python bash.py --distro Debian --init"
        )
    return host


def download_minirootfs(version: str) -> Path:
    tarball = f"alpine-minirootfs-{version}-x86_64.tar.gz"
    base = f"{MIRROR}/v{'.'.join(version.split('.')[:2])}/releases/x86_64"
    target = DOWNLOAD_DIR / tarball
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if target.exists() and target.stat().st_size > 0:
        eprint(f"[bash.py] 已缓存 {target.name}")
    else:
        url = f"{base}/{tarball}"
        eprint(f"[bash.py] 下载 {url}")
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        print(f"\r[bash.py]   {pct:3d}%  {human_size(done)} / {human_size(total)}",
                              end="", file=sys.stderr, flush=True)
            print("", file=sys.stderr)
        except (urllib.error.URLError, OSError) as exc:
            tmp.unlink(missing_ok=True)
            raise SandboxError(f"下载失败: {exc}\n可手动下载后放到 {DOWNLOAD_DIR}") from exc
        tmp.replace(target)

    verify_checksum(target, base)
    return target


def verify_checksum(tarball: Path, base: str) -> None:
    """尽量校验 SHA256；拿不到校验文件时只警告，不阻断。"""
    try:
        with urllib.request.urlopen(f"{base}/{tarball.name}.sha256", timeout=30) as resp:
            expected = resp.read().decode("utf-8", "replace").strip().split()[0].lower()
    except (urllib.error.URLError, OSError, IndexError):
        eprint("[bash.py] 警告: 未取到官方 SHA256，跳过校验")
        return
    digest = hashlib.sha256()
    with open(tarball, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        tarball.unlink(missing_ok=True)
        raise SandboxError(f"SHA256 校验失败！期望 {expected}，实际 {actual}")
    eprint(f"[bash.py] SHA256 校验通过: {actual[:16]}...")


def sandbox_ready() -> bool:
    """沙箱文件系统是否可用（不看标记：供应过程中也要能跑镜像测速）。"""
    return (ROOT_DIR / "bin" / "busybox").exists()


def fully_provisioned() -> bool:
    return sandbox_ready() and MARKER_PATH.exists()


def build_mirror_probe(branch: str, candidates: list[str]) -> str:
    """生成在沙箱内运行的镜像站测速脚本（用 Alpine 自带的 busybox wget）。

    每个候选站整体限时 12 秒：否则慢站在 busybox wget 的"读超时"下仍会慢慢下完
    528KB（实测官方 CDN 要 47 秒），把初始化拖长。
    """
    lines = ["best=''", "bestt=9999",
             "TO=''", 'command -v timeout >/dev/null 2>&1 && TO="timeout 12"']
    for base in candidates:
        url = f"{base}/v{branch}/main/x86_64/APKINDEX.tar.gz"
        # 必须伪装成 apk 的 UA：中科大/清华等镜像站会 403 掉 wget 默认 UA 和 Mozilla，
        # 但放行 apk-tools（实测），否则测速会误判它们不可用。
        lines.append(
            "t0=$(date +%s); "
            f"if $TO wget -q -U apk-tools/2.14 -T 8 -t 1 -O /dev/null {sh_quote(url)} 2>/dev/null; then "
            "dt=$(( $(date +%s) - t0 )); "
            f'echo "MIRROR {base} ${{dt}}s"; '
            f"if [ $dt -lt $bestt ]; then bestt=$dt; best={sh_quote(base)}; fi; "
            f'else echo "MIRROR {base} FAIL"; fi')
    lines.append(
        'if [ -n "$best" ]; then '
        f"printf '%s/v{branch}/main\\n%s/v{branch}/community\\n' \"$best\" \"$best\" "
        '> /etc/apk/repositories; echo "CHOSEN $best"; '
        'else echo "CHOSEN (none)"; fi')
    return "\n".join(lines) + "\n"


def write_repositories(opts: argparse.Namespace, base: str) -> None:
    """在沙箱内写 /etc/apk/repositories（在沙箱里写，天然是 LF 换行）。"""
    branch = ".".join(opts.alpine_version.split(".")[:2])
    content = f"{base.rstrip('/')}/v{branch}/main\n{base.rstrip('/')}/v{branch}/community\n"
    code, _out, err, _secs = run_in_sandbox(
        opts, f"printf '%s' {sh_quote(content)} > /etc/apk/repositories && cat /etc/apk/repositories")
    if code != 0:
        eprint(f"[bash.py] 警告: 写入 apk 镜像失败: {err.strip()}")


def probe_mirror(opts: argparse.Namespace) -> str | None:
    """在沙箱里实测各镜像站下载速度，选最快的。测速脚本自己会写 repositories。"""
    branch = ".".join(opts.alpine_version.split(".")[:2])
    eprint("[bash.py] 从沙箱内实测各镜像站下载速度（取最快）...")
    code, stdout, stderr, _secs = run_in_sandbox(opts, build_mirror_probe(branch, MIRROR_CANDIDATES))
    chosen = None
    for line in stdout.splitlines():
        if line.startswith("MIRROR "):
            eprint(f"[bash.py]   {line[7:]}")
        elif line.startswith("CHOSEN "):
            value = line[7:].strip()
            chosen = value if value and value != "(none)" else None
    if code != 0 or chosen is None:
        eprint("[bash.py] 镜像测速未成功，沿用默认镜像")
        return None
    eprint(f"[bash.py] 选用镜像: {chosen}")
    return chosen


def init_sandbox(opts: argparse.Namespace) -> None:
    """供应沙箱（幂等）：解压 rootfs + 选镜像站 + 写标记。"""
    if fully_provisioned():
        if opts.init:
            eprint(f"[bash.py] 沙箱已就绪: {ROOT_DIR}（如需重建: --reset --yes）")
        return
    if os.name != "nt":
        raise SandboxError("本示例面向 Windows + WSL2；在 Linux 上可直接用 chroot/容器。")
    if LEGACY_IMAGE.exists():
        eprint(f"[bash.py] 提示: 检测到旧版 ext4 镜像 {LEGACY_IMAGE.name}，"
               f"现在改为目录式沙箱（{ROOT_DIR.name}/），该文件已不再使用，"
               f"可用 --reset --yes 一并删除。")

    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    eprint(f"[bash.py] 初始化 Alpine {opts.alpine_version} 沙箱 -> {ROOT_DIR}")
    host = check_host_tools(opts)
    tarball = download_minirootfs(opts.alpine_version)

    repo_file = STAGING_DIR / "_repositories.txt"
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    repo_file.write_bytes(
        f"{MIRROR}/v{ALPINE_BRANCH}/main\n{MIRROR}/v{ALPINE_BRANCH}/community\n".encode("utf-8"))
    proc = run_control_script("_provision.sh", _PROVISION_SCRIPT, opts,
                              extra_args=[to_wsl_path(ROOT_DIR), to_wsl_path(tarball),
                                          to_wsl_path(repo_file)],
                              timeout=1800)
    if proc.returncode != 0 or not sandbox_ready():
        raise SandboxError("初始化失败:\n" + (decode(proc.stdout) + decode(proc.stderr)).strip())

    # Alpine 官方 CDN 在部分网络下极慢（实测 10 KB/s 级别），会导致 apk 卡几分钟。
    if opts.mirror:
        mirror = opts.mirror.rstrip("/")
        write_repositories(opts, mirror)
    elif opts.mirror_probe:
        mirror = probe_mirror(opts) or MIRROR
    else:
        mirror = MIRROR

    meta = {
        "alpine_version": opts.alpine_version,
        "root_dir": "sandbox/data",
        "host_distro": host,
        "apk_mirror": mirror,
        "mirror_probe_candidates": MIRROR_CANDIDATES,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    MARKER_PATH.write_text(f"{ALPINE_VERSION} {mirror}\n", encoding="utf-8")
    used, files = dir_usage(ROOT_DIR)
    eprint(f"[bash.py] 完成：沙箱根 = {ROOT_DIR}"
           f"（{human_size(used)} / {files} 个文件），改动会直接落在这个目录里")


def ensure_sandbox(opts: argparse.Namespace) -> None:
    if not sandbox_ready():
        init_sandbox(opts)


# --------------------------------------------------------------------------- 执行命令

def build_entry_script(command: str, cwd: str, envs: list[str]) -> str:
    lines = [
        "#!/bin/sh",
        "# 由 bash.py 自动生成：沙箱内的入口脚本（不要手工修改）",
        f"cd {sh_quote(cwd)} 2>/dev/null || cd /",
    ]
    for item in envs:
        key, _, value = item.partition("=")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise SandboxError(f"非法的环境变量名: {key!r}")
        lines.append(f"export {key}={sh_quote(value)}")
    if command:
        lines.append(command)
    return "\n".join(lines) + "\n"


def parse_volume(spec: str) -> tuple[str, str]:
    """把 "D:\\data:/data" 或 "/src:/dst" 拆成 (宿主路径, 沙箱内路径)。"""
    match = re.match(r"^([A-Za-z]:[\\/][^:]*):(.*)$", spec)
    if match:
        host_raw, guest = match.group(1), match.group(2)
        host = to_wsl_path(Path(host_raw))
    else:
        host, sep, guest = spec.rpartition(":")
        if not sep or not host:
            raise SandboxError(f"卷参数格式应为 宿主目录:沙箱目录，收到 {spec!r}")
        if not guest.startswith("/"):
            raise SandboxError(f"沙箱内路径必须是绝对路径: {guest!r}")
        if not Path(host).exists() and not host.startswith("/"):
            raise SandboxError(f"宿主目录不存在: {host}")
    if not guest.startswith("/"):
        raise SandboxError(f"沙箱内路径必须是绝对路径: {guest!r}")
    return host, guest


def build_run_script(opts: argparse.Namespace, shell_mode: bool) -> str:
    volumes = []
    for spec in opts.volume:
        host, guest = parse_volume(spec)
        volumes.append(f'mkdir -p "$ROOT{guest}" 2>/dev/null || true\n'
                       f'mount --rbind {sh_quote(host)} "$ROOT{guest}" '
                       f'|| die "挂载卷失败: {host} -> {guest}"')
    dns = ('cp -f /etc/resolv.conf "$ROOT/etc/resolv.conf" 2>/dev/null || true'
           if opts.dns_refresh else "")
    body = _RUN_SCRIPT.replace("__VOLUMES__", "\n".join(filter(None, [dns, *volumes])))
    return body.replace("__EXEC__", _EXEC_SHELL if shell_mode else _EXEC_CAPTURE)


def write_log(command: str, stdout: str, stderr: str, code: int, seconds: float) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    seq = len(list(LOG_DIR.glob("*.log"))) + 1
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{seq:03d}.log"
    path = LOG_DIR / name
    path.write_text(
        f"# command : {command}\n"
        f"# time    : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"# exit    : {code}\n"
        f"# seconds : {seconds:.2f}\n"
        f"# root    : {ROOT_DIR}\n"
        f"# --- stdout ---\n{stdout}"
        f"# --- stderr ---\n{stderr}",
        encoding="utf-8")
    return path


def run_in_sandbox(opts: argparse.Namespace, command: str, shell_mode: bool = False,
                   timeout: float | None = None,
                   ) -> tuple[int, str, str, float]:
    """在沙箱里跑一条命令，返回 (退出码, stdout, stderr, 用时秒)。

    入口脚本用随机文件名，避免并发调用互相覆盖。
    """
    ensure_sandbox(opts)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    entry_name = f".dsh-entry-{os.urandom(4).hex()}.sh"
    entry_host = ROOT_DIR / "tmp" / entry_name
    entry_host.parent.mkdir(parents=True, exist_ok=True)
    entry_host.write_bytes(
        build_entry_script("" if shell_mode else command, opts.cwd, opts.env).encode("utf-8"))

    script = build_run_script(opts, shell_mode)
    args = [to_wsl_path(ROOT_DIR), entry_name]
    started = time.perf_counter()
    try:
        if shell_mode:
            run_control_script("_run.sh", script, opts, extra_args=args, interactive=True)
            return 0, "", "", time.perf_counter() - started
        try:
            proc = run_control_script("_run.sh", script, opts, extra_args=args,
                                      timeout=timeout if timeout is not None else opts.timeout)
        except subprocess.TimeoutExpired as exc:
            seconds = time.perf_counter() - started
            limit = timeout if timeout is not None else opts.timeout
            stderr = decode(exc.stderr) + (
                f"\n[bash.py] 命令超时（>{limit}s），已被终止；以上就是中断前拿到的输出\n")
            return 124, decode(exc.stdout), stderr, seconds
        seconds = time.perf_counter() - started
        stdout, stderr = decode(proc.stdout), decode(proc.stderr)
        if proc.returncode == INTERNAL_EXIT and "[bash.py]" in stderr:
            raise SandboxError(stderr.strip())
        return proc.returncode, stdout, stderr, seconds
    finally:
        entry_host.unlink(missing_ok=True)


def execute(opts: argparse.Namespace, command: str) -> int:
    code, stdout, stderr, seconds = run_in_sandbox(opts, command, shell_mode=opts.shell)
    if opts.shell:
        eprint(f"[bash.py] 已退出 Alpine 沙箱（改动都留在 {ROOT_DIR}）")
        return 0

    if not opts.quiet:
        sys.stdout.write(stdout)
        sys.stdout.flush()
        sys.stderr.write(stderr)
        sys.stderr.flush()

    log = write_log(command, stdout, stderr, code, seconds)
    if opts.json:
        print(json.dumps({"command": command, "exit_code": code,
                          "stdout": stdout, "stderr": stderr,
                          "duration_ms": round(seconds * 1000),
                          "root": str(ROOT_DIR),
                          "log": str(log)}, ensure_ascii=False))
    else:
        rel = log.relative_to(HERE) if log.is_relative_to(HERE) else log
        eprint(f"[bash.py] 退出码 {code} · 用时 {seconds:.2f}s · 沙箱根 {ROOT_DIR.name}/"
               f" · 日志 {rel}")
    return code


# --------------------------------------------------------------------------- 其它子命令

def show_info(opts: argparse.Namespace) -> int:
    if not sandbox_ready():
        print("沙箱尚未初始化。先运行: python bash.py --init")
        return 1
    meta = {}
    if META_PATH.exists():
        try:
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    used, files = dir_usage(ROOT_DIR)
    print(f"沙箱根目录 : {ROOT_DIR}   ← 这个目录就是沙箱里的 /，可直接编辑")
    print(f"占用/文件数: {human_size(used)} / {files} 个文件")
    print(f"创建时间   : {meta.get('created', '?')}")
    print(f"Alpine 版本: {meta.get('alpine_version', '?')}   宿主: {meta.get('host_distro', '?')}")
    print(f"apk 镜像   : {meta.get('apk_mirror', '?')}")
    print(f"供应标记   : {'已完整初始化' if MARKER_PATH.exists() else '缺少 .provisioned'}")
    logs = sorted(LOG_DIR.glob("*.log"))
    print(f"运行日志   : {len(logs)} 个 (最近: {logs[-1].name if logs else '无'})")
    if LEGACY_IMAGE.exists():
        print(f"注意       : 还存在旧版镜像 {LEGACY_IMAGE.name}（已不用，--reset --yes 可删除）")
    print("--- 沙箱内部实况 ---")
    probe = ("echo -n 'alpine-release: '; cat /etc/alpine-release; "
             "echo -n 'kernel        : '; uname -r; "
             "echo -n 'arch          : '; uname -m; "
             "echo -n 'uid           : '; id -u; "
             "echo -n 'apk mirror    : '; head -1 /etc/apk/repositories; "
             "echo -n 'packages      : '; (apk info 2>/dev/null | wc -l); "
             "echo -n 'rootfs usage  : '; df -h / | tail -1")
    return execute(opts, probe)


def reset_sandbox(opts: argparse.Namespace) -> int:
    targets = [path for path in (ROOT_DIR, LEGACY_IMAGE) if path.exists()]
    if not targets and not META_PATH.exists():
        print("沙箱不存在，无需清理。")
        return 0
    if not opts.yes:
        print("这会删除整个沙箱环境（含里面所有文件）:")
        for path in targets or [ROOT_DIR]:
            print(f"  - {path}")
        print("确认请执行: python bash.py --reset --yes")
        return 2
    for path in targets:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        print(f"已删除 {path}")
    META_PATH.unlink(missing_ok=True)
    MARKER_PATH.unlink(missing_ok=True)
    for leftover in STAGING_DIR.glob("*"):
        leftover.unlink(missing_ok=True)
    print(f'下次运行 python bash.py "..." 会自动重建一个干净的 Alpine 环境。')
    return 0


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bash.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="在 Alpine Linux 沙箱里执行命令；沙箱根就是项目里的 sandbox/data/ 目录。",
        epilog="示例:\n"
               "  python bash.py \"cat /etc/alpine-release\"\n"
               "  python bash.py \"apk add --no-cache curl && curl -sI https://example.com | head -1\"\n"
               "  python bash.py --shell\n"
               "  python bash.py -v D:\\\\data:/data \"ls -l /data\"\n")
    parser.add_argument("cmd", nargs="*", help="要在沙箱里执行的命令（建议整体加引号）")
    parser.add_argument("--init", action="store_true", help="只初始化沙箱环境")
    parser.add_argument("-s", "--shell", action="store_true", help="进入交互式 Alpine shell")
    parser.add_argument("--info", action="store_true", help="显示沙箱环境信息")
    parser.add_argument("--reset", action="store_true", help="删除沙箱环境（需 --yes）")
    parser.add_argument("--yes", action="store_true", help="确认危险操作")
    parser.add_argument("--alpine-version", default=ALPINE_VERSION, help="Alpine 版本")
    parser.add_argument("--mirror", default="", metavar="URL",
                        help="指定 apk 镜像站（默认初始化时自动测速选择最快的）")
    parser.add_argument("--no-mirror-probe", dest="mirror_probe", action="store_false",
                        help="不测速，直接用 Alpine 官方镜像站")
    parser.add_argument("--distro", default=os.environ.get("DSH_WSL_DISTRO", ""),
                        help="承载沙箱的 WSL 发行版（默认 WSL 默认发行版）")
    parser.add_argument("--cwd", default="/root", help="命令在沙箱内的工作目录，默认 /root")
    parser.add_argument("-e", "--env", action="append", default=[], metavar="K=V",
                        help="注入环境变量，可重复")
    parser.add_argument("-v", "--volume", action="append", default=[], metavar="宿主:沙箱",
                        help="把宿主目录挂载进沙箱，可重复")
    parser.add_argument("--timeout", type=float, default=None, metavar="秒",
                        help="命令超时时间")
    parser.add_argument("--no-dns-refresh", dest="dns_refresh", action="store_false",
                        help="不刷新沙箱内的 /etc/resolv.conf")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("-q", "--quiet", action="store_true", help="不打印命令输出")
    return parser


def main(argv: list[str] | None = None) -> int:
    opts = build_parser().parse_args(argv)
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    with SandboxLock(SANDBOX_DIR / ".lock"):
        if opts.reset:
            return reset_sandbox(opts)
        if opts.init:
            init_sandbox(opts)
            return 0
        if opts.info:
            return show_info(opts)
        if opts.shell:
            return execute(opts, "")
        command = " ".join(opts.cmd).strip()
        if not command:
            build_parser().print_help()
            return 2
        return execute(opts, command)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SandboxError as exc:
        eprint(f"[bash.py] 错误: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        eprint("\n[bash.py] 已中断")
        sys.exit(130)
