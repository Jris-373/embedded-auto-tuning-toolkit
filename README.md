# Embedded Auto-Tuning Toolkit

嵌入式自动调优工具链——一套「固件端 `tracepoint.h` 埋点 + 主机端闭环分析」的通用工具集。你在固件中植入追踪桩，主机端通过串口二进制帧协议采集变量，然后根据配置的策略执行 **分析、决策、行动** 闭环。

---

## 1. 概述

### 一句话定义

一套零分配、I​​SR 安全的固件追踪桩 + 主机端闭环分析工具链，通过串口二进制帧协议实现 **监测、分析、调参** 全自动循环。

### 三类适用场景

| 场景 | 触发条件 | 闭环行为 | 示例 |
|------|---------|---------|------|
| **A. 参数寻优** | `config.yaml` 中有 `auto: true` 的 parameters | flash -> monitor -> analyze -> adjust -> 循环，直到变量收敛到预期范围 | PID 系数、滤波器截止频率、阈值门限、PWM 死区 |
| **B. 阶段诊断** | 有 variables 但无 `auto: true` 的 parameters，且 `loop.max_rounds` <= 3 | flash -> monitor -> analyze -> 打印诊断报告 -> 终止 | SD 卡烧录阶段定位、上电自检序列、外设初始化链 |
| **C. 长期监护** | 有 variables 但无 `auto: true` 的 parameters，且 `loop.max_rounds` 较大 | flash -> monitor -> analyze (周期性) -> 不修改代码，仅记录趋势和告警 | 电源纹波劣化、堆栈水位趋势、Flash 擦除周期逼近寿命 |

三种场景共用：

- 同一套帧协议 (`firmware/tracepoint.h` + `lib/protocol.py`)
- 同一套监控采集 (`monitor.py`)
- 同一套统计分析 (`analyze.py`)
- 不同的「行动」层：场景 A 调用 `adjust.py` 修改源码并重新烧录；场景 B/C 只输出报告

---

## 2. 快速开始

### 2.1 环境准备

```bash
# 安装 Python 依赖 (Python 3.10+)
pip install pyserial pyyaml

# 开发依赖 (仅测试)
pip install pytest

# 确认烧录工具链在 PATH 中 (以 OpenOCD 为例)
which openocd
which make       # 或 cmake
```

### 2.2 编辑配置

```bash
# 编辑 config.yaml，至少修改以下字段：
#   serial.port         — 你的串口设备路径 (/dev/ttyUSB0, COM3 等)
#   flash.backend       — 烧录后端 (openocd / jlink / stlink / dfu)
#   flash.openocd.*     — 调试器接口和目标芯片配置
#   project.root        — 工程根目录 (包含 Makefile/CMakeLists.txt)
#   variables           — 你要监控的变量 ID、名称和预期范围
#   parameters          — 你要调优的源码参数
vim tools/config.yaml
```

### 2.3 固件端集成

在固件中包含 `firmware/tracepoint.h`，实现平台相关的串口发送函数：

```c
// 固件唯一需要实现的平台相关函数
void tp_uart_tx(const uint8_t *data, uint16_t len);

// 在 main() 中使用
#include "tracepoint.h"

void main(void) {
    tracepoint_init(&huart2);
    tracepoint_boot_done();   // 通知主机：固件已就绪

    while (1) {
        // ... 你的控制循环 ...
        tracepoint_send_float(0x1001, motor_rpm);
        tracepoint_send_float(0x1002, bus_voltage_v);
        tracepoint_flush();   // 发送一帧
    }
}
```

### 2.4 单步测试命令

```bash
# Step 0: 前置校验 — 检查配置完整性、工具链、串口
python3 tools/validate.py --config tools/config.yaml

# Step 1: 编译并烧录
python3 tools/flash.py --config tools/config.yaml

# Step 2: 单次监控采集 (打印到终端)
python3 tools/monitor.py --config tools/config.yaml --once

# Step 3: 分析当前轮次的 CSV
python3 tools/analyze.py --config tools/config.yaml --round 1

# Step 4: 应用参数修改
python3 tools/adjust.py --config tools/config.yaml --round 1
```

