#include "Microfluidics.h"

const uint8_t Microfluidics::PUMP_MUX_CH[4] = {
    MUX_CH_PUMP_1, MUX_CH_PUMP_2, MUX_CH_PUMP_3, MUX_CH_PUMP_4};
const uint8_t Microfluidics::FLOW_MUX_CH[2] = {
    MUX_CH_FLOW_1, MUX_CH_FLOW_2};

Microfluidics::Microfluidics()
{
    for (int i = 0; i < NUM_CIRCUITS; i++)
    {
        _primingActive[i] = false;
        _lastCycleTime[i] = 0;
        _cycleCount[i] = 0;
        _lastFlowReading[i] = 0.0f;
        _lastTempReading[i] = 0.0f;
        _pid[i] = PidState{};
    }
    for (int i = 0; i < NUM_PUMPS; i++)
    {
        _curVoltage[i] = 0;
        _curFreqByte[i] = 0;
    }
}

// ---------------------------------------------------------------------------
// Low-level hardware helpers
// ---------------------------------------------------------------------------

void Microfluidics::select_Mux_Bus(uint8_t muxChannel)
{
    Wire.beginTransmission(MUX_ADDR_MICROFLUIDICS);
    Wire.write(1 << muxChannel);
    Wire.endTransmission();
}

void Microfluidics::set_Pump_Register_Page(uint8_t page)
{
    Wire.beginTransmission(PUMP_I2C_ADDR);
    Wire.write(0xFF);
    Wire.write(page);
    Wire.endTransmission();
}

void Microfluidics::send_Pump_Stop(int pumpIdx)
{
    select_Mux_Bus(PUMP_MUX_CH[pumpIdx]);
    set_Pump_Register_Page(0x00);
    Wire.beginTransmission(PUMP_I2C_ADDR);
    Wire.write(0x02);
    Wire.write(0x00); // STOP
    Wire.endTransmission();
}

void Microfluidics::send_Pump_Resume(int pumpIdx)
{
    select_Mux_Bus(PUMP_MUX_CH[pumpIdx]);
    set_Pump_Register_Page(0x00);
    Wire.beginTransmission(PUMP_I2C_ADDR);
    Wire.write(0x02);
    Wire.write(0x01); // RESUME
    Wire.endTransmission();
}

void Microfluidics::apply_Pending_Update(int pumpIdx)
{
    select_Mux_Bus(PUMP_MUX_CH[pumpIdx]);
    set_Pump_Register_Page(0x01);

    if (_pending[pumpIdx].hasVoltage)
    {
        Wire.beginTransmission(PUMP_I2C_ADDR);
        Wire.write(0x06);
        Wire.write(_pending[pumpIdx].voltage);
        Wire.endTransmission();
        _curVoltage[pumpIdx] = _pending[pumpIdx].voltage;
        _pending[pumpIdx].hasVoltage = false;
    }

    if (_pending[pumpIdx].hasFreq)
    {
        Wire.beginTransmission(PUMP_I2C_ADDR);
        Wire.write(0x07);
        Wire.write(_pending[pumpIdx].freqByte);
        Wire.endTransmission();
        _curFreqByte[pumpIdx] = _pending[pumpIdx].freqByte;
        _pending[pumpIdx].hasFreq = false;
    }

    send_Pump_Resume(pumpIdx);
    _pending[pumpIdx].active = false;
}

void Microfluidics::init_Single_Pump(int pumpIdx)
{
    select_Mux_Bus(PUMP_MUX_CH[pumpIdx]);

    // Control registers: GAIN=3 (100 Vpp full-scale), wake device, sequence plays waveform #1
    set_Pump_Register_Page(0x00);
    Wire.beginTransmission(PUMP_I2C_ADDR);
    Wire.write(0x01); // Start at reg 0x01
    Wire.write(0x03); // reg 0x01: GAIN=3 → 0-100 Vpp, digital input
    Wire.write(0x00); // reg 0x02: STANDBY=0, GO=0 (awake, not yet playing)
    Wire.write(0x01); // reg 0x03: play waveform ID #1
    Wire.write(0x00); // reg 0x04: end of sequence
    Wire.endTransmission();

    // RAM page 1: header + initial waveform synthesis packet
    set_Pump_Register_Page(0x01);
    Wire.beginTransmission(PUMP_I2C_ADDR);
    Wire.write(0x00);            // Start at RAM address 0x00
    Wire.write(0x05);            // Header size (5 = last header byte index for 1 waveform)
    Wire.write(0x80);            // Start addr upper byte: 0x80 = waveform synthesis mode
    Wire.write(0x06);            // Start addr lower byte
    Wire.write(0x00);            // Stop addr upper byte
    Wire.write(0x09);            // Stop addr lower byte
    Wire.write(0x00);            // Repeat count: 0 = infinite loop
    Wire.write(0x00);            // 0x06: amplitude = 0 (stopped at init)
    Wire.write(FLUID_FREQ_BYTE); // 0x07: frequency ~203 Hz
    Wire.write(100);             // 0x08: 100 cycles per play (≈ 0.5 s at 203 Hz, repeats infinitely)
    Wire.write(0x00);            // 0x09: no envelope
    Wire.endTransmission();
}

