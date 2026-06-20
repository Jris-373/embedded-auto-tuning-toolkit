# Bash→Python Migration: flash.sh & loop_runner.sh

**Version:** v0.2.0  
**Date:** 2026-06-19  
**Status:** Approved

---

## 1. 目标

将 `tools/flash.sh` 和 `tools/loop_runner.sh` 改写为 `flash.py` 和 `loop_runner.py`，实现 Windows、Linux、macOS 三平台原生调用。同时重构 `lib/backends.py` 中的 `FlashBackend` ABC，新增 `lib/commands.py`（CommandResult + CommandRunner）和 `lib/builders.py`（BuildRunner），并补齐 `tools/tests/`。

### 约束

- Python 3.10+，不新增 pip 依赖（已有 pyserial + pyyaml）
- 所有子进程使用参数数组 + `shell=False`
- 工具路径优先读配置字段 `executable`，其次 `shutil.which()`，失败则 `FileNotFoundError`
- 命令构造（`_*_args()` 纯函数）与命令执行（`CommandRunner.run()`）分离
- `validate.py` 失败固定映射为退出码 5
- `monitor_to_csv()` 显式接收 `require_boot_done: bool` 参数，不依赖全局 `args`

---

## 2. 文件变更清单

```
新增  tools/flash.py
新增  tools/loop_runner.py
新增  tools/lib/commands.py
新增  tools/lib/builders.py
新增  tools/tests/test_commands.py
新增  tools/tests/test_backends.py
新增  tools/tests/test_builders.py
新增  tools/tests/test_flash.py
新增  tools/tests/test_loop.py
新增  tools/tests/test_monitor.py
新增  tools/tests/test_validate.py

修改  tools/lib/backends.py         (重构 FlashBackend ABC + 5 个具体后端)
修改  tools/validate.py             (loop_runner.sh→loop_runner.py 引用更新)
修改  tools/config.yaml             (build段迁移+flash段地址/extra_args数组化+project.root="..")
修改  tools/monitor.py              (新增 --require-boot-done；monitor_to_csv 参数化)
修改  tools/README.md               (更新调用示例)

删除  tools/flash.sh
删除  tools/loop_runner.sh
```

---

## 3. `lib/commands.py` — CommandResult + CommandRunner

```python
@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    log_path: Path | None
    duration_ms: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

class CommandTimeoutError(TimeoutError): ...

class CommandRunner:
    """可注入的命令执行器，测试时注入 mock 避免真实 subprocess。"""
    def __init__(self, timeout_s: float = 120.0): ...
    def run(self, args: List[str], cwd: Path,
            log_path: Path | None = None) -> CommandResult:
        """进程启动失败抛 FileNotFoundError；超时抛 CommandTimeoutError。"""
```

进程启动失败（找不到可执行文件）和配置无效直接抛异常，不返回 `CommandResult`。

---

## 4. `lib/builders.py` — BuildRunner

### ABC

```python
class BuildRunner(ABC):
    def build(self, config: dict, project_root: Path) -> CommandResult: ...
    def clean(self, config: dict, project_root: Path) -> Optional[CommandResult]: ...
```

### 具体类

| 类 | build.system | 实现 |
|----|-------------|------|
| MakeBuilder | make | `make <target> -j<parallel> <flags>` |
| CMakeBuilder | cmake | `cmake --build <directory> --target <target> [--parallel N] [-- <flags>]` |
| CustomBuilder | custom | 执行 `build.command` 数组；命令必须非空 |

### 关键约束

- CommandRunner 通过构造函数注入
- `project_root` 由调用方显式传入，不重复解析配置中的相对路径
- CMake 仅在 `flags` 非空时追加 `--`
- 工具解析：`build.make_executable` / `build.cmake_executable` → `shutil.which()` → Windows `mingw32-make.exe` → `FileNotFoundError`（不自动选 nmake）
- Custom 模式 `build.command` 为空或非数组 → `ValueError`；跳过构建由 flash.py 的 `--skip-build` 在 Builder 调用之前处理

---

## 5. config.yaml 变更

### build 段迁移

```yaml
# 旧（删除）
build:
  flags: "-j$(nproc)"
  binary: "build/firmware.bin"

# 新
build:
  system: make
  directory: build
  parallel: 8
  flags: []
  target: all
  binary: "build/firmware.bin"
  clean_first: false
```

### flash 段扩充