### 2.5 全自动运行

```bash
# 一键启动闭环 (根据配置自动检测场景 A/B/C)
python3 tools/loop_runner.py --config tools/config.yaml

# 限制最大轮数
python3 tools/loop_runner.py --config tools/config.yaml --max-rounds 10
```

---

## 3. 硬件适配

### 3.1 已测试平台

| 芯片 | 调试器 | 串口 | 状态 |
|------|--------|------|------|
| STM32F407VG | ST-Link/V2 | USART2 (PA2/PA3) | **UNVERIFIED** — 接口已定义，未经实际硬件验证 |
| STM32F103C8 | ST-Link/V2 | USART1 (PA9/PA10) | **UNVERIFIED** — 接口已定义，未经实际硬件验证 |

### 3.2 适配新芯片 (三步)

**第 1 步：实现 `tp_uart_tx()` HAL 胶水**

```c
// 示例：STM32 HAL 库
void tp_uart_tx(const uint8_t *data, uint16_t len) {
    HAL_UART_Transmit(&huart2, (uint8_t *)data, len, 100);
}
```

确保 UART 已初始化，波特率与 `config.yaml` 中 `serial.baudrate` 一致。

**第 2 步：修改 `config.yaml` 烧录段**

```yaml
flash:
  backend: openocd               # 或 jlink / stlink
  openocd:
    interface: "interface/stlink-v2.cfg"
    target: "target/stm32f1x.cfg"   # 对应你的芯片
```

**第 3 步：运行前置校验**

```bash
python3 tools/validate.py --config tools/config.yaml
```

校验通过后，执行单步测试命令确认各环节正常工作。

---

## 4. 工程结构

```
tools/
├── README.md                    # 完整使用文档 (本文件)
├── config.yaml                  # 总配置文件 (含 UNVERIFIED 标记注释)
├── validate.py                  # 前置校验：配置完整性、工具链、串口、文件路径
├── simulate.py                  # 离线回放：用历史 CSV 测试 analyze -> adjust 链路
├── flash.py                     # 编译 -> 烧录 -> 读回校验 -> 等待 BOOT_DONE (跨平台)
├── monitor.py                   # 串口帧捕获 -> CSV 时序日志
├── analyze.py                   # 偏差分析 -> 收敛/发散/停滞判定 -> 决策 JSON
├── adjust.py                    # 根据决策修改源码 (生成 diff、回滚、dry-run)
├── loop_runner.py               # 顶层循环编排器 (自动检测场景 A/B/C, 跨平台)
├── firmware/
│   └── tracepoint.h             # 嵌入式端追踪桩头文件 (零分配、ISR 安全)
├── lib/
│   ├── __init__.py              # 包初始化
│   ├── commands.py              # CommandResult + CommandRunner 共享执行原语
│   ├── builders.py              # BuildRunner ABC + Make/CMake/Custom 构建器
│   ├── protocol.py              # 二进制帧协议解析器 (双端一致)
│   ├── backends.py              # MonitorBackend + FlashBackend ABC + 5 个烧录后端
│   ├── analyzers.py             # 分析策略插件 (DeviationAnalyzer + ThresholdAnalyzer)
│   ├── adjusters.py             # 参数修改插件 (MacroAdjuster)
│   └── sinks.py                 # 决策输出插件 (FileSink)
└── tests/
    ├── test_commands.py         # CommandRunner 超时/日志/cwd/shell=False
    ├── test_builders.py         # Make/CMake/Custom 命令构造
    ├── test_backends.py         # 5 个后端工厂/命令构造/能力声明/J-Link 路径
    ├── test_flash.py            # verify_flash 逻辑/退出码映射
    ├── test_loop.py             # 场景检测/StepError/退出码
    ├── test_monitor.py          # --require-boot-done 签名
    └── test_validate.py         # BIN 地址检查/custom 后端
```