void Microfluidics::init_Single_Flow_Sensor(int sensorIdx)
{
    select_Mux_Bus(FLOW_MUX_CH[sensorIdx]);
    Wire.beginTransmission(FLOW_I2C_ADDR);
    Wire.write(0x36);
    Wire.write(0x08); // Start continuous measurement
    Wire.endTransmission();
}

// ---------------------------------------------------------------------------
// Pending-update queue — non-blocking STOP → settle → write → RESUME
// ---------------------------------------------------------------------------

void Microfluidics::queue_Pump_Voltage(int pumpIdx, uint8_t voltage)
{
    if (_pending[pumpIdx].active)
    {
        // STOP already in flight; just update the queued value
        _pending[pumpIdx].voltage = voltage;
        _pending[pumpIdx].hasVoltage = true;
        return;
    }
    if (_curVoltage[pumpIdx] == voltage)
        return; // No change needed

    _pending[pumpIdx].voltage = voltage;
    _pending[pumpIdx].hasVoltage = true;
    _pending[pumpIdx].active = true;
    _pending[pumpIdx].stopSentAt = millis();
    send_Pump_Stop(pumpIdx);
}

void Microfluidics::queue_Pump_Freq_Byte(int pumpIdx, uint8_t freqByte)
{
    if (_pending[pumpIdx].active)
    {
        _pending[pumpIdx].freqByte = freqByte;
        _pending[pumpIdx].hasFreq = true;
        return;
    }
    if (_curFreqByte[pumpIdx] == freqByte)
        return;

    _pending[pumpIdx].freqByte = freqByte;
    _pending[pumpIdx].hasFreq = true;
    _pending[pumpIdx].active = true;
    _pending[pumpIdx].stopSentAt = millis();
    send_Pump_Stop(pumpIdx);
}

void Microfluidics::process_Pending_Updates()
{
    for (int i = 0; i < NUM_PUMPS; i++)
    {
        if (_pending[i].active && (millis() - _pending[i].stopSentAt >= PUMP_SETTLE_MS))
            apply_Pending_Update(i);
    }
}

// ---------------------------------------------------------------------------
// Circuit logic helpers
// ---------------------------------------------------------------------------

uint8_t Microfluidics::flow_Rate_To_Amp_Byte(float flowRate_uLmin) const
{
    // Feedforward table: flow rate → amplitude byte at fixed ~203 Hz, GAIN=3 (100 Vpp).
    // Values scaled to ~85% of full range to leave headroom for the PI trim term.
    // If steady-state error is consistently large, re-calibrate with real pump data.
    static const float FLOW_LUT[] = {0.f, 200.f, 500.f, 900.f, 1400.f, 2000.f};
    static const float AMP_LUT[] = {0.f, 68.f, 110.f, 153.f, 187.f, 217.f};
    static const int N = 6;

    float amp;
    if (flowRate_uLmin <= FLOW_LUT[0])
        amp = AMP_LUT[0];
    else if (flowRate_uLmin >= FLOW_LUT[N - 1])
        amp = AMP_LUT[N - 1];
    else
    {
        amp = AMP_LUT[N - 1];
        for (int i = 0; i < N - 1; i++)
        {
            if (flowRate_uLmin <= FLOW_LUT[i + 1])
            {
                float t = (flowRate_uLmin - FLOW_LUT[i]) / (FLOW_LUT[i + 1] - FLOW_LUT[i]);
                amp = AMP_LUT[i] + t * (AMP_LUT[i + 1] - AMP_LUT[i]);
                break;
            }
        }
    }

    return (uint8_t)constrain(amp, 0.f, 255.f);
}

// Pump index helpers — circuit layout:
//   Circuit 0 (UI circuit 1): pump 0 = fluid (pump 1), pump 2 = bubble (pump 3)
//   Circuit 1 (UI circuit 2): pump 1 = fluid (pump 2), pump 3 = bubble (pump 4)
static inline int fluid_Pump(int circuitIdx) { return circuitIdx; }
static inline int bubble_Pump(int circuitIdx) { return circuitIdx + 2; }

void Microfluidics::stop_Circuit_Pumps(int circuitIdx)
{
    queue_Pump_Voltage(fluid_Pump(circuitIdx), 0);
    queue_Pump_Voltage(bubble_Pump(circuitIdx), 0);
}

