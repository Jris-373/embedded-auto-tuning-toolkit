# Embedded Auto-Tuning Toolkit — Design Spec

**Version:** v0.1.0
**Date:** 2026-06-19
**Status:** Draft

---

## 1. 概述

### 1.1 一句话定义

Embedded Auto-Tuning Toolkit 是一套「固件端埋点 + 主机端闭环分析」的通用工具链。
你在固件中植入 `tracepoint.h` 追踪桩，主机端通过串口二进制帧协议收集变量，
然后根据配置的策略执行 **分析 → 决策 → 行动** 闭环。

### 1.2 核心抽象

```
固件端 tracepoint.h 埋点  →  串口二进制帧  →  monitor.py 采集 CSV
                                                   ↓
                                         analyze.py 判定
                                                   ↓
                              ┌─────────────────────┴──────────────────────┐
                              ↓                     ↓                       ↓
                         场景 A: 参数寻优      场景 B: 阶段诊断        场景 C: 长期监护
                         adjust.py 改源码     输出故障阶段变量         持续告警/记录
                         重新烧录 → 循环       单次采集 → 终止         不修改，持续监控
```

### 1.3 三类适用场景

| 场景 | 触发条件 | 闭环行为 | 示例 |
|------|---------|---------|------|
| **A. 参数寻优** | config.yaml 有 `auto: true` 的 parameters | flash → monitor → analyze → adjust → 循环，直到变量收敛到预期范围 | PID 系数、滤波器截止频率、阈值门限、PWM 死区 |
| **B. 阶段诊断** | config.yaml 有 variables 但无 `auto: true` 的 parameters | flash → monitor → analyze → 打印诊断报告 → 终止。固件在每个阶段设置状态变量，出错置 1 | SD 卡烧录阶段定位、上电自检序列、外设初始化链 |
| **C. 长期监护** | loop.max_rounds 设为极大值，parameters 全为 `auto: false` 或空 | flash → monitor → analyze（周期性）→ 不修改代码，仅记录趋势和告警 | 电源纹波劣化、堆栈水位趋势、Flash 擦除周期逼近寿命 |

**共用机制：**
- 同一套帧协议（`tracepoint.h` + `lib/protocol.py`）
- 同一套监控采集（`monitor.py`）
- 同一套统计分析（`analyze.py`）
- 不同的「行动」层（场景 A 调 adjust.py，场景 B/C 只输出报告）

---

## 2. 工程结构

```
tools/
├── README.md                    # 完整使用文档
├── config.yaml                  # 总配置（含 UNVERIFIED 标记注释）
├── validate.py                  # 前置校验（config完整性、工具链、串口、文件路径）
├── simulate.py                  # 离线回放（用历史CSV测试analyze→adjust链路）
├── flash.sh                     # 编译 → 烧录 → 读回校验 → 等待BOOT_DONE
├── monitor.py                   # 串口帧捕获 → CSV时序日志
├── analyze.py                   # 偏差分析 → 收敛/发散/停滞判定 → 决策JSON
├── adjust.py                    # 根据决策修改源码（生成diff、回滚、人工确认分级）
├── loop_runner.sh               # 顶层循环编排器（自动检测场景 A/B/C）
├── firmware/
│   └── tracepoint.h             # 嵌入式端追踪桩头文件（零分配、ISR安全）
└── lib/
    ├── __init__.py
    └── protocol.py              # 二进制帧协议解析器（双端一致）
```

### 2.1 运行时日志目录

```
tools/logs/
├── validate.log                 # 一次性，启动时生成
├── flash_r{N}_build.log         # 编译元数据（size, sha256）
├── flash_r{N}_openocd.log       # 烧录工具输出
├── flash_r{N}_readback.bin      # 读回校验（临时）
├── monitor_r{N}.csv             # 时序监控数据
├── decision_r{N}.json           # 分析决策（含 unverified_assumptions 字段）
├── adjust_r{N}.log              # 参数修改日志
├── adjust_r{N}.diff             # 源码改动 unified diff
└── final_report.md              # 最终报告
```

---

## 3. 帧协议

### 3.1 二进制帧格式（little-endian）

```
┌──────┬──────┬──────┬──────┬────────────────────…──────────────────┬──────┐
│ 0xAA │ 0x55 │ seq  │count │ [id(2B)  type(1B)  value(4B)] × N    │ crc8 │
└──────┴──────┴──────┴──────┴────────────────────…──────────────────┴──────┘
```

