"""Find a DUPLO train and read its information without activating its motor."""

import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

SERVICE = "00001623-1212-efde-1623-785feabcd123"
CHARACTERISTIC = "00001624-1212-efde-1623-785feabcd123"
LEGO_COMPANY_ID = 0x0397
# Older trains advertise 0x20; the 10427 advertises 0x21.
DUPLO_HUB_TYPES = {0x20, 0x21}


async def find_trains(timeout):
    print(f"Searching for DUPLO trains for {timeout:g} seconds...", flush=True)
    found = await BleakScanner.discover(
        timeout=timeout, return_adv=True, service_uuids=[SERVICE]
    )
    trains = []
    for device, advertisement in found.values():
        manufacturer = advertisement.manufacturer_data.get(LEGO_COMPANY_ID, b"")
        if len(manufacturer) >= 2 and manufacturer[1] in DUPLO_HUB_TYPES:
            trains.append(device)
            print(
                f"Found: {advertisement.local_name or device.name or 'DUPLO Train'}\n"
                f"  Bluetooth identifier: {device.address}\n"
                f"  Signal: {advertisement.rssi} dBm",
                flush=True,
            )
    if not trains:
        print("No DUPLO train found. Turn it on nearby and close the LEGO phone app.")
    return trains


async def read_info(device):
    battery_received = asyncio.Event()

    def notification(_sender, data):
        if len(data) < 5:
            return
        if data[2] == 0x01 and data[4] == 0x06:
            property_id, payload = data[3], data[5:]
            if property_id == 0x01:
                print(f"Name: {payload.decode('utf-8', errors='replace')}", flush=True)
            elif property_id == 0x06 and payload:
                print(f"Battery: {payload[0]}%", flush=True)
                battery_received.set()
            elif property_id == 0x08:
                print(f"Manufacturer: {payload.decode('utf-8', errors='replace')}", flush=True)
        elif data[2] == 0x05:
            print(f"Train reported a protocol error: {data.hex(' ')}", flush=True)

    print("Connecting to the train...", flush=True)
    async with BleakClient(device, timeout=20) as client:
        characteristic = client.services.get_characteristic(CHARACTERISTIC)
        if characteristic is None:
            raise RuntimeError("The train's LEGO Bluetooth characteristic was not found.")
        print("Connected successfully. Reading train information...", flush=True)
        await client.start_notify(characteristic, notification)
        response = "write" in characteristic.properties
        # Hub Properties / Request Update: these only request information.
        for property_id in (0x01, 0x08, 0x06):
            await client.write_gatt_char(
                characteristic, bytes([5, 0, 0x01, property_id, 0x05]), response=response
            )
            await asyncio.sleep(0.1)
        try:
            await asyncio.wait_for(battery_received.wait(), timeout=5)
        except TimeoutError:
            print("Bluetooth connected, but the train did not return its battery level.")
        await client.stop_notify(characteristic)
    print("Disconnected. No motor commands were sent.")


async def main(args):
    trains = await find_trains(args.timeout)
    if not trains:
        return 1
    if args.command == "scan":
        return 0
    if args.address:
        trains = [device for device in trains if device.address.lower() == args.address.lower()]
        if not trains:
            raise RuntimeError("The requested Bluetooth identifier was not found.")
    if len(trains) != 1:
        raise RuntimeError("Several trains found. Choose one using --address IDENTIFIER.")
    await read_info(trains[0])
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scan", "info"), nargs="?", default="info")
    parser.add_argument("--address", help="Bluetooth identifier printed by scan")
    parser.add_argument("--timeout", type=float, default=10, help="Scan duration in seconds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        sys.exit(asyncio.run(main(args)))
    except KeyboardInterrupt:
        sys.exit(130)
    except (BleakError, RuntimeError, TimeoutError) as error:
        print(f"Bluetooth check failed: {error}", file=sys.stderr)
        sys.exit(1)