// ---------------------------------------------------------------------------
// PID — feedforward + PI correction; runs at PID_INTERVAL_MS cadence.
// Reads the flow sensor for this circuit, updates _lastFlowReading, and
// writes a new outputAmpByte into _pid[circuitIdx].
// ---------------------------------------------------------------------------
void Microfluidics::update_Fluid_Pump_Pid(int circuitIdx)
{
    unsigned long now = millis();
    PidState &pid = _pid[circuitIdx];

    if (pid.lastMs != 0 && (now - pid.lastMs) < PID_INTERVAL_MS)
        return; // Not time yet — keep using previous output

    float dt = (pid.lastMs == 0) ? (PID_INTERVAL_MS / 1000.0f)
                                 : (now - pid.lastMs) / 1000.0f;
    pid.lastMs = now;

    // Sample flow sensor (circuit 0 → sensor 1, circuit 1 → sensor 2)
    float measured = read_Flow_Rate(circuitIdx + 1);
    _lastFlowReading[circuitIdx] = measured;

    float setpoint = _config[circuitIdx].flowRate_uLmin;
    float error = setpoint - measured;

    // Feedforward gives a calibrated starting amplitude; PI trims steady-state error
    float ff = (float)flow_Rate_To_Amp_Byte(setpoint);

    float newIntegral = pid.integral + error * dt;
    float rawOutput = ff + PID_KP * error + PID_KI * newIntegral;

    float clamped = constrain(rawOutput, 0.0f, 255.0f);

    // Anti-windup: only integrate when output is not saturated
    if (clamped == rawOutput)
        pid.integral = newIntegral;

    pid.prevError = error;
    pid.outputAmpByte = clamped;

    // Slew-rate limiter: prevents abrupt amplitude jumps on setpoint changes / startup
    float delta = pid.outputAmpByte - pid.slewedAmpByte;
    delta = constrain(delta, -PID_SLEW_BYTES_PER_TICK, PID_SLEW_BYTES_PER_TICK);
    pid.slewedAmpByte = constrain(pid.slewedAmpByte + delta, 0.0f, 255.0f);
}

void Microfluidics::apply_Circuit_Continuous(int circuitIdx)
{
    update_Fluid_Pump_Pid(circuitIdx);

    uint8_t fluidAmp = (uint8_t)_pid[circuitIdx].slewedAmpByte;
    int fp = fluid_Pump(circuitIdx);

    // Frequency is fixed; queue_Pump_Freq_Byte is a no-op after the first write
    queue_Pump_Freq_Byte(fp, FLUID_FREQ_BYTE);

    // Only push amplitude update when change exceeds deadband (>1 byte ≈ 0.4 Vpp)
    // to avoid STOP/RESUME cycles from single-bit PID jitter
    if (_curVoltage[fp] == 0 || abs((int)fluidAmp - (int)_curVoltage[fp]) > 1)
        queue_Pump_Voltage(fp, fluidAmp);

    // Bubble-removal pump: fixed frequency/voltage whenever fluid pump is running
    queue_Pump_Freq_Byte(bubble_Pump(circuitIdx), BUBBLE_FREQ_BYTE);
    queue_Pump_Voltage(bubble_Pump(circuitIdx), BUBBLE_VOLTAGE);
}