```yaml
flash:
  backend: openocd
  verify: true
  allow_unverified: false
  allow_no_reset: false
  openocd:
    executable: null
    interface: "interface/stlink-v2.cfg"
    target: "target/stm32f4x.cfg"
    address: "0x08000000"
    extra_args: ["-c", "adapter speed 4000"]
  jlink:
    executable: null
    device: "STM32F407VG"
    interface: "SWD"
    speed: 4000
    address: "0x08000000"
  stlink:
    executable: null
    address: "0x08000000"
  dfu:
    executable: null
    vid: "0483"
    pid: "df11"
    alt: 0
  custom:
    flash_command: []
```

### project.root

```yaml
project:
  root: ".."       # 从 tools/ 向上到项目根（原为 "."）
```

---

## 6. `lib/backends.py` — FlashBackend 重构

### FlashContext（定义于 `lib/backends.py`）

```python
@dataclass(frozen=True)
class FlashContext:
    round_number: int
    project_root: Path
    log_dir: Path

    def log_path(self, stem: str) -> Path:
        """生成带轮次前缀的日志路径，如 flash_r01_openocd.log。"""
        return self.log_dir / f"flash_r{self.round_number:02d}_{stem}.log"
```

### FlashBackend ABC

```python
class VerifyMode(Enum):
    SEPARATE = "separate"
    INLINE   = "inline"
    NONE     = "none"

class ResetMode(Enum):
    SUPPORTED = "supported"
    INLINE    = "inline"
    NONE      = "none"

class UnsupportedOperationError(RuntimeError): ...

class FlashBackend(ABC):
    verify_mode: VerifyMode    # 类属性
    reset_mode: ResetMode      # 类属性

    def __init__(self, executable: str, config: dict,
                 runner: CommandRunner, context: FlashContext):
        self._exe = executable
        self._cfg = config
        self._runner = runner
        self._ctx = context

    @abstractmethod
    def flash(self, binary: Path) -> CommandResult: ...

    def verify(self, binary: Path) -> CommandResult:
        raise UnsupportedOperationError(...)

    def reset(self) -> CommandResult:
        raise UnsupportedOperationError(...)
```

### 后端能力表

| 后端 | verify_mode | reset_mode | 说明 |
|------|-------------|------------|------|
| OpenOCD | INLINE | INLINE | `program ... verify reset exit` |
| J-Link | INLINE | INLINE | 脚本 `loadfile/loadbin` + `r; g` |
| ST-Link | SEPARATE | SUPPORTED | `st-flash write` / `st-flash verify` / `st-flash reset` |
| DFU | NONE | NONE | dfu-util 无校验原语；detach ≠ 复位 |
| Custom | NONE | NONE | 由 `flash_command` 定义 |

### 工厂函数

```python
def create_flash_backend(config: dict, runner: CommandRunner,
                         context: FlashContext) -> FlashBackend:
```

工具解析：`flash.<backend>.executable` → `shutil.which()` → Windows 变体 → `FileNotFoundError`。

### BIN 地址要求

- ELF/AXF/HEX：镜像自带地址
- BIN：`flash.<backend>.address` 必须配置。仅 openocd/jlink/stlink 受此约束，DFU 和 Custom 不检查

### J-Link 路径转义

```python
def _jlink_validate_and_quote(path: str) -> str:
    if '"' in path or '\n' in path or '\r' in path:
        raise ValueError(f"Path contains unsafe characters: {path}")
    return f'"{path}"'
```

`loadbin` 语法：`loadbin "<path>", <address>`（逗号分隔）。

### OpenOCD Tcl 路径转义

```python
def _tcl_escape(path: str) -> str:
    return "{" + path.replace("{", "\\{").replace("}", "\\}") + "}"
```

`extra_args` 位于 `-f` 之后、`-c program ...` 之前。

### Custom 占位符

`flash_command` 支持 `{binary}` 和 `{project_root}` 占位符。未识别的 `{...}` 抛 `ValueError`。空数组抛 `ValueError`。

### verify_flash() 逻辑（flash.py 内）

