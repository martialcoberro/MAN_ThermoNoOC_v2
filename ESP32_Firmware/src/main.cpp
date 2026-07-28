/**
 * @file main.cpp
 * @brief ThermoNoOC — top-level orchestrator.
 *
 * Each module owns exactly one domain. This file is the only place where
 * modules are instantiated and their data is wired together. No domain logic
 * lives here — only setup/loop coordination and WebSocket command dispatch.
 */

#include <Arduino.h>
#include <esp_system.h>
#include "WiFiManager.h"
#include "Control.h"
#include "Incubator.h"
#include "LED_Array.h"
#include "Microfluidics.h"

// ---------------------------------------------------------------------------
// Network configuration
// ---------------------------------------------------------------------------
static const char *WIFI_SSID = "ThermoNoOC";
static const char *WIFI_PASSWORD = "thermonooc";
static const IPAddress AP_IP(192, 168, 4, 1);
static const IPAddress AP_GW(192, 168, 4, 1);
static const IPAddress AP_SN(255, 255, 255, 0);
static const uint16_t WS_PORT = 5000;

// ---------------------------------------------------------------------------
// Module instances — one per domain, no cross-ownership
// ---------------------------------------------------------------------------
WiFiManager wifi(WIFI_SSID, WIFI_PASSWORD, WS_PORT);
Control control;
Incubator incubator;
LED_Array leds;
Microfluidics fluidics;

// ---------------------------------------------------------------------------
// Safety interlock — all sensor/actuator activity is gated behind this flag.
// It is set to true only when the UI confirms the incubator lid is closed.
// ---------------------------------------------------------------------------
static bool is_Incubator_Closed = false;

// Microfluidics interlock — pumps and flow sensors are inhibited while the
// microfluidics circuit is open. Independent of the incubator interlock.
static bool is_Micro_Closed = false;

// ---------------------------------------------------------------------------
// Telemetry timing — single 1 s broadcast containing all sensor data
// ---------------------------------------------------------------------------
static unsigned long lastTelemetryMs = 0;
static const unsigned long TELEMETRY_INTERVAL_MS = 1000;

// ---------------------------------------------------------------------------
// WebSocket command handler (registered in setup, executed by WiFiManager::loop)
// ---------------------------------------------------------------------------
void on_WebSocket_Event(uint8_t clientNum, WStype_t type, uint8_t *payload, size_t length)
{
    if (type != WStype_TEXT || length == 0)
        return;

    String msg = String((char *)payload);
    Serial.print("[WS RX] '"); Serial.print(msg); Serial.println("'");

    // --- Temperature setpoint ---
    // Expected format: "SET_TEMP:37.5"
    if (msg.startsWith("SET_TEMP:"))
        incubator.targetTemperature = msg.substring(9).toFloat();

    // --- LED group control ---
    // Expected format: "SET_LED:1:1:75"  (group, enabled 0/1, intensity 0-100)
    else if (msg.startsWith("SET_LED:"))
    {
        int g1 = msg.indexOf(':');
        int g2 = msg.indexOf(':', g1 + 1);
        int g3 = msg.indexOf(':', g2 + 1);
        int group = msg.substring(g1 + 1, g2).toInt();
        bool enabled = msg.substring(g2 + 1, g3).toInt() != 0;
        uint8_t inten = (uint8_t)msg.substring(g3 + 1).toInt();
        leds.set_Group_Enabled(group, enabled);
        leds.set_Group_Intensity(group, inten);
    }

    // --- Incubator safety interlock ---
    // Expected format: "SET_INCUBATOR:1" (closed) / "SET_INCUBATOR:0" (opened)
    else if (msg.startsWith("SET_INCUBATOR:"))
        is_Incubator_Closed = msg.substring(14).toInt() != 0;

    // --- Microfluidics interlock ---
    // Expected format: "SET_MICRO:1" (closed) / "SET_MICRO:0" (opened)
    else if (msg.startsWith("SET_MICRO:"))
    {
        bool closed = msg.substring(10).toInt() != 0;
        if (!closed && is_Micro_Closed)
            fluidics.stop_All(); // Immediately kill pumps when circuit is opened
        is_Micro_Closed = closed;
    }

    // --- Pump circuit configuration ---
    // Expected format: "SET_PUMP:1:500.0:0:0:0:0"
    //   fields: circuit, flowRate_uLmin, pulsed, feedTime_s, pauseTime_s, cycles
    else if (msg.startsWith("SET_PUMP:"))
    {
        int f[6]; // field start positions
        f[0] = msg.indexOf(':');
        for (int i = 1; i < 6; i++)
            f[i] = msg.indexOf(':', f[i - 1] + 1);

        Microfluidics::PumpConfig cfg;
        int circuit = msg.substring(f[0] + 1, f[1]).toInt();
        cfg.flowRate_uLmin = msg.substring(f[1] + 1, f[2]).toFloat();
        cfg.pulsed = msg.substring(f[2] + 1, f[3]).toInt() != 0;
        cfg.feedTime_s = msg.substring(f[3] + 1, f[4]).toFloat();
        cfg.pauseTime_s = msg.substring(f[4] + 1, f[5]).toFloat();
        cfg.cycles = msg.substring(f[5] + 1).toInt();
        fluidics.set_Circuit_Config(circuit, cfg);
    }

    // --- Priming mode ---
    // Expected format: "SET_PRIMING:1:1"  (circuit 1 or 2, active 0/1)
    // Gated on is_Micro_Closed: no hardware action while the circuit is open.
    else if (msg.startsWith("SET_PRIMING:") && is_Micro_Closed)
    {
        int f1 = msg.indexOf(':');
        int f2 = msg.indexOf(':', f1 + 1);
        int circuit = msg.substring(f1 + 1, f2).toInt();
        bool active = msg.substring(f2 + 1).toInt() != 0;
        fluidics.set_Priming(circuit, active);
    }
}

