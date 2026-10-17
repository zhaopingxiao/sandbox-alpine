# Alpine Sandbox Runner

[中文说明](#中文说明) · [English](#english)

Single-file Python script that runs commands inside an **Alpine Linux sandbox** on Windows,
where the sandbox root is just an ordinary directory in your project — `sandbox/data/`.
No disk image, no loop device, no `mkfs`.

单文件 Python 脚本：在 **Alpine Linux 沙箱**里执行命令并取回输出。沙箱的根就是项目里的一个
普通目录 `sandbox/data/` —— 没有镜像文件、没有 loop 挂载、不用 `mkfs`。

---

## 中文说明

### 它是什么

* 沙箱里写的文件，**立刻出现在** `sandbox\data\root\...`，资源管理器/编辑器直接能看、能改；
* 反过来你在 `sandbox\data\` 里放文件，沙箱内部下一条命令就能看到；
* 目录是持久的：`apk` 装的包、写的文件，下次运行依然在；
* 沙箱内跑在私有 **mount + PID 命名空间**里，用 `pivot_root` 把沙箱根变成真正的 `/`
  —— 宿主文件系统在沙箱里完全看不见，退出后挂载自动回收，宿主不留痕迹。

```
D:\alpine-sandbox> python bash.py "cat /etc/alpine-release; id -u"
3.24.2
0
[bash.py] 退出码 0 · 用时 0.19s · 沙箱根 data/ · 日志 sandbox\runs\20261017-071853-011.log

D:\alpine-sandbox> python bash.py "echo hi > /root/a.txt"
D:\alpine-sandbox> type sandbox\data\root\a.txt
hi
```

### 环境要求

* Windows 10/11 + **WSL2**（任一发行版作为宿主即可，Debian 默认满足）
* 宿主里要有 `tar` / `unshare` / `mount` / `chroot` / `pivot_root`
* Python 3.8+（只用标准库，无第三方依赖）

### 快速开始

```powershell
python bash.py --init                                  # 初始化沙箱（幂等；首次运行也会自动执行）
python bash.py "cat /etc/alpine-release"                # 执行命令并拿到输出
python bash.py "echo 你好 > /root/a.txt"                 # 写文件（持久，宿主机可见）
python bash.py "apk add --no-cache curl && curl -sI https://example.com | head -1"
python bash.py --shell                                  # 交互式 Alpine shell
python bash.py --info                                   # 查看沙箱环境实况
python bash.py --reset --yes                            # 清空沙箱（下次运行自动重建）
```

### 参数

| 参数 | 说明 |
| --- | --- |
| `"命令"` | 在沙箱里执行的命令（建议整体加双引号） |
| `--init` | 只初始化沙箱环境（幂等） |
| `-s, --shell` | 进入交互式 Alpine shell（`exit` 退出，改动直接落在 `sandbox/data/`） |
| `--info` | 显示沙箱根目录、占用、文件数、版本、镜像站 |
| `--reset --yes` | 删除 `sandbox/data/`（下次运行自动重建） |
| `-e K=V` | 向沙箱注入环境变量，可重复 |
| `-v 宿主:沙箱` | 把宿主目录挂进沙箱，如 `-v D:\data:/data` |
| `--cwd DIR` | 命令的工作目录，默认 `/root` |
| `--timeout 秒` | 命令超时（超时会把中断前的输出原样打印出来） |
| `--json` | 以 JSON 输出（`exit_code` / `stdout` / `stderr` / 用时 / 沙箱根 / 日志路径） |
| `-q, --quiet` | 不打印命令输出，只要退出码 |
| `--alpine-version` / `--mirror` / `--distro` | Alpine 版本 / apk 镜像站 / 承载沙箱的 WSL 发行版 |

退出码即沙箱内命令的退出码：`python bash.py "exit 42"` → 本进程退出码 42；超时返回 124。

### 长任务与超时（重要）

* **默认不限时**：不传 `--timeout` 时 `bash.py` 不会给你加任何上限，前台任务能跑多久跑多久
  （实测 90 秒任务正常跑完、退出码 0）。适合编译、跑测试、下载大文件。
* **传 `--timeout N` 才会停**：到点强制终止，退出码 **124**，并且**已经打印的输出会保留**
  （实测 5 秒任务被 4 秒掐断时，前 4 个 tick 都在）。超时是**真杀进程**，实测宿主里没有残留。
* **上游超时也算**：如果调用方（终端、CI、Agent 工具）自己设了更短的超时，那是上游掐断，
  不是 `bash.py` 的限制。
* **不能后台常驻**：每次调用都是独立的 mount + PID 命名空间，`python bash.py` 一退出，
  里面的进程全部被内核杀掉。`nohup` / `&` / `setsid` / `systemd` / 守护进程都活不下来，
  下一次调用也看不到（实测：宿主里 0 个、下一次调用里 0 个）。
  持久化的是**文件**（`sandbox/data/`），不是进程。
* 需要"跨多次调用推进"的长任务：把进度写进文件，分几次调用继续跑；
  真要常驻服务，别放在沙箱里 —— 用宿主 WSL（`wsl -u root -- nohup ... &`）、Windows 任务计划，
  或自己加一个 `--bg` 模式。
* 长任务建议边跑边输出（`echo` / `--verbose`）：输出是实时透传的，长时间无输出容易被误以为卡死。

### 工作原理

```
Windows 侧 (bash.py)                        WSL2 侧（宿主发行版，如 Debian）
─────────────────────                       ──────────────────────────────
1. 下载 alpine-minirootfs-*.tar.gz
   (Windows 网络通常比 WSL 快)
2. sandbox/data/                        ──▶  tar 解压成普通目录
   （就是沙箱的 /，可直接编辑）               + 写 /etc/apk/repositories、resolv.conf…
3. 把命令写成 <root>/tmp/.dsh-entry-*.sh ─┐
                                          └─▶  unshare -mpf --mount-proc  ← 私有 mount+PID 命名空间
                                               mount --bind 沙箱根到自身（先 bind，再挂子挂载）
                                               mount proc / sysfs / rbind dev（+ -v 指定的卷）
                                               pivot_root 把沙箱根变成真正的 /，卸载旧根
                                               执行 entry.sh（失败则退回 chroot）
                        ┌─────────────────  取回 stdout/stderr/退出码
4. 打印输出 + 写日志   ◀┘                    namespace 退出 → 挂载自动回收，宿主无残留
```

### 目录结构

```
alpine-sandbox/
├─ bash.py                  # 全部代码（单文件，无第三方依赖）
├─ README.md
└─ sandbox/                 # 运行时生成，--init 可完全重建（已在 .gitignore 中）
   ├─ data/                 # ← 沙箱根文件系统：这个目录就是沙箱里的 "/"
   │   ├─ bin/ etc/ lib/ usr/ var/ …
   │   └─ root/             #    命令默认工作目录（沙箱内 /root）
   ├─ downloads/            # 缓存的 alpine-minirootfs tarball
   ├─ staging/              # 每次运行生成的控制脚本
   ├─ runs/*.log            # 每次运行的完整记录（命令 + 输出 + 退出码 + 用时）
   ├─ sandbox.json          # 版本 / 镜像站 / 创建时间
   └─ .provisioned          # 供应完成标记
```

### 几个关键设计决策（都是实测踩出来的）

1. **目录式沙箱，不用 ext4 镜像。** 早先版本把 Alpine 装进 2GB 稀疏 ext4 镜像再 loop 挂载，
   好处是权限/属主是真的；代价是要 `mkfs`、要管 loop 设备、Windows 侧看不见里面、容量要预分配。
   改成普通目录后都省了，`apk` 依然正常（实测 `apk add --no-cache python3` 可用）。
   *drvfs 上 `chmod 700` 会变 `777`、`chown` 是空操作，但沙箱内是 root，busybox 和 apk 都不在意。*

2. **`mount --bind` 必须在挂 proc/sys/dev 之前做。** 普通 `--bind` 不带子挂载，先挂 proc 再 bind
   整个根会把 `/proc` 遮成空目录（现象：沙箱里 `df`、`mount` 报 "no /proc/mounts"）。

3. **`pivot_root` 要在当前 shell 里做**，放进子 shell 只会改挂载表不改进程 root，`/proc` 会错乱。

4. **重新执行的自己必须看到 `DSH_NS`**：用 `DSH_NS=1`（普通赋值，未 export）子进程看不到，
   会无限递归 fork 直到超时。

5. **apk 镜像站自动测速。** 本机 WSL 到 Alpine 官方 CDN 只有约 **11 KB/s**（`apk add curl` 实测
   **607 秒**）；初始化时会在沙箱内实测几个镜像站，选最快的写入 `/etc/apk/repositories`，
   换国内镜像后同样操作只要 **1~2 秒**。测速请求需伪装成 `apk-tools` 的 UA（中科大/清华会 403 掉
   wget 默认 UA）。

#### 常驻服务（`--service-*`）

普通执行用的是 PID 命名空间，`python bash.py` 一退出里面所有进程都会被内核杀掉 —— 所以
**跑服务要用单独的一套指令**。它会把服务放进自己的 mount 命名空间，并交给一个独立的
`wsl.exe` 进程在前台持有，所以 `python bash.py` 退出后服务继续跑：

```powershell
# 起一个常驻 HTTP 服务（Alpine 的 busybox 没编 httpd，先 apk add python3）
python bash.py "apk add --no-cache python3"
python bash.py --service-start web "python3 -m http.server 8080 --directory /root"
#   → 服务 web 已启动（pid 871，持有进程 wsl.exe 14608）
#   然后从 Windows 直接访问 http://localhost:8080/index.html 就行

python bash.py --service-list                # 看所有服务的状态 / pid / 启动时间
python bash.py --service-logs web            # 看服务最近输出（也可 --lines 200）
python bash.py --service-stop web            # 停服务（先杀进程组，必要时杀掉持有进程）
python bash.py --service-start web "..." --force   # 覆盖同名正在运行的服务
```

* 服务日志：`sandbox/services/<名字>/service.log`；pid：同目录下 `pid` / `winpid`。
* 服务的 `/proc`、`/sys`、`/dev` 只在它自己的 mount 命名空间里，宿主挂载表不受影响。
* 服务看到的文件系统就是 `sandbox/data/`，和普通命令完全一致。
* **限制**：`wsl --shutdown`、注销 WSL、或杀掉那个持有进程，都会结束服务；服务看不到
  PID 隔离（它和宿主 WSL 共享 PID 命名空间）；`--reset` 会先停掉正在运行的服务再删目录。

### 关于 docker

**沙箱里跑不了 dockerd**（也不是加个参数能解决的）：daemon 需要 cgroups、iptables、
自己的 init、可写的 `/sys`，Restricted 环境下还要特权 —— 而这里只是一个 chroot +
mount 命名空间。想要"容器"能力，按需要选：

| 需求 | 做法 |
| --- | --- |
| 只是想跑容器 | Windows 上装 Docker Desktop（最省事） |
| 想在 WSL 里有一套完整服务管理 | `wsl --import` 一个 Alpine 发行版，开 systemd/openrc，在那里当正常 Linux 用 |
| 沙箱里需要 `docker` 命令 | 在**宿主 WSL** 装 docker daemon，把 `/var/run/docker.sock` 用 `-v` 挂进沙箱，沙箱里只装 docker CLI 去连宿主 daemon（CI 常见做法） |
| 只是要"一直跑着"的用户态进程 | 就用上面的 `--service-start`（http/worker/定时循环都行） |

## 已知限制

* **不是安全隔离**：沙箱与宿主共享内核和网络（有 mount + PID 命名空间，没有用户/网络命名空间）。
  跑不受信任的代码请用容器或 `wsl --import` 独立发行版。
* 沙箱内是 root，默认工作目录 `/root`；`--cwd` 可改。
* 磁盘占用就是 `sandbox/data/` 的真实大小（Alpine 最小系统约 8 MB）。
* 不要并发运行多个 `bash.py`（有 `sandbox/.lock` 保护，会直接拒绝并提示）。

### 故障排查

| 现象 | 处理 |
| --- | --- |
| 找不到 `wsl.exe` | 以管理员运行 `wsl --install -d Debian` |
| 初始化报缺工具 | 在宿主发行版补上 `tar/unshare/mount/chroot`（Debian 自带） |
| 沙箱里 `/proc` 空、`df` 报错 | `--reset --yes` 重建后再试（通常是手工改过挂载顺序） |
| `apk` 很慢 | `python bash.py --mirror https://mirrors.ustc.edu.cn/alpine` |
| 锁文件残留 | 确认无其它进程后删除 `sandbox\.lock` |

---

## English

### What it is

* Files created inside the sandbox **show up immediately** under `sandbox\data\root\...` —
  you can open, edit and delete them with any Windows tool;
* the other direction works too: drop a file into `sandbox\data\`, and the very next command
  inside the sandbox sees it;
* the directory is persistent — packages installed with `apk` and files you write survive
  between runs;
* each run happens inside a private **mount + PID namespace**, and the sandbox root is turned
  into a real `/` with `pivot_root`, so the host filesystem is invisible from inside and all
  mounts are reclaimed when the namespace exits — nothing is left behind on the host.

```
D:\alpine-sandbox> python bash.py "cat /etc/alpine-release; id -u"
3.24.2
0
[bash.py] exit 0 · 0.19s · root data/ · log sandbox\runs\20261017-071853-011.log

D:\alpine-sandbox> python bash.py "echo hi > /root/a.txt"
D:\alpine-sandbox> type sandbox\data\root\a.txt
hi
```

### Requirements

* Windows 10/11 with **WSL2** (any distro as the host works; Debian is fine out of the box)
* `tar` / `unshare` / `mount` / `chroot` / `pivot_root` available inside that distro
* Python 3.8+ (standard library only, no third-party packages)

### Quick start

```powershell
python bash.py --init                                   # initialise the sandbox (idempotent, also runs on demand)
python bash.py "cat /etc/alpine-release"                 # run a command and capture its output
python bash.py "echo hello > /root/a.txt"                # write a file (persistent, visible from Windows)
python bash.py "apk add --no-cache curl && curl -sI https://example.com | head -1"
python bash.py --shell                                   # interactive Alpine shell
python bash.py --info                                    # show sandbox details
python bash.py --reset --yes                             # wipe the sandbox (auto-rebuilt on next run)
```

### Options

| Option | Description |
| --- | --- |
| `"command"` | Command to run inside the sandbox (quote the whole thing) |
| `--init` | Only initialise the sandbox (idempotent) |
| `-s, --shell` | Interactive Alpine shell (`exit` to leave; changes land in `sandbox/data/`) |
| `--info` | Show sandbox root, disk usage, file count, versions, apk mirror |
| `--reset --yes` | Delete `sandbox/data/` (rebuilt automatically on the next run) |
| `-e K=V` | Inject an environment variable (repeatable) |
| `-v host:sandbox` | Bind-mount a host directory into the sandbox, e.g. `-v D:\data:/data` |
| `--cwd DIR` | Working directory for the command (default `/root`) |
| `--timeout seconds` | Command timeout (partial output is still printed) |
| `--json` | Emit JSON (`exit_code` / `stdout` / `stderr` / duration / root / log path) |
| `-q, --quiet` | Suppress command output, only keep the exit code |
| `--alpine-version` / `--mirror` / `--distro` | Alpine version / apk mirror / host WSL distro |

The process exit code is the command's exit code: `python bash.py "exit 42"` exits 42, and a
timeout returns 124.

### Long-running tasks & timeouts

* **No limit by default**: without `--timeout`, `bash.py` imposes no cap at all — a foreground
  task runs as long as it needs (verified with a 90-second job, exit code 0). Good for builds,
  test suites, big downloads.
* **`--timeout N` is opt-in**: the command is killed at N seconds, the exit code becomes **124**,
  and **output printed so far is preserved** (a 5-second job cut at 4 s still shows the first four
  ticks). The kill is real — no orphan processes are left on the host.
* **Upstream timeouts also count**: a shorter timeout in your terminal, CI job or agent tool will
  cut it off; that is not `bash.py`'s limit.
* **No background daemons**: every call gets its own mount + PID namespace, and when
  `python bash.py` exits the kernel kills everything inside it. `nohup` / `&` / `setsid` /
  `systemd` / daemons do not survive and are invisible to the next call (verified: 0 processes on
  the host, 0 in the following call). What persists is **files** (`sandbox/data/`), not processes.
* To advance a long job across calls, keep progress in files and resume; for a real always-on
  service, run it outside the sandbox (host WSL with `nohup`, Windows Task Scheduler), or add a
  `--bg` mode.
* Prefer tasks that emit progress: output is streamed live, and long silences look like a hang.

### How it works

```
Windows (bash.py)                           WSL2 host distro (e.g. Debian)
─────────────────                           ──────────────────────────────
1. download alpine-minirootfs-*.tar.gz
   (Windows networking is usually faster than WSL's)
2. sandbox/data/                        ──▶  extract it as a plain directory
   (this IS the sandbox /)                    + write /etc/apk/repositories, resolv.conf…
3. write the command to <root>/tmp/.dsh-entry-*.sh ─┐
                                                    └─▶ unshare -mpf --mount-proc  ← private mount+PID ns
                                                        mount --bind the root onto itself (bind BEFORE child mounts)
                                                        mount proc / sysfs / rbind dev (+ volumes from -v)
                                                        pivot_root: root becomes a real /, old root detached
                                                        run entry.sh (falls back to chroot)
                              ┌──────────────────────  collect stdout/stderr/exit code
4. print output + write log   ◀┘                       namespace exits → all mounts reclaimed, host untouched
```

### Layout

```
alpine-sandbox/
├─ bash.py                  # the whole tool (single file, stdlib only)
├─ README.md
└─ sandbox/                 # generated at runtime, fully rebuildable via --init (git-ignored)
   ├─ data/                 # ← sandbox root filesystem: this directory is the sandbox's "/"
   │   ├─ bin/ etc/ lib/ usr/ var/ …
   │   └─ root/             #    default working directory (sandbox /root)
   ├─ downloads/            # cached alpine-minirootfs tarball
   ├─ staging/              # control scripts generated per run
   ├─ runs/*.log            # per-run log (command + output + exit code + duration)
   ├─ sandbox.json          # version / mirror / creation time
   └─ .provisioned          # provisioning marker
```

### Design notes (each one was a real bug at some point)

1. **Directory sandbox instead of an ext4 image.** An earlier version kept Alpine inside a 2 GB
   sparse ext4 image loop-mounted into a mount namespace: real permissions and ownership, but it
   needed `mkfs`, loop-device bookkeeping, a preallocated size, and was invisible from Windows.
   A plain directory removes all of that and `apk` still works (`apk add --no-cache python3` verified).
   *On drvfs `chmod 700` becomes `777` and `chown` is a no-op — irrelevant here, since the sandbox
   runs as root and neither busybox nor apk cares.*

2. **`mount --bind` the root *before* mounting proc/sys/dev.** A plain `--bind` does not carry child
   mounts, so binding the root after mounting `/proc` hides it and leaves an empty `/proc`
   (symptom: `df` and `mount` inside the sandbox complain about missing `/proc/mounts`).

3. **`pivot_root` must run in the current shell.** Inside a subshell it only rewrites the mount table
   without changing the process root, which leaves `/proc` in a broken state.

4. **The re-executed script has to see `DSH_NS`.** `DSH_NS=1` without `export` is invisible to the
   child process, which then recurses into `unshare` forever until the timeout fires.

5. **apk mirrors are speed-tested automatically.** From this machine WSL reaches the official Alpine
   CDN at roughly **11 KB/s** (`apk add curl` took **607 s**). Initialisation therefore benchmarks a
   few mirrors from inside the sandbox and writes the fastest into `/etc/apk/repositories`, which
   brought the same command down to **1–2 s**. The probe must send an `apk-tools` User-Agent —
   USTC and TUNA answer `403` to wget's default UA.

#### Persistent services (`--service-*`)

Normal execution runs inside a PID namespace, so when `python bash.py` exits the kernel kills
everything in it. **Services therefore need their own commands**: the service runs in its own
mount namespace and is held by a **detached `wsl.exe` process**, so it keeps running after
`python bash.py` returns.

```powershell
# Alpine's busybox has no httpd applet, so install python3 first
python bash.py "apk add --no-cache python3"
python bash.py --service-start web "python3 -m http.server 8080 --directory /root"
#   → service web started (pid 871, holder wsl.exe 14608)
#   now open http://localhost:8080/index.html from Windows

python bash.py --service-list                # status / pid / start time of every service
python bash.py --service-logs web            # recent output (also --lines 200)
python bash.py --service-stop web            # stop (process group first, holder if needed)
python bash.py --service-start web "..." --force   # replace a running service of the same name
```

* Logs: `sandbox/services/<name>/service.log`; pids: `pid` and `winpid` in the same directory.
* `/proc`, `/sys` and `/dev` are mounted **only in the service's private mount namespace** —
  the host mount table is never touched. The service sees `sandbox/data/` as its root, exactly
  like a normal command.
* **Limits**: `wsl --shutdown`, unregistering WSL, or killing the holder process ends the
  service; services share the host PID namespace (no PID isolation); `--reset` stops running
  services before deleting the directory.

### What about Docker?

**You cannot run `dockerd` inside this sandbox**, and no flag will change that: the daemon needs
cgroups, iptables, its own init, a writable `/sys` and (typically) privileges, while this is a
chroot plus a mount namespace. Depending on what you actually need:

| Need | Do this |
| --- | --- |
| Just run containers | Install Docker Desktop on Windows (simplest) |
| A full Linux with real service management | `wsl --import` an Alpine distro, enable systemd/openrc, use it as a normal Linux |
| `docker` CLI inside the sandbox | Run the daemon in the **host WSL**, bind `/var/run/docker.sock` in with `-v`, install only the CLI in the sandbox (common CI pattern) |
| "Always running" user-space process | Use `--service-start` above (http servers, workers, polling loops) |

## Limitations

* **Not a security boundary**: the sandbox shares the host kernel and network (mount + PID namespaces,
  but no user/network namespace). For untrusted code use a container or an imported WSL distro.
* The sandbox runs as root; the default working directory is `/root` (use `--cwd`).
* Disk usage is the real size of `sandbox/data/` (a minimal Alpine is about 8 MB).
* Do not run several `bash.py` processes at once — a `sandbox/.lock` guard rejects the second one.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `wsl.exe` not found | run `wsl --install -d Debian` as administrator |
| Init complains about missing tools | install `tar/unshare/mount/chroot` in the host distro (Debian has them) |
| Empty `/proc` or `df` errors inside the sandbox | `--reset --yes` and retry |
| `apk` is slow | `python bash.py --mirror https://mirrors.ustc.edu.cn/alpine` |
| Stale lock file | make sure nothing else is running, then delete `sandbox\.lock` |