```python
def verify_flash(backend, binary, config) -> bool:
    # 1. flash.verify: false → 跳过
    if not config["flash"].get("verify", True):
        return True

    # 2. INLINE → flash() 已完成
    if backend.verify_mode == VerifyMode.INLINE:
        return True

    # 3. SEPARATE → 调用 backend.verify()
    if backend.verify_mode == VerifyMode.SEPARATE:
        try:
            result = backend.verify(binary)
            if result.ok:
                return True
            print("Verify FAILED")
            return False
        except UnsupportedOperationError:
            if config["flash"].get("allow_unverified", False):
                print("WARNING: verify unsupported, proceeding")
                return True
            raise

    # 4. NONE → 检查 allow_unverified
    if backend.verify_mode == VerifyMode.NONE:
        if config["flash"].get("allow_unverified", False):
            print("WARNING: no verification available")
            return True
        raise UnsupportedOperationError(
            "Set flash.allow_unverified: true to proceed"
        )
```

---

## 7. flash.py — 构建+烧录+校验+等待

```
flash.py [--config <path>] [--skip-build] [--skip-verify]
          [--skip-boot-wait] [--round N]

Exit codes:
  0 — 成功
  1 — 构建/clean 失败
  2 — 烧录失败（含内联校验失败）
  3 — 独立校验失败
  4 — 启动超时
  5 — 配置/前置错误
```

### 流程

```
1. 解析参数 → 所有路径 resolve() 为绝对路径
2. 应用 CLI 覆盖（--skip-verify → config["flash"]["verify"] = False）
3. Build: clean_first → builder.clean()（result is not None and not result.ok → exit 1）
          → builder.build()（not ok → exit 1）
4. Binary metadata → log
5. Flash: backend.flash(binary)（not ok → exit 2）
6. Verify: verify_flash(...)（False → exit 3, UnsupportedOperationError → exit 5）
7. Reset: INLINE 跳过 / SUPPORTED 调用 reset() / NONE 检查 allow_no_reset
8. Boot wait: --skip-boot-wait 跳过，否则 wait_for_boot_done(...)
```

### 路径

```python
tool_dir = Path(__file__).resolve().parent
config_path = Path(args.config).resolve()
project_root = (config_path.parent / config["project"]["root"]).resolve()
binary = (project_root / config["build"]["binary"]).resolve()
log_dir = tool_dir / "logs"
```

### 异常与超时映射（flash.py）

| 异常 / 条件 | 退出码 |
|------------|--------|
| `ValueError`, `FileNotFoundError`, `yaml.YAMLError` | 5 |
| `CommandTimeoutError`（build/clean 阶段） | 1 |
| `CommandTimeoutError`（flash/reset 阶段） | 2 |
| `CommandTimeoutError`（其他阶段） | 4 |

---

## 8. loop_runner.py — 闭环编排

```
loop_runner.py [--config <path>] [--max-rounds N]

Exit codes:
  0  — 成功（场景A收敛/场景B诊断/场景C正常结束）
  1  — 场景A达到最大轮次未收敛
  2  — 场景A停滞
  3  — 紧急停止
  4  — 步骤执行失败
  5  — 配置错误
```

### 场景检测（使用 CLI 覆盖后的 effective_max）

| 条件 | 场景 |
|------|------|
| 存在 `auto: true` 的 parameters | A |
| 有 variables 且 effective_max ≤ 3 | B |
| 有 variables 且 effective_max > 3 | C |
| 无 variables | 退出 5 |

### 主流程

```
0. Validate → exit 5 on failure（调用 run_step(..., failure_exit_code=5)）
Loop:
  1. Flash: flash.py --skip-boot-wait（retry_on=2 最多重试一次）
  2. Monitor: monitor.py --require-boot-done
  3. Analyze: analyze.py → load decision JSON
  4. Score tracking（best_score < ... - 0.001 才算改善）
  5. Termination:
     - emergency → exit 3
     - 场景A: convergence → exit 0 / stall → exit 2
     - 场景B: 打印诊断 → exit 0
     - 场景C: 不做收敛/停滞判断
  6. Action:
     - 场景A 且 round_num < effective_max: adjust.py
     - 场景A 最后一轮: 跳过 adjust
     - 场景B: 已退出
     - 场景C: 无操作
Max rounds → 场景A exit 1 / 场景C exit 0
```

### run_step() 签名

### 异常映射

```python
def main() -> int:
    try:
        return _run(args)
    except StepError as e:
        print(f"[loop] {e.step} failed: {e.message}")
        return e.exit_code   # validate→5, 其他→4
    except CommandTimeoutError as e:
        print(f"[loop] Timeout: {e}")
        return 4
    except (ValueError, FileNotFoundError, yaml.YAMLError) as e:
        print(f"[loop] Configuration error: {e}")
        return 5
    except KeyboardInterrupt:
        # 在 _run() 内捕获，生成报告后 return 130
```