| 字段 | 偏移 | 长度 | 说明 |
|------|------|------|------|
| sync_1 | 0 | 1 | 0xAA |
| sync_2 | 1 | 1 | 0x55 |
| seq | 2 | 1 | 序列号，溢出回绕 |
| count | 3 | 1 | 本帧变量数（≤16） |
| var_id | 4+7i | 2 | 变量 ID（0x0001-0x0003 保留为特殊帧） |
| type | 6+7i | 1 | 0x01=int32, 0x02=uint32, 0x03=float, 0x04=uint16, 0x05=int16 |
| value | 7+7i | 4 | 小端序原始字节 |
| crc8 | 末 | 1 | CRC-8-ATM (poly=0x07) 覆盖 sync_1 至最后一个 value 字节 |

### 3.2 特殊帧（var_id 保留值）

| var_id | 含义 | 负载 |
|--------|------|------|
| 0x0001 | BOOT_DONE | 固件启动完成 |
| 0x0002 | ERROR | error_code 在 value[0:2] |
| 0x0003 | HEARTBEAT | 附加 uptime_ms 在 value |

### 3.3 固件端集成

固件唯一需要实现的平台相关函数：
```c
void tp_uart_tx(const uint8_t *data, uint16_t len);
```
其余全部为 `static inline` 宏，零堆分配，ISR 安全。

---

## 4. validate.py — 前置校验

### 4.1 校验清单

| 类别 | 检查项 | 失败级别 |
|------|--------|---------|
| 配置文件 | config.yaml 存在且 YAML 合法 | ERROR |
| 配置文件 | 所有 variables.id 唯一 | ERROR |
| 配置文件 | parameters.depends_on 引用的 var_id 存在 | ERROR |
| 配置文件 | flash.backend 是支持的值 | ERROR |
| 工具链 | 构建系统 (make/cmake) 在 PATH | ERROR |
| 工具链 | 烧录后端 CLI 存在 | ERROR |
| 工具链 | Python 依赖 (pyserial, pyyaml) 可 import | ERROR |
| 文件路径 | project.root 存在 | ERROR |
| 文件路径 | parameters.file 存在且 pattern 匹配到至少一行 | WARN |
| 硬件连接 | serial.port 设备节点存在 | WARN |
| 硬件连接 | 串口可打开 | WARN |
| 版本一致 | config.protocol 与 tracepoint.h 宏一致 | ERROR |

### 4.2 输出

- 全部通过 → `[validate] OK`，退出 0，静默
- 有 ERROR → 逐项打印错误和修复建议，退出 1
- 有 WARN 无 ERROR → 打印黄色警告，退出 0

---

## 5. simulate.py — 离线回放

### 5.1 运行模式

| 模式 | 命令 | 用途 |
|------|------|------|
| 单轮回放 | `simulate.py --csv logs/monitor_r03.csv` | 调试某一轮决策 |
| 多轮演练 | `simulate.py --all` | 回放所有 CSV，输出每轮决策对比 |
| 假数据注入 | `simulate.py --test-case <name>` | 用内置测试向量验证判定逻辑 |

### 5.2 内置测试用例

| 用例名 | 数据特征 | 期望判定 |
|--------|---------|---------|
| `perfect_convergence` | 变量 3 轮内进入 expected 范围 | success |
| `slow_divergence` | 偏差逐轮增大 5% | diverging |
| `oscillation` | 均值和上轮持平但方差大 | oscillating |
| `emergency_breach` | 一个值超出 emergency 上限 | emergency |
| `noisy_sensor` | 帧错误率 8% | warn: frame_error |

每个用例的期望判定写在代码注释中——可自动化验证。

---

## 6. 分析引擎 (analyze.py) 改动

### 6.1 UNVERIFIED 标记注入（4 处）

| # | 位置 | 假设内容 | 标记 |
|---|------|---------|------|
| 1 | `build_recommendations()` 方向推断 | 变量偏差与参数呈单调负相关 | `UNVERIFIED: assumes monotonic negative correlation` |
| 2 | `detect_trend()` 阈值 | 0.05 归一化偏差阈值是任意值 | `UNVERIFIED: 0.05 threshold is arbitrary` |
| 3 | `check_termination()` 轮数 | 收敛 3 轮/停滞 5 轮无领域依据 | `UNVERIFIED: placeholder defaults` |
| 4 | `build_recommendations()` 权重 | 所有依赖变量等权相加 | `UNVERIFIED: equal weighting, no sensitivity matrix` |

### 6.2 decision JSON 新增字段

```json
{
  "round": 3,
  "unverified_assumptions": [
    "param_dir_heuristic: assumes monotonic negative correlation",
    "trend_threshold: fixed 0.05, no per-variable noise calibration",
    "convergence_rounds: default 3, not tuned to system dynamics",
    "var_weighting: equal weight, no sensitivity matrix"
  ],
  "variables": [...],
  "recommendations": [...]
}
```

