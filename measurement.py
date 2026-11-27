"""Capture INA219 samples over ST-LINK and export a CSV file and a plot for INA219_test."""
import argparse
import msvcrt
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


PROJECT = Path(__file__).resolve().parent
ELF = PROJECT / "Debug" / "INA219_test.elf"
RECORD = np.dtype([("shunt", "<i2"), ("bus", "<u2")])
SWD = ["STM32_Programmer_CLI", "-c", "port=SWD", "mode=HOTPLUG"]


def run(*args):
    result = subprocess.run(args, cwd=PROJECT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, encoding="utf-8",
                            errors="replace", timeout=30)
    if result.returncode or re.search(r"^\s*Error:", result.stdout, re.MULTILINE):
        hint = (" If a CubeIDE debug session is running, click Terminate and check the USB connection."
                if args[0] == SWD[0] else "")
        raise RuntimeError(result.stdout.strip() + hint)
    return result.stdout


def read_memory(address, size, path):
    path.unlink(missing_ok=True)
    run(*SWD, "-u", hex(address), str(size), str(path))
    data = path.read_bytes()
    if len(data) != size:
        raise RuntimeError("ST-LINK returned incomplete data.")
    return data


def verify_firmware(elf, flash, tmp):
    image = tmp / "build.bin"
    objcopy = ["arm-none-eabi-objcopy", "-O", "binary"]
    run(*objcopy, "--gap-fill=0x00", str(elf), str(image))
    expected = image.read_bytes()
    actual = read_memory(flash, len(expected), tmp / "flash.bin")
    if actual == expected:
        return

    # Only gaps between loadable sections change when objcopy's fill byte changes.
    run(*objcopy, "--gap-fill=0xff", str(elf), str(image))
    other_fill = image.read_bytes()
    for offset, value in enumerate(expected):
        if value == other_fill[offset] and value != actual[offset]:
            raise RuntimeError(f"The firmware on the board differs from the Debug build at address "
                               f"0x{flash + offset:08X}. In CubeIDE, use Debug to flash the firmware, "
                               "click Terminate to end the session, then repeat the capture.")


def capture_problem(probe, config, capture_status, tries, error):
    if tries == 0:
        return "The STM32 has not finished its first I²C connection check."
    if capture_status == 2:
        return ""  # Measurement is in progress.
    if probe != 0:
        stage = "The initial I²C transfer to address 0x40 failed."
    elif config != 0:
        if config == 1 and error == 0:
            return "The configuration register readback differs from the capture setting."
        stage = "Could not verify the INA219 configuration register."
    elif capture_status == 3 and error == 0:
        return "The STM32 missed the sampling deadline."
    else:
        stage = "Could not read a sample from the INA219."
    if error & 0x20:
        return f"{stage} The I²C transfer timed out (attempt {tries})."
    if error & 0x01:
        return f"{stage} I²C bus error (attempt {tries})."
    if error & 0x04:
        return f"{stage} The device did not acknowledge the transfer (NACK, attempt {tries})."
    return f"{stage} I²C error code 0x{error:08X} (attempt {tries})."


def capture(tmp):
    if not ELF.is_file():
        raise RuntimeError("First build and flash the INA219_test project in CubeIDE "
                           "using the Debug configuration.")
    elf = tmp / ELF.name
    shutil.copyfile(ELF, elf)  # Use the same build for both symbols and flash verification.
    # Read symbols and the sampling period from the actual Debug build, offline.
    query = ('printf "LAYOUT %u %u %u %u %u %u %u %u %u %u %u\\n", (unsigned)&g_pfnVectors, '
             '(unsigned)&ina219_log, sizeof(ina219_log), sizeof(ina219_log[0]), '
             '(unsigned)&ina219_capture_status, (unsigned)&ina219_sample_count, INA219_SAMPLE_MS, '
             '(unsigned)&ina219_status, (unsigned)&ina219_attempts, '
             '(unsigned)&ina219_config_status, (unsigned)&')
    gdb = ["arm-none-eabi-gdb", "-nx", "--batch", "-iex", "set auto-load off",
           str(elf), "-ex", "list INA219_Capture", "-ex"]
    try:
        output = run(*gdb, query + "ina219_i2c_error")
    except RuntimeError as exc:
        if 'No symbol "ina219_i2c_error"' not in str(exc):
            raise
        output = run(*gdb, query + "hi2c1.ErrorCode")  # Earlier hardware-I2C builds.
    layout = re.search(r"^LAYOUT ([\d ]+)\s*$", output, re.MULTILINE)
    if not layout:
        raise RuntimeError("Debug/INA219_test.elf does not contain the INA219 capture layout.")
    (flash, log, log_bytes, record_bytes, status, count, interval,
     probe, attempts, config_status, error) = map(int, layout[1].split())
    if record_bytes != RECORD.itemsize or log_bytes == 0 or interval == 0:
        raise RuntimeError("The STM32 sample format differs from the expected format.")

    verify_firmware(elf, flash, tmp)

    samples = log_bytes // record_bytes
    seconds = samples * interval / 1000
    print(f"Starting a capture of {samples} samples at {interval} ms intervals ({seconds:.2f} s).", flush=True)
    run(*SWD, "-rst")
    expected_end = time.monotonic() + seconds
    deadline = expected_end + max(15, 2 * seconds)
    time.sleep(min(4, seconds))
    start = min(status, probe, config_status, attempts, error) & ~3
    end = max(status + 1, probe + 1, config_status + 1, attempts + 4, error + 4)
    reported = False
    while True:
        info = read_memory(start, end - start, tmp / "status.bin")
        hal_status = info[status - start]
        expired = time.monotonic() >= deadline
        if hal_status == 0 or expired:
            n = int.from_bytes(read_memory(count, 4, tmp / "count.bin"), "little")
            if hal_status == 0:
                if n != samples:
                    raise RuntimeError(f"The STM32 finished the capture with {n} of {samples} samples.")
                raw = read_memory(log, log_bytes, tmp / "samples.bin")
                return np.frombuffer(raw, RECORD), interval
        problem = capture_problem(info[probe - start], info[config_status - start], hal_status,
                                  int.from_bytes(info[attempts - start:attempts - start + 4], "little"),
                                  int.from_bytes(info[error - start:error - start + 4], "little"))
        if expired:
            raise RuntimeError(f"The capture did not finish (capture status {hal_status}, {n}/{samples} samples). "
                               + (problem or "The STM32 is still capturing samples."))
        if problem and not reported:
            print(problem + " Waiting for the STM32 to retry.", flush=True)
            reported = True
        time.sleep(max(1, expected_end - time.monotonic()))