### 运行时日志目录

```
tools/logs/
├── validate.log                 # 一次性，启动时生成
├── flash_r{N}_build.log         # 编译元数据 (size, sha256)
├── flash_r{N}_openocd.log       # 烧录工具输出
├── flash_r{N}_readback.bin      # 读回校验 (临时文件)
├── monitor_r{N}.csv             # 时序监控数据
├── decision_r{N}.json           # 分析决策 (含 unverified_assumptions 字段)
├── adjust_r{N}.log              # 参数修改日志
├── adjust_r{N}.diff             # 源码改动 unified diff
└── final_report.md              # 最终报告 (自动生成)
```

---

## 5. 配置参考

`config.yaml` 中各段的说明：

### `project`

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 项目名称 (用于报告) |
| `root` | path | 工程根目录 (Makefile/CMakeLists.txt 所在处) |

### `build`

| 字段 | 类型 | 说明 |
|------|------|------|
| `system` | string | 构建系统: `make` \| `cmake` \| `custom` |
| `directory` | string | 构建输出目录 (默认 `build`) |
| `parallel` | int | 并行任务数: make 转换为 `-jN`, cmake 转换为 `--parallel N` |
| `flags` | list | 额外构建参数 (数组，无 shell 展开) |
| `target` | string | 构建目标 (默认 `all`) |
| `binary` | path | 烧录镜像路径 (elf/bin/hex) |
| `clean_first` | bool | 每次构建前执行 clean |

### `flash`

| 字段 | 类型 | 说明 |
|------|------|------|
| `backend` | string | 烧录后端: `openocd` \| `jlink` \| `stlink` \| `dfu` \| `custom` |
| `verify` | bool | 启用校验 (INLINE 或 SEPARATE 模式) |
| `allow_unverified` | bool | NONE 校验模式时是否允许继续 |
| `allow_no_reset` | bool | NONE 复位模式时是否允许继续 |
| `boot_timeout_ms` | int | 等待 BOOT_DONE 帧的最大时间 |
| `openocd.executable` | path/null | OpenOCD 可执行文件 (null = 自动查找 PATH) |
| `openocd.interface` | path | 调试器接口配置文件 |
| `openocd.target` | path | 目标芯片配置文件 |
| `openocd.address` | string | BIN 镜像的加载地址 (ELF/HEX 不需要) |
| `openocd.extra_args` | list | 额外参数数组 (如 `["-c", "adapter speed 4000"]`) |
| `jlink.executable` | path/null | J-Link 可执行文件 (Windows 自动尝试 `JLink.exe`) |
| `jlink.device` | string | 设备名 (如 `STM32F407VG`) |
| `jlink.interface` | string | 接口协议: `SWD` \| `JTAG` |
| `jlink.speed` | int | 接口速度 (kHz) |
| `jlink.address` | string | BIN 镜像加载地址 |
| `stlink.executable` | path/null | st-flash 可执行文件 |
| `stlink.address` | string | 镜像加载地址 |
| `dfu.executable` | path/null | dfu-util 可执行文件 |
| `dfu.vid` / `dfu.pid` | hex | USB VID/PID |
| `dfu.alt` | int | DFU alternate setting |
| `custom.flash_command` | list | 自定义烧录命令数组 (支持 `{binary}`, `{project_root}` 占位符) |

验证模式 (由各后端类属性声明):

| 模式 | 含义 | 后端 |
|------|------|------|
| INLINE | 烧录命令自带校验 | OpenOCD, J-Link |
| SEPARATE | 独立读回比较 | ST-Link (`st-flash verify`) |
| NONE | 无法校验 | DFU, Custom |

### `serial`