---

## 7. adjust.py 改动

### 7.1 diff 生成

每次修改文件后：
1. 保留 `.bak` 备份
2. 调用 `diff -u <backup> <modified>` 生成 unified diff
3. 写入 `tools/logs/adjust_r{N}.diff`
4. 终端打印 diff 前 10 行摘要

多文件修改在同一 diff 文件中用 `=== file: <path> ===` 分隔。

### 7.2 loop_runner.sh 流水线

```
Step 0: validate   → python3 tools/validate.py --config ...    ← 新增
Step 1: flash      (不变)
Step 2: monitor    (不变)
Step 3: analyze    (不变)
Step 4: adjust     → 生成 diff，打印摘要到终端                 ← 增强
```

### 7.3 场景自动检测

`loop_runner.sh` 在启动时检查 config.yaml：
- 存在 `auto: true` 的 parameters → **场景 A**（参数寻优），启用完整闭环
- 有 variables 但无 `auto: true` 的 parameters 且 `loop.max_rounds` ≤ 3 → **场景 B**（阶段诊断），跑一轮后终止
- 有 variables 但无 `auto: true` 的 parameters 且 `loop.max_rounds` 较大 → **场景 C**（长期监护），持续监控不修改代码

---

## 8. 扩展接口（预留 API）

### 8.1 监控后端接口 (Monitor Backend)

```python
# lib/backends.py — 预留
class MonitorBackend:
    """抽象监控后端。当前实现：SerialBackend。"""
    def open(self, config: dict) -> None: ...
    def read(self, timeout_ms: int) -> bytes: ...
    def close(self) -> None: ...

# 已注册：
#   - SerialBackend       (UART/USB CDC)
# 预留：
#   - RTTBackend          (SEGGER RTT via J-Link)
#   - SWOBackend          (ARM SWO trace via debug probe)
#   - CANBackend          (CAN bus via socketcan)
#   - TCPBackend          (WiFi/Ethernet TCP telemetry)
#   - FileBackend         (回放预录的二进制日志)
```

### 8.2 烧录后端接口 (Flash Backend)

```python
# lib/backends.py — 预留
class FlashBackend:
    """抽象烧录后端。当前实现：通过 flash.sh 子进程调用。"""
    def build(self, config: dict) -> bool: ...
    def flash(self, config: dict, binary_path: str) -> bool: ...
    def verify(self, config: dict, binary_path: str) -> bool: ...
    def reset(self) -> bool: ...

# 已注册：
#   - OpenOCDBackend
#   - JLinkBackend
#   - STLinkBackend
#   - DFUBackend
# 预留：
#   - CMSISDAPBackend     (pyOCD)
#   - BootloaderBackend   (自定义串口 bootloader 协议)
#   - RaspberryPiPicoBackend (picoprobe)
#   - ESP32Backend        (esptool.py)
```

### 8.3 分析策略接口 (Analyzer Plugin)

```python
# lib/analyzers.py — 预留
class Analyzer:
    """抽象分析策略。当前实现：DeviationAnalyzer（基于预期范围偏差）。"""
    def analyze(self, csv_path: str, var_configs: list, history: list) -> Decision: ...

# 已注册：
#   - DeviationAnalyzer   (当前实现，基于 expected/min/max 范围)
# 预留：
#   - ThresholdAnalyzer   (场景 B 诊断：检测 flag 变量是否非零)
#   - TrendAnalyzer       (场景 C 监护：Mann-Kendall 趋势检验)
#   - FFTAnalyzer         (频域分析：检测振荡频率和幅值)
#   - MLAnomalyDetector   (基于历史数据训练异常检测模型)
```

### 8.4 参数修改接口 (Adjuster Plugin)

```python
# lib/adjusters.py — 预留
class Adjuster:
    """抽象参数修改器。当前实现：MacroAdjuster（C #define 正则替换）。"""
    def read_current(self, param_config: dict) -> float: ...
    def apply(self, param_config: dict, new_value: float) -> bool: ...
    def diff(self) -> str: ...

# 已注册：
#   - MacroAdjuster        (C 头文件 #define)
# 预留：
#   - KconfigAdjuster      (Linux Kconfig / Zephyr Kconfig)
#   - JSONConfigAdjuster   (JSON 配置文件)
#   - EEPROMAdjuster       (通过串口协议运行时写 EEPROM 参数，无需重新烧录)
#   - CLICommandAdjuster   (通过串口发送 AT 命令修改参数)
```

### 8.5 决策输出接口 (Decision Sink)

