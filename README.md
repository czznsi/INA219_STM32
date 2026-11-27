# INA219 current and power logger

This STM32CubeIDE project measures battery-side current and input power for a DC/DC converter operating in bursts. By default, a NUCLEO-L452RE-P records **30,000 samples at 4 ms intervals** (250 samples/s for 120 s). A Windows tool downloads the completed capture over ST-LINK, saves the samples as CSV, and plots the current waveform. It is helpful to determine inrush/magnetization average current and average energy used by secondary load on DC/DC converter, especially when trying to optimise standby readiness that keeps DC/DC on for a period of time, and thus lowers power usage in physical standby battery applications.

## Bench connections

The firmware uses the STM32L452's hardware I²C1 peripheral and the INA219's 7-bit address `0x40`. The calculations assume a **0.1 Ω shunt** on the breakout.

| INA219 pin | Connect to |
| --- | --- |
| VCC / VS | Nucleo 3.3 V |
| GND | Common ground for the Nucleo, bench supply and circuit under test |
| SDA | PA10 (D0 on a factory-configured NUCLEO-L452RE-P) |
| SCL | PB8 (D15) |
| VIN+ | Positive terminal of the bench supply or battery |
| VIN− | Positive supply input of the circuit under test |

Current flows from VIN+ through the shunt to VIN−. The Nucleo connects to the PC through its ST-LINK USB port. [Nucleo connector pinout](https://www.st.com/resource/en/user_manual/dm00387966-stm32-nucleo-64-p-boards-stmicroelectronics.pdf#page=38)

## Run a measurement

1. Import the project into STM32CubeIDE, build the **Debug** configuration, and use **Debug** to flash the board. Once the download finishes, click **Terminate** to release ST-LINK.
2. On Windows, add Python, the STM32CubeProgrammer CLI (`STM32_Programmer_CLI`) and the Arm GNU tools (`arm-none-eabi-gdb` and `arm-none-eabi-objcopy`) to `PATH`. Install the Python dependencies with `python -m pip install numpy matplotlib`.
3. Start **INA219 - New measurement** from CubeIDE's **External Tools** menu or run the following command from the project directory.

   ```powershell
   python measurement.py --open
   ```

Each run resets the board and starts a fresh capture. The `--open` option opens the numbered plot when the capture finishes.

## How the capture works

**On the STM32.** At each sampling slot, the firmware writes `0x319B` to configuration register `0x00` to trigger one shunt-voltage conversion and one bus-voltage conversion, both at 12-bit resolution (±160 mV shunt range). It verifies the configuration on the first sample, polls the conversion-ready bit (`CNVR`) in bus register `0x02`, then reads shunt register `0x01`. Each entry in the RAM buffer `ina219_log` contains a signed 16-bit shunt reading and an unsigned 16-bit bus reading. [Firmware implementation](Core/Src/main.c)

The STM32 schedules samples with its millisecond tick. A missed deadline or failed I²C transfer ends the attempt. The firmware waits one second, attempts bus recovery if needed (up to nine SCL pulses), and retries from sample zero. In the debugger, `ina219_sample_count` shows progress. When `ina219_capture_status` reaches `HAL_OK`, the sample buffer and the averages `ina219_mean_current_mA` and `ina219_mean_power_mW` stay in RAM until reset.

**On the PC.** The Python tool reads the buffer layout and sampling interval from `Debug/INA219_test.elf` with GDB. It checks the board's flash against that build, resets the STM32, then polls its capture status over SWD. Once the expected sample count is complete, it downloads the buffer with STM32CubeProgrammer and decodes the records with NumPy. [Host implementation](measurement.py)

## Reading the results

Each CSV row represents one sample. The bus voltage is measured at VIN− relative to ground. Adding the signed shunt drop gives the source voltage at VIN+, which the code uses to calculate battery-side power. [INA219 register definitions](https://www.ti.com/lit/ds/symlink/ina219.pdf)

| CSV column | Calculation |
| --- | --- |
| `time_ms` | Sample index × sampling interval, starting at zero |
| `shunt_mV` | `shunt_raw * 0.01` (10 µV per count) |
| `bus_V` | `(bus_raw >> 3) * 0.004` (4 mV per count) |
| `battery_V` | `bus_V + shunt_mV / 1000` |
| `current_mA` | `shunt_raw * 0.1` with the 0.1 Ω shunt |
| `power_mW` | `battery_V * current_mA` |

The tool reports mean current and the mean of the per-sample power values. It detects a burst start when current reaches **15 mA or more** and at least **100 ms** have elapsed since the previous sample at or above that threshold. With two or more detected starts, it averages the samples from the first start up to, but excluding, the last start. It also reports the mean start-to-start period.

The project directory receives a numbered pair such as `INA219_capture_001.csv` and `INA219_capture_001.png`. The plot shows the full current trace and a close-up around the highest sampled current. `INA219_latest.png` gives the latest plot a stable filename.

## Changing the capture duration

Edit `INA219_SAMPLE_COUNT` or `INA219_SAMPLE_MS` in the firmware, then rebuild and flash the Debug configuration. The nominal duration is their product in milliseconds. Each additional sample needs 4 bytes of RAM, so the default buffer occupies 120,000 bytes. Increasing the interval extends the recording with the same buffer at a lower sampling rate. [Capture settings](Core/Src/main.c)

Build the checked-in C sources to reproduce this version. They use a 16 MHz I²C peripheral input clock and `TIMINGR = 0x1092232B`. The saved CubeMX configuration records 4 MHz and `0x00100D14`. Before regenerating code, synchronize the clock, I²C timing, pull-ups and pin labels (`INA_SDA` and `INA_SCL`) with the C sources. [CubeMX configuration](INA219_test.ioc) · [I²C pin initialization](Core/Src/stm32l4xx_hal_msp.c)