| 字段 | 类型 | 说明 |
|------|------|------|
| `port` | string | 串口设备路径 (`/dev/ttyUSB0`, `COM3`) |
| `baudrate` | int | 波特率 |
| `data_bits` | int | 数据位 (默认 8) |
| `stop_bits` | int | 停止位 (默认 1) |
| `parity` | string | 校验位: `N` \| `E` \| `O` |

### `protocol`

| 字段 | 类型 | 说明 |
|------|------|------|
| `sync_byte_1` | hex | 帧同步头字节 1 (0xAA) |
| `sync_byte_2` | hex | 帧同步头字节 2 (0x55) |
| `max_payload_bytes` | int | 最大帧长度 (含头部和 CRC) |
| `crc_poly` | hex | CRC-8-ATM 多项式 (0x07) |

### `monitor`

| 字段 | 类型 | 说明 |
|------|------|------|
| `duration_ms` | int | 每轮采集时长 |
| `warmup_ms` | int | BOOT_DONE 后丢弃的预热数据时长 |
| `csv_dir` | path | CSV 输出目录 |
| `frame_error_threshold` | float | 帧错误率阈值 (超过则告警) |

### `loop`

| 字段 | 类型 | 说明 |
|------|------|------|
| `max_rounds` | int | 最大调优轮数 |
| `convergence_rounds` | int | 连续达标轮数即判定成功 |
| `stall_rounds` | int | 无改善轮数即判定停滞 |
| `cooldown_ms` | int | 烧录完成到开始监控的冷却时间 |

### `safety`

| 字段 | 类型 | 说明 |
|------|------|------|
| `emergency_action` | string | 紧急行为: `stop` (立即终止) \| `warn` (仅告警) |

### `variables`

每个变量的配置：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | hex | 变量 ID (16-bit，与固件 TRACEPOINT_ID 一致) |
| `name` | string | 可读名称 |
| `type` | string | 数据类型: `int32` \| `uint32` \| `float` \| `uint16` \| `int16` |
| `unit` | string | 显示单位 (仅用于日志，不做转换) |
| `expected` | {min, max} | 预期范围 (成功判据) |
| `warn` | {min, max} | 超出此范围则告警 |
| `emergency` | {min, max} | 超出此范围则紧急停止 |
| `window_ms` | int | 统计滚动窗口 (0 = 使用 monitor.duration_ms) |

### `parameters`

每个可调参数的配置：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 参数名称 |
| `file` | path | 包含此参数的源文件 |
| `pattern` | regex | 定位参数值的正则 (需捕获一个数字组) |
| `type` | string | 参数类型: `macro` \| `kconfig` \| `json` \| `struct_field` |
| `format` | string | printf 格式串 (如 `"%.4f"`, `"%d"`) |
| `range` | {min, max} | 参数的绝对取值边界 |
| `default` | number | 出厂默认值 (用于回滚) |
| `step` | number | 默认调整步长 |
| `auto` | bool | `true` = 工具可自动修改; `false` = 需人工确认 |
| `depends_on` | list | 依赖的变量 ID 列表 |

### 场景 B 诊断配置示例

```yaml
# 场景 B — 阶段诊断 (注释掉了，取消注释即用)
variables:
  - id: 0x2001
    name: "stage_bootloader_init"
    type: uint16
    unit: "flag"
    expected: { min: 0, max: 0 }
    warn:     { min: 1, max: 1 }

  - id: 0x2002
    name: "stage_partition_table"
    type: uint16
    unit: "flag"
    expected: { min: 0, max: 0 }
    warn:     { min: 1, max: 1 }

parameters: []   # 无 auto 参数 -> 自动识别为场景 B
```

---

## 6. 工作流

### 6.1 流程图