void Microfluidics::apply_Circuit_Pulsed(int circuitIdx)
{
    const PumpConfig &cfg = _config[circuitIdx];

    // Infinite when cycles == 0; stop once all cycles complete
    if (cfg.cycles > 0 && _cycleCount[circuitIdx] >= cfg.cycles)
    {
        stop_Circuit_Pumps(circuitIdx);
        return;
    }

    unsigned long now = millis();
    unsigned long elapsed = now - _lastCycleTime[circuitIdx];
    unsigned long feedMs = (unsigned long)(cfg.feedTime_s * 1000.0f);
    unsigned long cycleMs = feedMs + (unsigned long)(cfg.pauseTime_s * 1000.0f);

    if (elapsed >= cycleMs)
    {
        _cycleCount[circuitIdx]++;
        _lastCycleTime[circuitIdx] = now;
        elapsed = 0;
    }

    if (elapsed < feedMs)
        apply_Circuit_Continuous(circuitIdx); // Feed phase — PID active
    else
    {
        stop_Circuit_Pumps(circuitIdx); // Pause phase — both pumps off
        _lastFlowReading[circuitIdx] = 0.0f;
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void Microfluidics::begin()
{
    for (int i = 0; i < NUM_PUMPS; i++)
        init_Single_Pump(i);
    for (int i = 0; i < NUM_SENSORS; i++)
        init_Single_Flow_Sensor(i);
    delay(60); // SLF3S-0600F: 60 ms warm-up after start continuous measurement
}

void Microfluidics::set_Circuit_Config(int circuit, const PumpConfig &config)
{
    if (circuit < 1 || circuit > NUM_CIRCUITS)
        return;
    int idx = circuit - 1;
    _config[idx] = config;
    _cycleCount[idx] = 0;
    _lastCycleTime[idx] = millis();
    _pid[idx] = PidState{}; // Reset PID so new setpoint starts fresh
}

void Microfluidics::set_Priming(int circuit, bool active)
{
    if (circuit < 1 || circuit > NUM_CIRCUITS)
        return;
    int idx = circuit - 1;
    _primingActive[idx] = active;

    if (active)
    {
        _pid[idx] = PidState{}; // Freeze PID so it does not fight the fixed-output priming
        int fp = fluid_Pump(idx);
        int bp = bubble_Pump(idx);
        // Queue freq first so voltage piggybacks on the same STOP/RESUME cycle
        queue_Pump_Freq_Byte(fp, PRIMING_FREQ_BYTE);
        queue_Pump_Voltage(fp, PRIMING_VOLT_BYTE);
        queue_Pump_Freq_Byte(bp, PRIMING_FREQ_BYTE);
        queue_Pump_Voltage(bp, PRIMING_VOLT_BYTE);
    }
    // Deactivation: flag cleared; update_Pumps() resumes normal circuit logic on next tick.
    // main.cpp follows up with SET_PUMP which calls set_Circuit_Config(), resetting PID
    // and restoring the user-configured frequency and flow rate.
}

void Microfluidics::stop_All()
{
    for (int i = 0; i < NUM_CIRCUITS; i++)
        _primingActive[i] = false; // Interlock opened — clear priming so PID resumes cleanly
    for (int i = 0; i < NUM_PUMPS; i++)
        queue_Pump_Voltage(i, 0);
    for (int i = 0; i < NUM_CIRCUITS; i++)
        _pid[i] = PidState{}; // Reset PID so next run starts clean
}

void Microfluidics::update_Pumps()
{
    process_Pending_Updates();

    for (int i = 0; i < NUM_CIRCUITS; i++)
    {
        if (_primingActive[i])
            continue; // Priming holds fixed freq/voltage; circuit logic and PID are suspended

        if (_config[i].flowRate_uLmin <= 0.0f)
        {
            stop_Circuit_Pumps(i);
            _lastFlowReading[i] = 0.0f;
        }
        else if (_config[i].pulsed)
            apply_Circuit_Pulsed(i);
        else
            apply_Circuit_Continuous(i);
    }
}

float Microfluidics::read_Flow_Rate(int sensorNum)
{
    if (sensorNum < 1 || sensorNum > NUM_SENSORS)
        return 0.0f;
    int idx = sensorNum - 1;
    select_Mux_Bus(FLOW_MUX_CH[idx]);
    // Request 6 bytes: flow (2 + CRC) + temperature (2 + CRC)
    unsigned long t0 = micros();
    Wire.requestFrom(FLOW_I2C_ADDR, (uint8_t)6);
    unsigned long dt = micros() - t0;
    if (dt > 5000)  // flag anything over 5 ms — a healthy read should be well under 1 ms
        Serial.printf("[Flow] sensor %d requestFrom took %lu us\n", sensorNum, dt);
    if (Wire.available() >= 6)
    {
        int16_t flowRaw = (int16_t)((Wire.read() << 8) | Wire.read());
        Wire.read(); // discard flow CRC
        int16_t tempRaw = (int16_t)((Wire.read() << 8) | Wire.read());
        Wire.read();                                     // discard temperature CRC
        _lastTempReading[idx] = (float)tempRaw / 200.0f; // SLF3S-0600F: 200 LSB/°C
        return (float)flowRaw / 10.0f;                   // SLF3S-0600F: 10 LSB/(µL/min)
    }
    return 0.0f;
}

float Microfluidics::get_Last_Flow_Reading(int circuitNum) const
{
    if (circuitNum < 1 || circuitNum > NUM_CIRCUITS)
        return 0.0f;
    return _lastFlowReading[circuitNum - 1];
}

float Microfluidics::get_Last_Temp_Reading(int circuitNum) const
{
    if (circuitNum < 1 || circuitNum > NUM_CIRCUITS)
        return 0.0f;
    return _lastTempReading[circuitNum - 1];
}

void Microfluidics::set_Pump_Voltage(int pumpNum, uint8_t voltage)
{
    if (pumpNum < 1 || pumpNum > NUM_PUMPS)
        return;
    queue_Pump_Voltage(pumpNum - 1, voltage);
}

void Microfluidics::set_Pump_Frequency(int pumpNum, uint16_t frequency_Hz)
{
    if (pumpNum < 1 || pumpNum > NUM_PUMPS)
        return;
    uint8_t freqByte = (uint8_t)(frequency_Hz / HZ_PER_BIT);
    queue_Pump_Freq_Byte(pumpNum - 1, (freqByte == 0) ? 1 : freqByte);
}