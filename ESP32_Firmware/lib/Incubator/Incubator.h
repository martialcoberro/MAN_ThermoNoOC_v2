#ifndef INCUBATOR_H
#define INCUBATOR_H

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SHT31.h>
#include <Adafruit_LTR390.h>
#include "Pinout.h"

/**
 * @class Incubator
 * @brief Manages the biological incubation environment.
 *
 * Single responsibility: dual SHT35 (temp/humidity), LTR390 (UV index,
 * optional — physically absent on current hardware revision), T6615
 * (CO2 via UART), and ITO glass heater PWM. No pump, LED, fan, or WiFi logic.
 *
 * CO2 reading is non-blocking: a two-state machine sends the command on one
 * loop tick and parses the response on a subsequent tick, with timeout handling.
 */
class Incubator
{
private:
    // ITO glass heater PWM (Channel 5 — channels 0-4 used by LEDs and fans)
    static const int ITO_FREQ_HZ = 5000;
    static const int ITO_RES_BIT = 8;
    static const int ITO_PWM_CH = 5;

    // PID gains — conservative starting values for a slow ambient thermal system
    // with a fast, low-thermal-mass ITO actuator.
    static constexpr float ITO_KP             = 3.0f;
    static constexpr float ITO_KI             = 0.05f;
    static constexpr float ITO_KD             = 2.0f;
    static constexpr float ITO_INTEGRAL_LIMIT = 40.0f;  // anti-windup clamp
    static constexpr float ITO_DERIV_ALPHA    = 0.1f;   // low-pass for noisy sensor

    // Hard PWM output cap. The glass reaches dangerous temperatures at duty
    // cycles as low as ~16 % for a SINGLE ~70 ohm glass (see test_ito_glass.cpp).
    // Both glasses are wired in PARALLEL to this same PWM output (see README),
    // which roughly HALVES total resistance and therefore roughly DOUBLES the
    // power drawn at any given duty cycle vs. a single glass. This cap is set
    // well under half of the original single-glass limit for that reason.
    // Re-tune upward only after measuring real current draw on the bench —
    // never raise it just because heating "feels slow".
    static const uint8_t ITO_PWM_MAX = 15;  // ~5.9 %

    // Forced cooling cycle — hard backstop against runaway glass temperature /
    // sustained overcurrent, independent of whatever the PID or bang-bang logic
    // below computes. After ITO_MAX_ON_MS of cumulative on-time, the glass is
    // forced off for ITO_MIN_OFF_MS no matter what.
    static const uint32_t ITO_MAX_ON_MS  = 5000;  // ms of cumulative on-time before forced cooling
    static const uint32_t ITO_MIN_OFF_MS = 3000;  // ms of forced off time

    enum class ITOPhase : uint8_t { HEATING, FORCED_COOL };

    // Bang-bang threshold: if error exceeds this, bypass PID and go full power
    // (still subject to ITO_PWM_MAX and the forced-cooling backstop above).
    static constexpr float ITO_BANGBANG_THRESHOLD = 2.0f;  // °C

    // Max setpoint change rate (°C/s) — limits power ramp to protect ITO glass
    static constexpr float ITO_RAMP_RATE_CS = 0.5f;

    // LTR390 sensitivity factors for Gain=18x, Resolution=20-bit
    static constexpr float LTR390_SENSITIVITY = 2300.0f;
    static constexpr float LTR390_UVI_TO_WM2 = 0.025f;

    // T6615 CO2 non-blocking state machine
    enum class CO2State : uint8_t
    {
        IDLE,
        CMD_SENT
    };
    static constexpr unsigned long CO2_TIMEOUT_MS = 500;

    static const uint8_t SHT35_ADDR = 0x45;   // confirmed via I2C scan — both units at 0x45

    Adafruit_SHT31 _sht1;
    Adafruit_SHT31 _sht2;
    Adafruit_LTR390 _ltr;
    bool _ltrPresent;   // false on current hardware — sensor removed, never polled if false

    CO2State _co2State;
    unsigned long _co2CmdTime;

    // PID state
    float         _integral;
    float         _prevError;
    float         _filteredDeriv;
    float         _rampedTarget;
    unsigned long _lastHeaterMs;

    // Forced cooling cycle state
    ITOPhase      _itoPhase;
    unsigned long _itoPhaseStartMs;
    unsigned long _itoOnAccumMs;

    void select_Sensor_Bus(uint8_t muxChannel);
    void read_SHT35_Sensors();
    void read_UV_Sensor();
    void tick_CO2_State_Machine();

public:
    // --- Live sensor readings (populated by read_All_Sensors()) ---
    float temp1, hum1;  // SHT35 #1 — temperature (°C) and humidity (%RH)
    float temp2, hum2;  // SHT35 #2
    float uvIndex;      // LTR390 UV Index (0-11+) — stays 0 if sensor not present
    float uvIrradiance; // LTR390 irradiance (W/m²) — stays 0 if sensor not present
    float co2Percent;   // T6615 CO₂ concentration (0-20 %)

    // --- Setpoint (written by command parser in main.cpp) ---
    float targetTemperature;

    Incubator();

    /** Initialises I2C bus, Serial2 for CO2, and ITO PWM. Call once in setup(). */
    void begin();

    /**
     * Non-blocking sensor poll. Reads SHT35 directly; reads LTR390 only if
     * it was detected at begin(); advances the CO2 state machine (send
     * command OR parse response). Call every loop().
     */
    void read_All_Sensors();

    /** Directly sets ITO glass heater duty cycle (0-255). */
    void set_ITO_Power(uint8_t power);

    /** PID controller with setpoint ramp. Drives ITO duty cycle toward targetTemperature. Call every loop(). */
    void update_Heater_PWM();
};

#endif // INCUBATOR_H