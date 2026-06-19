/**
 * =============================================================================
 * tracepoint.h — Lightweight firmware-side instrumentation
 * =============================================================================
 *
 * Zero-allocation, ISR-safe telemetry for the auto-tuning loop.
 *
 * Usage in firmware:
 *   #include "tracepoint.h"
 *
 *   void main(void) {
 *       tracepoint_init(&huart2);        // bind to a UART
 *       tracepoint_boot_done();          // signal host: ready
 *
 *       while (1) {
 *           // ... control loop ...
 *           tracepoint_send_float(0x1001, motor_rpm);
 *           tracepoint_send_float(0x1002, bus_voltage_v);
 *           tracepoint_flush();          // send frame
 *       }
 *   }
 *
 * Frame format (binary, little-endian):
 *   ┌──────┬──────┬──────┬──────┬───────────────…─────────────────┬──────┐
 *   │ 0xAA │ 0x55 │ seq  │count │ [id(2B)|type(1B)|val(4B)]*N    │ crc8 │
 *   └──────┴──────┴──────┴──────┴───────────────…─────────────────┴──────┘
 *
 * Types: 0x01=int32, 0x02=uint32, 0x03=float, 0x04=uint16, 0x05=int16
 */

#ifndef TRACEPOINT_H
#define TRACEPOINT_H

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#ifdef __cplusplus
extern "C" {
#endif

/* --------------------------------------------------------------------------
 * Configuration — adjust buffer size and max variables per frame here
 * -------------------------------------------------------------------------- */
#ifndef TP_MAX_VARS_PER_FRAME
#define TP_MAX_VARS_PER_FRAME   16
#endif

#ifndef TP_TX_BUFFER_SIZE
#define TP_TX_BUFFER_SIZE       256
#endif

#define TP_SYNC_1               0xAA
#define TP_SYNC_2               0x55

#define TP_TYPE_INT32           0x01
#define TP_TYPE_UINT32          0x02
#define TP_TYPE_FLOAT           0x03
#define TP_TYPE_UINT16          0x04
#define TP_TYPE_INT16           0x05

/* Special frame types sent as variable ID 0x0000 */
#define TP_SPECIAL_BOOT_DONE    0x0001
#define TP_SPECIAL_ERROR        0x0002
#define TP_SPECIAL_HEARTBEAT    0x0003

/* --------------------------------------------------------------------------
 * UART HAL abstraction — implement these for your platform
 * -------------------------------------------------------------------------- */
typedef void (*tp_uart_tx_fn)(const uint8_t *data, uint16_t len);

/* ---- Implement this in your HAL glue ---------------------------------- */
extern void tp_uart_tx(const uint8_t *data, uint16_t len);

/* --------------------------------------------------------------------------
 * Internal state
 * -------------------------------------------------------------------------- */
static uint8_t  tp_tx_buf[TP_TX_BUFFER_SIZE];
static uint8_t  tp_var_count;
static uint16_t tp_buf_pos;
static uint8_t  tp_seq;

/* --------------------------------------------------------------------------
 * CRC-8-ATM (x^8 + x^2 + x + 1)
 * -------------------------------------------------------------------------- */
static uint8_t tp_crc8(const uint8_t *data, uint16_t len)
{
    uint8_t crc = 0x00;
    while (len--) {
        crc ^= *data++;
        for (uint8_t i = 0; i < 8; i++) {
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (crc << 1);
        }
    }
    return crc;
}

/* --------------------------------------------------------------------------
 * Begin a new frame
 * -------------------------------------------------------------------------- */
static void tp_frame_begin(void)
{
    tp_buf_pos  = 0;
    tp_var_count = 0;

    /* Sync bytes */
    tp_tx_buf[tp_buf_pos++] = TP_SYNC_1;
    tp_tx_buf[tp_buf_pos++] = TP_SYNC_2;

    /* Seq (placeholder) */
    tp_tx_buf[tp_buf_pos++] = tp_seq;

    /* Count (placeholder) */
    tp_tx_buf[tp_buf_pos++] = 0;
}

/* --------------------------------------------------------------------------
 * Append a typed variable to the frame
 * -------------------------------------------------------------------------- */
static bool tp_append(uint16_t id, uint8_t type, const uint8_t *val4b)
{
    uint16_t needed = tp_buf_pos + 7 + 1;  /* 7 bytes entry + crc slot */
    if (needed > TP_TX_BUFFER_SIZE) return false;
    if (tp_var_count >= TP_MAX_VARS_PER_FRAME) return false;

    /* VarID (little-endian) */
    tp_tx_buf[tp_buf_pos++] = (uint8_t)(id & 0xFF);
    tp_tx_buf[tp_buf_pos++] = (uint8_t)((id >> 8) & 0xFF);

    /* Type */
    tp_tx_buf[tp_buf_pos++] = type;

    /* Value (4 bytes, little-endian) */
    tp_tx_buf[tp_buf_pos++] = val4b[0];
    tp_tx_buf[tp_buf_pos++] = val4b[1];
    tp_tx_buf[tp_buf_pos++] = val4b[2];
    tp_tx_buf[tp_buf_pos++] = val4b[3];

    tp_var_count++;
    return true;
}

/* --------------------------------------------------------------------------
 * Public: add variables to the current frame
 * -------------------------------------------------------------------------- */
static bool tracepoint_send_int32(uint16_t id, int32_t val)
{
    uint8_t b[4];
    b[0] = (uint8_t)(val & 0xFF);
    b[1] = (uint8_t)((val >> 8) & 0xFF);
    b[2] = (uint8_t)((val >> 16) & 0xFF);
    b[3] = (uint8_t)((val >> 24) & 0xFF);
    return tp_append(id, TP_TYPE_INT32, b);
}

static bool tracepoint_send_uint32(uint16_t id, uint32_t val)
{
    uint8_t b[4];
    b[0] = (uint8_t)(val & 0xFF);
    b[1] = (uint8_t)((val >> 8) & 0xFF);
    b[2] = (uint8_t)((val >> 16) & 0xFF);
    b[3] = (uint8_t)((val >> 24) & 0xFF);
    return tp_append(id, TP_TYPE_UINT32, b);
}

static bool tracepoint_send_float(uint16_t id, float val)
{
    uint8_t b[4];
    uint32_t raw;
    memcpy(&raw, &val, sizeof(raw));
    b[0] = (uint8_t)(raw & 0xFF);
    b[1] = (uint8_t)((raw >> 8) & 0xFF);
    b[2] = (uint8_t)((raw >> 16) & 0xFF);
    b[3] = (uint8_t)((raw >> 24) & 0xFF);
    return tp_append(id, TP_TYPE_FLOAT, b);
}

static bool tracepoint_send_uint16(uint16_t id, uint16_t val)
{
    uint8_t b[4] = {0};
    b[0] = (uint8_t)(val & 0xFF);
    b[1] = (uint8_t)((val >> 8) & 0xFF);
    return tp_append(id, TP_TYPE_UINT16, b);
}

static bool tracepoint_send_int16(uint16_t id, int16_t val)
{
    uint8_t b[4] = {0};
    uint16_t uv = (uint16_t)val;
    b[0] = (uint8_t)(uv & 0xFF);
    b[1] = (uint8_t)((uv >> 8) & 0xFF);
    return tp_append(id, TP_TYPE_INT16, b);
}

/* --------------------------------------------------------------------------
 * Public: finalize and transmit the frame
 * -------------------------------------------------------------------------- */
static void tracepoint_flush(void)
{
    if (tp_var_count == 0) return;

    /* Write actual count into placeholder */
    tp_tx_buf[3] = tp_var_count;

    /* Update sequence in placeholder */
    tp_tx_buf[2] = tp_seq++;

    /* CRC over everything from sync_1 through last variable byte */
    uint16_t payload_len = tp_buf_pos;
    uint8_t crc = tp_crc8(tp_tx_buf, payload_len);
    tp_tx_buf[tp_buf_pos++] = crc;

    tp_uart_tx(tp_tx_buf, tp_buf_pos);
}

/* --------------------------------------------------------------------------
 * Special frames
 * -------------------------------------------------------------------------- */
static void tracepoint_boot_done(void)
{
    tp_frame_begin();
    tracepoint_send_uint16(TP_SPECIAL_BOOT_DONE, 0);
    tracepoint_flush();
}

static void tracepoint_error(uint16_t error_code)
{
    tp_frame_begin();
    tracepoint_send_uint16(TP_SPECIAL_ERROR, error_code);
    tracepoint_flush();
}

static void tracepoint_heartbeat(uint32_t uptime_ms)
{
    tp_frame_begin();
    tracepoint_send_uint16(TP_SPECIAL_HEARTBEAT, 0);
    tracepoint_send_uint32(0xFFFF, uptime_ms);
    tracepoint_flush();
}

#ifdef __cplusplus
}
#endif

#endif /* TRACEPOINT_H */
