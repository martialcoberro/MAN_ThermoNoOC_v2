#ifndef MICROFLUIDICS_H
#define MICROFLUIDICS_H

#include <Arduino.h>
#include <Wire.h>
#include "Pinout.h"

/**
 * @class Microfluidics
 * @brief Controls 4 Bartels mp-Lowdriver peristaltic pumps and 2 Sensirion SLF3S-0600F
 *        flow sensors via I2C multiplexer MUX_ADDR_MICROFLUIDICS (0x71).
 *
 * Single responsibility: pump configuration, continuous/pulsed flow logic, and
 * flow sensor reading. No incubator sensor data, LED, fan, or WiFi logic.
 *
 * Circuit organisation:
 *   Circuit 1 — pump 1 (fluid) + pump 3 (air bubble removal)
 *   Circuit 2 — pump 2 (fluid) + pump 4 (air bubble removal)
 *
 * Non-blocking pump updates:
 *   The mp-Lowdriver requires a STOP → [5 ms settle] → write → RESUME sequence
 *   when changing voltage or frequency. This is handled internally via a per-pump
 *   pending-update state machine driven by update_Pumps(). Callers never block.
 *
 * mp-Lowdriver specs: 0-150 Vpp, 8-2000 Hz
 * SLF3S-0600F specs:  0-2000 µL/min
 */
class Microfluidics
{
public:
    /** Per-circuit pump configuration. Written by command parser in main.cpp. */
    struct PumpConfig
    {
        float flowRate_uLmin = 0.0f; // Desired flow rate (0-2000 µL/min); 0 = off
        bool pulsed = false;         // false = continuous, true = pulsed
        float feedTime_s = 0.0f;     // Feed duration per cycle  (pulsed mode)
        float pauseTime_s = 0.0f;    // Pause duration per cycle (pulsed mode)
        int cycles = 0;              // Total cycles to run; 0 = infinite
    };

private:
    static const uint8_t PUMP_I2C_ADDR = 0x59; // mp-Lowdriver default address
    static const uint8_t FLOW_I2C_ADDR = 0x08; // SLF3S-0600F default address
    static const int NUM_CIRCUITS = 2;
    static const int NUM_PUMPS = 4;
    static const int NUM_SENSORS = 2;

    static const uint8_t PUMP_MUX_CH[NUM_PUMPS]; // Defined in .cpp
    static const uint8_t FLOW_MUX_CH[NUM_SENSORS];

    // Flow rate → pump frequency mapping via manufacturer LUT (10–100 Hz at 100 Vpp)
    static const uint16_t FREQ_MIN_HZ = 8;
    static const uint16_t FREQ_MAX_HZ = 800;
    // mp-Lowdriver frequency register: 1 LSB = 7.8125 Hz
    static constexpr float HZ_PER_BIT = 7.8125f;
    // Required settle time between STOP and parameter write (mp-Lowdriver datasheet)
    static const unsigned long PUMP_SETTLE_MS = 5;

    // PID feedback — feedforward + PI correction on fluid pump amplitude.
    // Frequency is fixed at FLUID_FREQ_BYTE; amplitude byte (0-255 → 0-100 Vpp, GAIN=3)
    // is the PID control variable.
    static constexpr float PID_KP = 0.04f;            // ampByte / (µL/min)
    static constexpr float PID_KI = 0.025f;           // ampByte / (µL/min·s)
    static const unsigned long PID_INTERVAL_MS = 100; // Sample period
    // Max amplitude change per PID tick — limits abrupt startup transients
    static constexpr float PID_SLEW_BYTES_PER_TICK = 15.0f; // bytes / 100 ms → 150 bytes/s

    // Fixed frequency for fluid pumps: 26 × 7.8125 Hz = 203.125 Hz
    static const uint8_t FLUID_FREQ_BYTE = 26;

    // Fixed operating point for bubble-removal pumps (run whenever fluid pump is active)
    static const uint8_t BUBBLE_FREQ_BYTE = 20; // ≈ 156 Hz
    static const uint8_t BUBBLE_VOLTAGE = 168;  // ≈ 100 Vpp

    // Priming mode: ~305 Hz + 150 Vpp (hardware ceiling at GAIN=3)
    // 300 / 7.8125 = 38.4 → rounded to 39 → 39 × 7.8125 ≈ 305 Hz
    static const uint8_t PRIMING_FREQ_BYTE = 39;
    static const uint8_t PRIMING_VOLT_BYTE = 220; // pulled back from the 255 hardware ceiling to 86.3% of total capacity