### run_step() 超时支持

```python
def run_step(cmd: list, *, cwd: Path, retry_on: int | None = None,
             timeout_s: float = 300.0, failure_exit_code: int = 4) -> int:
    result = subprocess.run(cmd, cwd=cwd, timeout=timeout_s, check=False)
    ...
```

`subprocess.TimeoutExpired` → `CommandTimeoutError`。

### validate.py 退出码约束

`validate.py` 失败必须返回非零，`loop_runner.py` 遇到任何非零统一映射为退出码 5。

---

## 9. monitor.py 变更

### 新增参数

```
--require-boot-done   未收到 BOOT_DONE 时返回非零（默认仅警告）
```

### 函数签名

```python
def monitor_to_csv(cfg: dict, var_index: dict, round_num: int,
                   require_boot_done: bool = False) -> str:
```

不依赖全局 `args`。

### loop_runner.py 调用

```python
run_step([
    sys.executable, str(tool_dir / "monitor.py"),
    "--config", str(config_path),
    "--round", str(round_num),
    "--require-boot-done",
], cwd=project_root)
```

---

## 10. validate.py 变更

两处 `loop_runner.sh` 引用更新为 `loop_runner.py`：

- 文件头注释
- 错误提示文本

同时新增 BIN 地址校验：

```python
def check_bin_address(cfg: dict):
    binary = Path(cfg["build"]["binary"])
    if binary.suffix.lower() != ".bin":
        return
    backend = cfg["flash"]["backend"]
    if backend not in {"openocd", "jlink", "stlink"}:
        return
    addr = cfg["flash"].get(backend, {}).get("address")
    if not addr:
        ck.error(f"BIN image requires flash.{backend}.address")
```

---

## 11. 测试覆盖

| 文件 | 测试内容 |
|------|---------|
| `test_commands.py` | CommandRunner：超时抛异常、日志文件写入、cwd 正确传递、参数数组不含 shell |
| `test_backends.py` | 后端工厂选择（含 Windows 变体 fallback）、命令构造（各后端 `_*_args()` 输出）、校验/复位能力声明；J-Link Windows 空格路径用例 |
| `test_builders.py` | MakeBuilder/CMakeBuilder `_build_args()` 在各种 parallel/flags 组合下输出正确数组；CustomBuilder 空命令抛 ValueError |
| `test_flash.py` | `--skip-build`/`--skip-verify`/`--skip-boot-wait` 行为、reset 模式分支、各退出码映射 |
| `test_loop.py` | 场景检测逻辑、退出码映射（StepError→exit code、validate→5）、最后一轮不执行 adjust、收敛/停滞判定 |
| `test_monitor.py` | `--require-boot-done` 行为：未收到时返回非零 |
| `test_validate.py` | BIN 地址检查、`check_flash_backend` 含 custom、自身非零退出；loop_runner 将 validate 失败映射为 5 |
| J-Link 路径测试 | `loadbin "C:\\My Docs\\fw.bin", 0x08000000`、含双引号路径→ValueError、含换行路径→ValueError |

---

## 12. 不变文件

`firmware/tracepoint.h`, `lib/protocol.py`, `lib/analyzers.py`, `lib/adjusters.py`, `lib/sinks.py`, `analyze.py`, `adjust.py`, `simulate.py`

---

## 13. 规格自查

### Placeholder Scan
无 TODO / TBD 残留。

### 内部一致性
- CommandResult 仅由 CommandRunner.run() 构造，各后端不直接实例化
- FlashContext 由调用方创建并注入，后端不自行推断轮次
- 所有路径在 flash.py/loop_runner.py 入口处 resolve()，内部不再次解析
- 退出码映射在 main() 最外层统一处理，StepError 不在中间层调用 sys.exit()

### 作用域
聚焦两个 bash→python 迁移 + FlashBackend 重构，不涉及 analyze/adjust/simulate 逻辑变更。

### 歧义检查
- INLINE vs SEPARATE vs NONE 由类属性声明，不运行时推断
- "工具不支持"（UnsupportedOperationError）与"校验失败"（CommandResult.returncode != 0）严格区分
- 场景 B/C 的边界：effective_max ≤ 3 为 B，否则 C；使用 CLI 覆盖后的值