```
┌─────────────────────────────────────────────────────────────┐
│                      loop_runner.py                         │
│                                                             │
│  Step 0: validate  ──> python3 tools/validate.py            │
│       │                                                    │
│       ▼                                                    │
│  Step 1: flash     ──> python3 tools/flash.py                  │
│       │              (build -> flash -> verify -> BOOT_DONE)│
│       ▼                                                    │
│  Step 2: monitor   ──> python3 tools/monitor.py            │
│       │              (serial capture -> CSV)               │
│       ▼                                                    │
│  Step 3: analyze   ──> python3 tools/analyze.py            │
│       │              (stats -> trend -> decision JSON)      │
│       ▼                                                    │
│  Step 4: adjust    ──> python3 tools/adjust.py  (场景 A)    │
│       │              or 打印报告并退出  (场景 B)             │
│       │              or 继续监控      (场景 C)               │
│       │                                                    │
│       ▼                                                    │
│  循环直到: 成功收敛 / 停滞 / 紧急停止 / 达到最大轮数          │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 各步骤职责

| 步骤 | 命令 | 输入 | 输出 | 失败处理 |
|------|------|------|------|---------|
| **0. validate** | `validate.py` | `config.yaml` | 通过/错误列表 | 有 ERROR 则终止 |
| **1. flash** | `flash.py` | 源码 + 工具链 | 烧录好的固件 | 按错误码分类重试或终止 |
| **2. monitor** | `monitor.py` | 串口帧数据 | `monitor_r{N}.csv` | 终止 |
| **3. analyze** | `analyze.py` | CSV + 历史 | `decision_r{N}.json` | 终止 |
| **4. adjust** | `adjust.py` | 决策 JSON + 源码 | 修改后的源文件 + diff | 继续下一轮 |

### 6.3 场景自动检测逻辑

`loop_runner.py` 启动时自动判断：

```
config.yaml 中有 auto:true 的 parameters?
    │
    ├── 是 ──> 场景 A (参数寻优)
    │         启用完整闭环: flash -> monitor -> analyze -> adjust -> 循环
    │
    └── 否 ──> 有 variables?
                  │
                  ├── 是 ──> loop.max_rounds <= 3?
                  │            │
                  │            ├── 是 ──> 场景 B (阶段诊断)
                  │            │         跑一轮后打印诊断报告，终止
                  │            │
                  │            └── 否 ──> 场景 C (长期监护)
                  │                      持续采集分析，不修改代码
                  │
                  └── 否 ──> 错误: 无监控变量，退出
```

---

## 7. 帧协议

### 7.1 二进制帧格式 (little-endian)

```
┌──────┬──────┬──────┬──────┬──────────────────────…────────────────────┬──────┐
│ 0xAA │ 0x55 │ seq  │count │ [id(2B)  type(1B)  value(4B)] × N        │ crc8 │
└──────┴──────┴──────┴──────┴──────────────────────…────────────────────┴──────┘
```

| 字段 | 偏移 | 长度 | 说明 |
|------|------|------|------|
| sync_1 | 0 | 1 | 同步字节 1: `0xAA` |
| sync_2 | 1 | 1 | 同步字节 2: `0x55` |
| seq | 2 | 1 | 帧序列号，溢出回绕 |
| count | 3 | 1 | 本帧变量数 (<= 16) |
| var_id | 4 + 7*i | 2 | 变量 ID (0x0001-0x0003 保留) |
| type | 6 + 7*i | 1 | `0x01`=int32, `0x02`=uint32, `0x03`=float, `0x04`=uint16, `0x05`=int16 |
| value | 7 + 7*i | 4 | 小端序原始字节 |
| crc8 | 末 | 1 | CRC-8-ATM (poly=0x07)，覆盖 sync_1 至最后一个 value 字节 |

### 7.2 特殊帧 (var_id 保留值)

| var_id | 含义 | 负载 |
|--------|------|------|
| `0x0001` | BOOT_DONE | 固件启动完成，value[0:2] 可忽略 |
| `0x0002` | ERROR | error_code 在 value[0:2] |
| `0x0003` | HEARTBEAT | uptime_ms 通过附加 0xFFFF ID 的 uint32 传递 |

### 7.3 tracepoint.h 使用示例

```c
#include "tracepoint.h"