// ---------------------------------------------------------------------------
// Telemetry JSON builder — all sensor data in one 1 s broadcast
// ---------------------------------------------------------------------------
String build_Telemetry_JSON()
{
    static char buf[384];   // static: reused every call, no repeated malloc/free
    snprintf(buf, sizeof(buf),
        "{\"incClosed\":%s,\"microClosed\":%s,"
        "\"temp1\":%.2f,\"hum1\":%.1f,\"temp2\":%.2f,\"hum2\":%.1f,"
        "\"uvIndex\":%.3f,\"uvW\":%.4f,\"co2\":%.4f,"
        "\"flow1\":%.1f,\"flow2\":%.1f,"
        "\"fluidTemp1\":%.1f,\"fluidTemp2\":%.1f}",
        is_Incubator_Closed ? "true" : "false",
        is_Micro_Closed ? "true" : "false",
        incubator.temp1, incubator.hum1, incubator.temp2, incubator.hum2,
        incubator.uvIndex, incubator.uvIrradiance, incubator.co2Percent,
        fluidics.get_Last_Flow_Reading(1), fluidics.get_Last_Flow_Reading(2),
        fluidics.get_Last_Temp_Reading(1), fluidics.get_Last_Temp_Reading(2));
    return String(buf);   // one single allocation, instead of ~11 growing reallocations
}

// ---------------------------------------------------------------------------
// setup / loop
// ---------------------------------------------------------------------------
void setup()
{
    Serial.begin(115200);
    delay(200);
    Serial.print("[Boot] Reset reason: ");
    switch (esp_reset_reason())
    {
        case ESP_RST_POWERON:  Serial.println("Power-on"); break;
        case ESP_RST_EXT:      Serial.println("External reset pin"); break;
        case ESP_RST_SW:       Serial.println("Software reset"); break;
        case ESP_RST_PANIC:    Serial.println("PANIC / crash"); break;
        case ESP_RST_INT_WDT:  Serial.println("Interrupt watchdog"); break;
        case ESP_RST_TASK_WDT: Serial.println("Task watchdog (loop() blocked too long)"); break;
        case ESP_RST_WDT:      Serial.println("Other watchdog"); break;
        case ESP_RST_BROWNOUT: Serial.println("Brownout (power supply dip)"); break;
        default:                Serial.println("Other"); break;
    }

    wifi.begin(AP_IP, AP_GW, AP_SN);
    wifi.server().onEvent(on_WebSocket_Event);

    control.begin();
    incubator.begin();
    leds.begin();
    fluidics.begin();
}

void loop()
{
    // 1. WiFi always runs — needed to receive SET_INCUBATOR and other commands
    wifi.loop();

    // 2. Always active: fan and LEDs have no interlock requirement.
    control.update_Fan_Speed(control.read_PCB_Temperature());
    leds.update_All_Groups();

    // 3. Incubator gate: environmental sensors and ITO heater only when lid is closed.
    if (is_Incubator_Closed)
    {
        incubator.read_All_Sensors();
        incubator.update_Heater_PWM();
    }

    // 4. Microfluidics gate: PID reads flow sensor and adjusts pump frequency every 100 ms.
    if (is_Micro_Closed)
        fluidics.update_Pumps();

    // 5. Telemetry always broadcasts so the UI reflects the current interlock state
    unsigned long now = millis();
    if (now - lastTelemetryMs >= TELEMETRY_INTERVAL_MS)
    {
        lastTelemetryMs = now;
        wifi.broadcast(build_Telemetry_JSON());
        Serial.print("[Heap] free=");
        Serial.println(ESP.getFreeHeap());
    }
}