"""Zajem INA219 prek ST-LINK ter izvoz CSV in grafa za projekt INA219_test."""
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
        hint = " Če teče Debug, v CubeIDE klikni Terminate in preveri USB." if args[0] == SWD[0] else ""
        raise RuntimeError(result.stdout.strip() + hint)
    return result.stdout


def read_memory(address, size, path):
    path.unlink(missing_ok=True)
    run(*SWD, "-u", hex(address), str(size), str(path))
    data = path.read_bytes()
    if len(data) != size:
        raise RuntimeError("ST-LINK je vrnil nepopolne podatke.")
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
            raise RuntimeError(f"Program na plošči se razlikuje od gradnje Debug pri naslovu "
                               f"0x{flash + offset:08X}. V CubeIDE naloži program z Debug, "
                               "končaj sejo s Terminate in ponovi meritev.")


def capture_problem(probe, config, capture_status, tries, error):
    if tries == 0:
        return "STM32 še ni zaključil prvega preverjanja povezave I²C."
    if capture_status == 2:
        return ""  # Measurement is in progress.
    if probe != 0:
        stage = "Začetni prenos I²C na naslov 0x40 ni uspel."
    elif config != 0:
        if config == 1 and error == 0:
            return "Prebrani konfiguracijski register se razlikuje od nastavitve za zajem."
        stage = "Preverjanje konfiguracijskega registra INA219 ni uspelo."
    elif capture_status == 3 and error == 0:
        return "STM32 je prekoračil časovni okvir vzorca."
    else:
        stage = "Branje vzorca INA219 ni uspelo."
    if error & 0x20:
        return f"{stage} Pretekla je časovna omejitev prenosa I²C (poskus {tries})."
    if error & 0x01:
        return f"{stage} Napaka stanja vodila I²C (poskus {tries})."
    if error & 0x04:
        return f"{stage} Naprava ni potrdila prenosa (NACK, poskus {tries})."
    return f"{stage} Koda napake I²C 0x{error:08X}, poskus {tries}."


def capture(tmp):
    if not ELF.is_file():
        raise RuntimeError("V CubeIDE najprej zgradi in naloži projekt INA219_test v načinu Debug.")
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
        raise RuntimeError("V datoteki Debug/INA219_test.elf ni podatkov o zajemu INA219.")
    (flash, log, log_bytes, record_bytes, status, count, interval,
     probe, attempts, config_status, error) = map(int, layout[1].split())
    if record_bytes != RECORD.itemsize or log_bytes == 0 or interval == 0:
        raise RuntimeError("Oblika zapisa v programu STM32 se razlikuje od pričakovane.")

    verify_firmware(elf, flash, tmp)

    samples = log_bytes // record_bytes
    seconds = samples * interval / 1000
    print(f"Začenjam meritev: {samples} vzorcev, vsakih {interval} ms ({seconds:.2f} s).", flush=True)
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
                    raise RuntimeError(f"STM32 je zaključil zajem s {n}/{samples} vzorcev.")
                raw = read_memory(log, log_bytes, tmp / "samples.bin")
                return np.frombuffer(raw, RECORD), interval
        problem = capture_problem(info[probe - start], info[config_status - start], hal_status,
                                  int.from_bytes(info[attempts - start:attempts - start + 4], "little"),
                                  int.from_bytes(info[error - start:error - start + 4], "little"))
        if expired:
            raise RuntimeError(f"Zajem se ni zaključil (status zajema {hal_status}, {n}/{samples} vzorcev). "
                               + (problem or "STM32 še izvaja zajem."))
        if problem and not reported:
            print(problem + " Čakam na ponovni poskus STM32.", flush=True)
            reported = True
        time.sleep(max(1, expected_end - time.monotonic()))


def save_capture(raw, interval, tmp):
    previous = [int(m[1]) for path in PROJECT.iterdir()
                if (m := re.fullmatch(r"INA219_zajem_(\d+)\.(?:csv|png)", path.name, re.I))]
    name = f"INA219_zajem_{1 + max(previous, default=0):03d}"
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
    full = f"Celoten zajem: {current.mean():.2f} mA, {power.mean():.2f} mW"
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
        cycles = (f"Celi cikli ({len(starts) - 1}): {current[whole].mean():.2f} mA, "
                  f"{power[whole].mean():.2f} mW, perioda {period:.1f} ms")

    peak = int(current.argmax())
    near = (t >= t[peak] - 24) & (t <= t[peak] + 68)
    fig, (overview, detail) = plt.subplots(2, 1, figsize=(9, 5.8), constrained_layout=True)
    fig.suptitle(f"{name}\n{full}" + (f"\n{cycles}" if cycles else ""), fontsize=12)
    overview.plot(t / 1000, current, color="#086d79", lw=1)
    overview.set_xlabel("Čas od začetka zajema (s)")
    detail.plot(t[near] - t[peak], current[near], "o-", color="#086d79", markersize=4)
    detail.set_title(f"Najvišji vzorec {current[peak]:.1f} mA pri {t[peak] / 1000:.3f} s", loc="left")
    detail.set_xlabel("Čas glede na najvišji vzorec (ms)")
    for axis in (overview, detail):
        axis.set_ylabel("Tok (mA)")
        axis.grid(axis="y", alpha=0.25)
    png = tmp / (name + ".png")
    fig.savefig(png, dpi=150)
    plt.close(fig)

    latest = tmp / "INA219_zadnja.png"
    shutil.copyfile(png, latest)
    saved_csv = csv.rename(PROJECT / csv.name)
    try:
        saved_png = png.rename(PROJECT / png.name)
    except OSError:
        saved_csv.rename(csv)
        raise
    print(f"Povprečna napajalna napetost na VIN+ je {battery_v.mean():.3f} V.")
    print(full)
    print(cycles or "Premalo zaznanih začetkov sunkov za izračun povprečja čez cele cikle.")
    print(f"Shranjeno: {csv.name} in {png.name}")
    try:
        latest.replace(PROJECT / latest.name)
    except PermissionError:
        print(f"Datoteka {latest.name} je zaklenjena. Zadnji graf je {png.name}.")
    return saved_png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--odpri", action="store_true", help="Po meritvi odpri oštevilčeni graf.")
    args = parser.parse_args()
    # Windows releases the file lock even if the user stops the tool in CubeIDE.
    lock_path = Path(tempfile.gettempdir()) / "ina219_stlink_meritev.lock"
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("Druga meritev INA219 že poteka.") from None
        with tempfile.TemporaryDirectory(dir=PROJECT, prefix=".ina219-") as folder:
            tmp = Path(folder)
            raw, interval = capture(tmp)
            graph = save_capture(raw, interval, tmp)
    if args.odpri:
        try:
            os.startfile(graph)
        except OSError:
            print(f"Graf lahko odpreš v Project Explorerju: {graph.name}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        sys.exit(f"Napaka: {error}")