// 1. 初始化 — 绑定 UART 句柄
tracepoint_init(&huart2);

// 2. 固件就绪后，通知主机
tracepoint_boot_done();

// 3. 主循环中周期性埋点
while (1) {
    // 执行你的控制逻辑
    // ...

    // 发送浮点变量
    tracepoint_send_float(0x1001, motor_rpm);
    tracepoint_send_float(0x1002, bus_voltage_v);
    tracepoint_send_float(0x1003, cpu_temp_c);

    // 发送整型 flags (场景 B 诊断)
    tracepoint_send_uint16(0x2001, stage_bootloader_init_flag);
    tracepoint_send_uint16(0x2002, stage_partition_table_flag);

    // 刷出帧
    tracepoint_flush();
}

// 4. 可选 — 错误报告
if (critical_error) {
    tracepoint_error(0x01);  // error_code = 1
}
```

所有 API 均为 `static inline`，零堆分配，ISR 安全。

---

## 8. 日志与调试

### 8.1 查看当前状态

```bash
# 查看最近一轮的分析结果
cat tools/logs/decision_r01.json | python3 -m json.tool

# 查看所有轮次的摘要 (如果有 final_report.md)
cat tools/logs/final_report.md

# 查看参数修改历史
cat tools/logs/adjust_r01.log

# 查看源码改动
cat tools/logs/adjust_r01.diff
```

### 8.2 单轮回放

```bash
# 用指定 CSV 重跑分析 (不连接硬件)
python3 tools/simulate.py --csv tools/logs/monitor_r03.csv

# 回放所有历史轮次
python3 tools/simulate.py --all

# 使用 ThresholdAnalyzer (场景 B 诊断模式)
python3 tools/simulate.py --csv tools/logs/monitor_r01.csv --analyzer threshold
```

### 8.3 内置测试用例验证

```bash
# 验证分析逻辑 — 无需硬件
python3 tools/simulate.py --test-case perfect_convergence
python3 tools/simulate.py --test-case slow_divergence
python3 tools/simulate.py --test-case oscillation
python3 tools/simulate.py --test-case emergency_breach
python3 tools/simulate.py --test-case noisy_sensor
```

| 用例 | 数据特征 | 期望判定 |
|------|---------|---------|
| `perfect_convergence` | 变量进入 expected 范围 | ok |
| `slow_divergence` | 偏差逐轮增大 | bad (worsening) |
| `oscillation` | 均值正常但方差大 | bad |
| `emergency_breach` | 一个值超出 emergency 上限 | emergency |
| `noisy_sensor` | 正常数据，帧错误场景在 monitor 层处理 | ok |

### 8.4 对比两轮差异

```bash
# 查看决策差异
diff <(python3 -m json.tool tools/logs/decision_r01.json) \
     <(python3 -m json.tool tools/logs/decision_r02.json)

# 查看参数改动差异
diff tools/logs/adjust_r01.diff tools/logs/adjust_r02.diff
```

### 8.5 adjust.py 安全操作

```bash
# Dry-run: 只打印将要修改的内容，不改文件
python3 tools/adjust.py --config tools/config.yaml --round 1 --dry-run

