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

### 已知限制

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

### Limitations

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