    // ---- Per-circuit state ----
    bool _primingActive[NUM_CIRCUITS]; // true while priming mode is active for that circuit
    PumpConfig _config[NUM_CIRCUITS];
    unsigned long _lastCycleTime[NUM_CIRCUITS];
    int _cycleCount[NUM_CIRCUITS];

    // Per-circuit PID state; reset whenever set_Circuit_Config() is called
    struct PidState
    {
        float integral = 0.0f;
        float prevError = 0.0f;
        unsigned long lastMs = 0;
        float outputAmpByte = 0.0f; // Raw PID output [0-255]
        float slewedAmpByte = 0.0f; // Rate-limited version applied to hardware
    };
    PidState _pid[NUM_CIRCUITS];
    float _lastFlowReading[NUM_CIRCUITS]; // Cached sensor value written by PID tick
    float _lastTempReading[NUM_CIRCUITS]; // Cached liquid temperature written by read_Flow_Rate

    // ---- Non-blocking pump update state machine (one entry per physical pump) ----
    struct PumpUpdate
    {
        bool active = false;
        uint8_t voltage = 0;
        uint8_t freqByte = 0;
        bool hasVoltage = false;
        bool hasFreq = false;
        unsigned long stopSentAt = 0;
    };
    PumpUpdate _pending[NUM_PUMPS];
    uint8_t _curVoltage[NUM_PUMPS];
    uint8_t _curFreqByte[NUM_PUMPS];

    // ---- Low-level hardware helpers ----
    void select_Mux_Bus(uint8_t muxChannel);
    void set_Pump_Register_Page(uint8_t page);
    void send_Pump_Stop(int pumpIdx);
    void send_Pump_Resume(int pumpIdx);
    void apply_Pending_Update(int pumpIdx);
    void init_Single_Pump(int pumpIdx);
    void init_Single_Flow_Sensor(int sensorIdx);

    // ---- Pending-update queue helpers ----
    void queue_Pump_Voltage(int pumpIdx, uint8_t voltage);
    void queue_Pump_Freq_Byte(int pumpIdx, uint8_t freqByte);

    // ---- Circuit logic helpers ----
    uint8_t flow_Rate_To_Amp_Byte(float flowRate_uLmin) const;
    void stop_Circuit_Pumps(int circuitIdx);
    void update_Fluid_Pump_Pid(int circuitIdx); // Reads sensor, runs PID, updates _pid
    void apply_Circuit_Continuous(int circuitIdx);
    void apply_Circuit_Pulsed(int circuitIdx);

public:
    Microfluidics();

    /** Initialises all 4 pumps and 2 flow sensors. Call once in setup(). */
    void begin();

    /**
     * Sets the configuration for a fluidic circuit (1 or 2).
     * Resets pulsed-mode counters when called.
     */
    void set_Circuit_Config(int circuit, const PumpConfig &config);

    /**
     * Non-blocking update: processes pending pump parameter changes (stop→settle→write→resume)
     * and applies circuit continuous/pulsed logic. Call every loop().
     */
    void update_Pumps();

    /**
     * Activates or deactivates priming mode for a circuit (1 or 2).
     * Active  : sets both pumps of the circuit to ~300 Hz + 150 Vpp and suspends PID.
     * Inactive: clears the flag; caller must follow up with set_Circuit_Config() to
     *           restore normal frequency and flow-rate control.
     */
    void set_Priming(int circuit, bool active);

    /**
     * Immediately queues voltage = 0 for all 4 pumps and clears any active priming.
     * Called when the microfluidics interlock is opened from the UI.
     */
    void stop_All();

    /**
     * Reads flow rate from sensor 1 or 2 via I2C. Returns µL/min.
     * Also caches the liquid temperature (accessible via get_Last_Temp_Reading).
     */
    float read_Flow_Rate(int sensorNum);

    /**
     * Returns the last flow reading cached by the PID tick for circuit 1 or 2.
     * Use this in telemetry to avoid a redundant I2C transaction.
     */
    float get_Last_Flow_Reading(int circuitNum) const;

    /**
     * Returns the liquid temperature (°C) cached during the last read_Flow_Rate
     * call for circuit 1 or 2. Scale factor from SLF3S-0600F datasheet: 200 LSB/°C.
     */
    float get_Last_Temp_Reading(int circuitNum) const;

    /**
     * Drives the non-blocking STOP→settle→write→RESUME state machine.
     * Use instead of update_Pumps() when you want direct pump control
     * without the PID or circuit logic running (e.g. hardware tests).
     */
    void process_Pending_Updates();

    // Direct low-level pump control (bypasses circuit logic and PID)
    void set_Pump_Voltage(int pumpNum, uint8_t voltage);
    void set_Pump_Frequency(int pumpNum, uint16_t frequency_Hz);
};

#endif // MICROFLUIDICS_H