# 回滚某轮的修改 (从 .bak 备份恢复)
python3 tools/adjust.py --config tools/config.yaml --rollback --round 1
```

---

## 9. 扩展接口

工具链预留了 5 个扩展点，当前提供最小可用实现，未来可按需替换或扩展。

### 9.1 监控后端 (`lib/backends.py` — MonitorBackend)

抽象监控数据源。当前实现：
- **SerialBackend** — UART / USB CDC (基于 pyserial)
- **FileBackend** — 回放预录二进制日志 (用于 `simulate.py`)

预留后端：
- `RTTBackend` — SEGGER RTT via J-Link
- `SWOBackend` — ARM SWO trace via debug probe
- `CANBackend` — CAN bus via socketcan
- `TCPBackend` — WiFi / Ethernet TCP telemetry
- `FileBackend` — 已实现

### 9.2 烧录后端 (`lib/backends.py` — FlashBackend)

抽象烧录+校验+复位流程 (ABC 接口)，通过 `CommandRunner` 执行外部 CLI 工具。`create_flash_backend()` 工厂根据 `config.yaml` 自动选择具体后端。

已实现的 5 个后端：

| 后端类 | CLI 工具 | 校验模式 | 复位模式 |
|--------|---------|---------|---------|
| `OpenOCDBackend` | openocd | INLINE | INLINE |
| `JLinkBackend` | JLinkExe / JLink.exe | INLINE | INLINE |
| `STLinkBackend` | st-flash | SEPARATE | SUPPORTED |
| `DFUBackend` | dfu-util | NONE | NONE |
| `CustomFlashBackend` | 用户定义 | NONE | NONE |

预留后端：
- `CMSISDAPBackend` — pyOCD
- `BootloaderBackend` — 自定义串口 bootloader 协议
- `RaspberryPiPicoBackend` — picoprobe
- `ESP32Backend` — esptool.py

### 9.3 分析策略 (`lib/analyzers.py` — Analyzer)

抽象分析策略。当前实现：
- **DeviationAnalyzer** — 基于 expected / warn / emergency 范围的偏差分析 (场景 A)
- **ThresholdAnalyzer** — 基于 flag 变量非零检测的阶段诊断 (场景 B)

预留分析器：
- `TrendAnalyzer` — Mann-Kendall 趋势检验 (场景 C)
- `FFTAnalyzer` — 频域分析，检测振荡频率和幅值
- `MLAnomalyDetector` — 基于历史数据训练异常检测模型

### 9.4 参数修改器 (`lib/adjusters.py` — Adjuster)

抽象参数修改方式。当前实现：
- **MacroAdjuster** — C 头文件 `#define` 正则替换

预留修改器：
- `KconfigAdjuster` — Linux Kconfig / Zephyr Kconfig
- `JSONConfigAdjuster` — JSON 配置文件
- `EEPROMAdjuster` — 通过串口协议运行时写 EEPROM，无需重新烧录
- `CLICommandAdjuster` — 通过串口发送 AT 命令修改参数

### 9.5 决策输出 (`lib/sinks.py` — DecisionSink)

抽象决策持久化方式。当前实现：
- **FileSink** — 写入 `tools/logs/decision_r{N}.json`

预留输出端：
- `MQTTSink` — 发布到 MQTT broker
- `WebhookSink` — HTTP POST 到监控平台
- `SQLiteSink` — 写入本地数据库供长期分析

### config.yaml 扩展预留

```yaml
extensions:
  monitor_backend: "serial"       # 将来: rtt | swo | can | tcp
  analyzer: "deviation"           # 将来: threshold | trend | fft | ml
  adjuster: "macro"               # 将来: kconfig | json | eeprom | cli
  decision_sink: "file"           # 将来: mqtt | webhook | sqlite
```

---

## 10. 限制与假设 (UNVERIFIED)

以下所有标记均可在代码中通过 `grep -r "UNVERIFIED" tools/` 检索。每个假设均说明了影响和缓解措施。