def save_capture(raw, interval, tmp):
    # Continue the sequence across both current and older capture filenames.
    previous = [int(m[1]) for path in PROJECT.iterdir()
                if (m := re.fullmatch(r"INA219_(?:capture|zajem)_(\d+)\.(?:csv|png)", path.name, re.I))]
    number = 1 + max(previous, default=0)
    name = f"INA219_capture_{number:03d}"
    t = np.arange(len(raw)) * interval
    shunt_mv = raw["shunt"] * 0.01
    bus_v = (raw["bus"] >> 3) * 0.004
    battery_v = bus_v + shunt_mv / 1000
    current = raw["shunt"] * 0.1  # mA with the 0.1 ohm shunt
    power = battery_v * current  # mW at VIN+

    csv = tmp / (name + ".csv")
    np.savetxt(csv, np.column_stack((t, shunt_mv, bus_v, battery_v, current, power)),
               delimiter=",", fmt=["%d", "%.2f", "%.3f", "%.5f", "%.1f", "%.6f"],
               header="time_ms,shunt_mV,bus_V,battery_V,current_mA,power_mW", comments="")
    full = f"Full capture averages {current.mean():.2f} mA and {power.mean():.2f} mW"
    # A burst starts after at least 100 ms below 15 mA. Ignore partial edge cycles.
    starts = []
    last_high_ms = 0
    for index in np.flatnonzero(current >= 15):
        if t[index] - last_high_ms >= 100:
            starts.append(index)
        last_high_ms = t[index]
    cycles = ""
    if len(starts) >= 2:
        whole = slice(starts[0], starts[-1])
        period = (t[starts[-1]] - t[starts[0]]) / (len(starts) - 1)
        cycles = (f"Complete cycles ({len(starts) - 1}) averaged {current[whole].mean():.2f} mA "
                  f"and {power[whole].mean():.2f} mW, with a mean period of {period:.1f} ms")

    peak = int(current.argmax())
    near = (t >= t[peak] - 24) & (t <= t[peak] + 68)
    fig, (overview, detail) = plt.subplots(2, 1, figsize=(9, 5.8), constrained_layout=True)
    fig.suptitle(f"INA219 capture {number:03d}\n{full}" + (f"\n{cycles}" if cycles else ""), fontsize=12)
    overview.plot(t / 1000, current, color="#086d79", lw=1)
    overview.set_xlabel("Time since capture start (s)")
    detail.plot(t[near] - t[peak], current[near], "o-", color="#086d79", markersize=4)
    detail.set_title(f"Peak sample of {current[peak]:.1f} mA at {t[peak] / 1000:.3f} s", loc="left")
    detail.set_xlabel("Time relative to the peak sample (ms)")
    for axis in (overview, detail):
        axis.set_ylabel("Current (mA)")
        axis.grid(axis="y", alpha=0.25)
    png = tmp / (name + ".png")
    fig.savefig(png, dpi=150)
    plt.close(fig)

    latest = tmp / "INA219_latest.png"
    shutil.copyfile(png, latest)
    saved_csv = csv.rename(PROJECT / csv.name)
    try:
        saved_png = png.rename(PROJECT / png.name)
    except OSError:
        saved_csv.rename(csv)
        raise
    print(f"Average supply voltage at VIN+ is {battery_v.mean():.3f} V.")
    print(full)
    print(cycles or "Too few burst starts were detected to calculate averages over complete cycles.")
    print(f"Saved {csv.name} and {png.name}.")
    try:
        latest.replace(PROJECT / latest.name)
    except PermissionError:
        print(f"{latest.name} is locked. The latest plot is {png.name}.")
    return saved_png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", dest="open_plot", action="store_true",
                        help="Open the numbered plot after the capture.")
    # Older commands also accepted --o as an abbreviation.
    parser.add_argument("--odpri", "--o", dest="open_plot", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    # All versions share this lock. Windows releases it when the capture process exits.
    lock_path = Path(tempfile.gettempdir()) / "ina219_stlink_meritev.lock"
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("Another INA219 capture is already running.") from None
        with tempfile.TemporaryDirectory(dir=PROJECT, prefix=".ina219-") as folder:
            tmp = Path(folder)
            raw, interval = capture(tmp)
            graph = save_capture(raw, interval, tmp)
    if args.open_plot:
        try:
            os.startfile(graph)
        except OSError:
            print(f"You can open {graph.name} in Project Explorer.")


def cli():
    try:
        main()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        sys.exit(f"Error: {error}")


if __name__ == "__main__":
    cli()