```python
# lib/sinks.py — 预留
class DecisionSink:
    """抽象决策输出。当前实现：FileSink（写 JSON 文件）。"""
    def write(self, decision: Decision) -> None: ...

# 已注册：
#   - FileSink             (写入 tools/logs/)
# 预留：
#   - MQTTSink             (发布到 MQTT broker)
#   - WebhookSink          (HTTP POST 到监控平台)
#   - SQLiteSink           (写入本地数据库供长期分析)
```

### 8.6 config.yaml 扩展预留

```yaml
# 预留字段（当前版本不实现，但解析器不报错）
extensions:
  monitor_backend: "serial"       # 将来: rtt | swo | can | tcp
  analyzer: "deviation"           # 将来: threshold | trend | fft | ml
  adjuster: "macro"               # 将来: kconfig | json | eeprom | cli
  decision_sink: "file"           # 将来: mqtt | webhook | sqlite
```

---

## 9. README.md 结构大纲

```markdown
# Embedded Auto-Tuning Toolkit

## 1. 概述
  一句话定义 + 三类场景（A 参数寻优 / B 阶段诊断 / C 长期监护）+ 共用底层机制

## 2. 快速开始
  pip install pyserial pyyaml
  编辑 config.yaml（port / backend / variables / parameters）
  单步测试：validate → flash → monitor --once
  全自动：bash loop_runner.sh

## 3. 硬件适配
  ### 已测试平台
    | 芯片 | 调试器 | 串口 | 状态 |
    | STM32F407VG | ST-Link | USART2 | UNVERIFIED |
    | STM32F103C8 | ST-Link | USART1 | UNVERIFIED |
  ### 适配新芯片（3 步）
    1. 实现 tp_uart_tx() HAL 胶水
    2. 修改 config.yaml flash 段
    3. 运行 python3 tools/validate.py 验证

## 4. 工程结构
  目录树 + 每个文件职责一句话

## 5. 配置参考
  config.yaml 每个段的说明表 + 场景 B 诊断配置示例

## 6. 工作流
  ASCII 流程图 + 5 步描述 + 场景自动检测逻辑

## 7. 帧协议
  二进制帧格式表 + tracepoint.h 使用示例

## 8. 日志与调试
  查看当前状态 / 单轮回放 / 对比两轮差异

## 9. 扩展接口
  8.1 监控后端 / 8.2 烧录后端 / 8.3 分析策略 / 8.4 参数修改器 / 8.5 决策输出

## 10. 限制与假设 (UNVERIFIED)
  所有 UNVERIFIED 标记点、影响、缓解方法

## 11. 版本
  v0.1.0 — 初始模板，等待实际项目打磨
```

---

## 10. 实现范围（本次）

### 新增文件
- `tools/validate.py`
- `tools/simulate.py`
- `tools/README.md`
- `tools/lib/backends.py`（接口定义 + SerialBackend + FileBackend）
- `tools/lib/analyzers.py`（DeviationAnalyzer + ThresholdAnalyzer）
- `tools/lib/adjusters.py`（MacroAdjuster）
- `tools/lib/sinks.py`（FileSink）
- `docs/superpowers/specs/2026-06-19-embedded-auto-tuning-toolkit-design.md`（本文件）

### 修改文件
- `tools/config.yaml`：加 UNVERIFIED 注释 + extensions 预留段 + 场景 B 示例注释
- `tools/analyze.py`：4 处 UNVERIFIED 标记 + decision JSON 新字段 + 场景 B 诊断模式
- `tools/adjust.py`：diff 生成 + --dry-run 增强
- `tools/loop_runner.sh`：Step 0 validate + 场景自动检测

### 不变文件
- `tools/firmware/tracepoint.h`
- `tools/lib/protocol.py`
- `tools/monitor.py`（仅加 UNVERIFIED 注释）
- `tools/flash.sh`（加前置依赖检查注释）

---

## 11. 规格自查

### 11.1 Placeholder Scan
- 无 TODO / TBD 残留

### 11.2 内部一致性
- 帧协议定义在 tracepoint.h、protocol.py、config.yaml 三处一致
- 三类场景共用 analyze.py，通过 config 区分路径
- 扩展接口全部以 class 抽象定义，当前实现为特化子类

### 11.3 作用域
- 聚焦单次 loop_runner.sh 调用，不涉及 CI/CD / 多板卡并行 / 远程管理
- 扩展接口只定义 API 签名和预留注册表，不实现

### 11.4 歧义检查
- 场景 B 和 C 的边界：以 `auto: true` parameters 的存在为判据，明确
- UNVERIFIED 标记统一使用 `UNVERIFIED:` 前缀，grep 可检索