| # | 位置 | 假设 | 影响 | 缓解 |
|---|------|------|------|------|
| 1 | `lib/analyzers.py` DeviationAnalyzer | 变量偏差与参数呈**单调负相关** (偏差为正则减小参数) | 非单调系统 (如谐振峰) 的调参方向可能错误 | 在 `config.yaml` 中为每个参数添加 `direction_map` 字段，记录不同工作区的方向 |
| 2 | `lib/analyzers.py` detect_trend() | **0.05** 归一化偏差阈值是任意值 | 高噪声变量可能误判趋势；低噪声变量可能延迟检测 | 为每个变量配置独立的 `trend_threshold`，或使用 Mann-Kendall 统计检验 |
| 3 | `config.yaml` `loop.convergence_rounds` | 收敛判定 3 轮 / 停滞判定 5 轮无领域依据 | 快速系统可能过早结束，惯性系统可能过晚结束 | 根据系统主导时间常数和噪声特性调校这两个值 |
| 4 | `lib/analyzers.py` build_recommendations() | 所有依赖变量**等权相加** | 各变量对参数的敏感度不同时，建议可能偏向噪声大的变量 | 引入灵敏度矩阵 `sensitivity` 字段，记录 ∂var/∂param |
| 5 | `lib/adjusters.py` MacroAdjuster | 每个宏只在源码中定义**一次** | `#ifdef` 平台分支可能漏改第二个定义 | 检查所有匹配，多匹配时告警并要求人工确认 |
| 6 | `lib/analyzers.py` ThresholdAnalyzer | flag 变量值 **> 0 即表示错误** | 硬件可能使用不同错误编码约定 (如负值、位掩码) | 在变量配置中添加 `error_convention` 字段 (如 `nonzero` / `bitmask:0x01`) |
| 7 | `config.yaml` parameters.step | step 值 (0.1, 0.05, 0.01) 是**任意值** | 步长过大可能振荡，过小可能收敛极慢 | 根据系统灵敏度 (∂var/∂param) 校调步长，或使用自适应步长 |
| 8 | 硬件平台 | STM32F407VG / STM32F103C8 接口**未经实际硬件验证** | 烧录命令、串口引脚、HAL 胶水可能存在配置差异 | 首次使用时逐步骤验证：validate -> flash -> monitor --once |
| 9 | 所有预留扩展 | RTT / SWO / CAN / TCP / CMSIS-DAP / MQTT / SQLite 等仅命名预留 | 切换后端需要自行实现对应子类 | 所有接口均为抽象基类 (ABC)，子类化时 API 已定义好 |
| 10 | 帧协议完整性 | CRC-8-ATM 对连续位错误的检测概率为 99.6% (单字节) | 对丢字节/插入字节的恢复依赖 sync header 重同步 | `FrameParser` 实现了 sync 重扫描，强噪声环境建议增加帧计数校验或 CRC-16 |

---

## 11. 版本

**v0.2.0** — Bash→Python 跨平台迁移。

变更摘要 (相对 v0.1.0):

- `flash.sh` → `flash.py`：构建+烧录+校验+复位+启动等待，Windows/Linux/macOS 三平台
- `loop_runner.sh` → `loop_runner.py`：场景自动检测 + 闭环编排 + 异常/超时映射
- 新增 `lib/commands.py`：CommandResult + CommandRunner 共享执行原语 (可注入，可测试)
- 新增 `lib/builders.py`：Make/CMake/Custom 构建器，命令构造与执行分离
- 重构 `lib/backends.py`：FlashBackend ABC + 5 个后端 (OpenOCD/JLink/STLink/DFU/Custom) + FlashContext
- `config.yaml` 迁移：`build.flags` 数组化、`parallel` 替代 `$(nproc)`、BIN 地址配置、flash 段新增 `allow_unverified`/`allow_no_reset`
- `monitor.py`：新增 `--require-boot-done` 参数，`monitor_to_csv()` 签名参数化
- `validate.py`：新增 BIN 地址检查、`custom` 后端支持
- 新增 7 个测试文件，44 个测试用例
- Python 最低版本要求：3.10+

保留不变 (v0.1.0):

- 三类场景 (A/B/C) 自动检测
- `UNVERIFIED:` 前缀标记 (共 10 处)，可通过 `grep -r "UNVERIFIED" tools/` 检索
- 5 个扩展接口 (MonitorBackend / FlashBackend / Analyzer / Adjuster / DecisionSink)
