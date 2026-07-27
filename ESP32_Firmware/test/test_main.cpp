/**
 * @file test_main.cpp
 * @brief Centralized Hardware Validation Runner.
 * * This file uses preprocessor directives to selectively compile and run
 * specific hardware tests. To run a test, change its corresponding
 * ENABLE macro from 0 to 1.
 * * NOTE: It is recommended to enable only one test at a time to avoid
 * peripheral or Serial monitor resource conflicts.
 */

#include <Arduino.h>

// Forward declarations of test setup and loop functions
// First PCB Test
void setup_first_pcb();
void loop_first_pcb();
// Control NTC Fans Test
void setup_control_ntc_fans();
void loop_control_ntc_fans();
// Environmental Sensors Test
void setup_environmental_sensors();
void loop_environmental_sensors();
// I2C Scanner Test
void setup_i2c_scanner();
void loop_i2c_scanner();
// LED Array Test
void setup_led_array();
void loop_led_array();
// Pumps Logic Test
void setup_pumps_logic();
void loop_pumps_logic();
// ITO Glass Test
void setup_ito_glass();
void loop_ito_glass();
// WiFi Communication Test
void setup_wifi_communication();
void loop_wifi_communication();

// Test Selection Flags
// Set to 1 to enable the specific test, 0 to skip it during compilation
#define ENABLE_FIRST_PCB_TEST 0
#define ENABLE_CONTROL_NTC_FANS_TEST 0
#define ENABLE_ENVIRONMENTAL_SENSORS_TEST 0
#define ENABLE_I2C_SCANNER_TEST 0
#define ENABLE_LED_ARRAY_TEST 0
#define ENABLE_PUMPS_LOGIC_TEST 0
#define ENABLE_ITO_GLASS_TEST 0
#define ENABLE_WIFI_COMMUNICATION_TEST 0

/**
 * @brief Main Setup function.
 * Dispatches to the initialization function of the enabled test module.
 */
void setup(void)
{
    // Initialize Serial communication for all tests
    Serial.begin(115200);
#if (ENABLE_FIRST_PCB_TEST == 1)
    setup_first_pcb();
#endif

#if (ENABLE_CONTROL_NTC_FANS_TEST == 1)
    setup_control_ntc_fans();
#endif

#if (ENABLE_ENVIRONMENTAL_SENSORS_TEST == 1)
    setup_environmental_sensors();
#endif

#if (ENABLE_I2C_SCANNER_TEST == 1)
    setup_i2c_scanner();
#endif

#if (ENABLE_LED_ARRAY_TEST == 1)
    setup_led_array();
#endif

#if (ENABLE_PUMPS_LOGIC_TEST == 1)
    setup_pumps_logic();
#endif

#if (ENABLE_ITO_GLASS_TEST == 1)
    setup_ito_glass();
#endif

#if (ENABLE_WIFI_COMMUNICATION_TEST == 1)
    setup_wifi_communication();
#endif
}

/**
 * @brief Main Loop function.
 * Dispatches to the repetitive logic of the enabled test module.
 */
void loop(void)
{
#if (ENABLE_FIRST_PCB_TEST == 1)
    loop_first_pcb();
#endif

#if (ENABLE_CONTROL_NTC_FANS_TEST == 1)
    loop_control_ntc_fans();
#endif

#if (ENABLE_ENVIRONMENTAL_SENSORS_TEST == 1)
    loop_environmental_sensors();
#endif

#if (ENABLE_I2C_SCANNER_TEST == 1)
    loop_i2c_scanner();
#endif

#if (ENABLE_LED_ARRAY_TEST == 1)
    loop_led_array();
#endif

#if (ENABLE_PUMPS_LOGIC_TEST == 1)
    loop_pumps_logic();
#endif

#if (ENABLE_ITO_GLASS_TEST == 1)
    loop_ito_glass();
#endif

#if (ENABLE_WIFI_COMMUNICATION_TEST == 1)
    loop_wifi_communication();
#endif
}