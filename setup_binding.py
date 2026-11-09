"""Save the owner's device address locally without scanning or connecting."""
from getpass import getpass
import json
import re

from ble_readonly import BINDING_FILE
from local_runtime import secure_directory, write_private


def save_binding(address, path=BINDING_FILE):
    if not isinstance(address, str):
        raise ValueError('Invalid device address')
    address = address.strip().upper()
    if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', address):
        raise ValueError('Expected six colon-separated hexadecimal pairs')
    if path.exists():
        raise ValueError('Binding already exists. Review the existing local binding first.')
    secure_directory(path.parent)
    write_private(path, json.dumps({'address': address}))


def main():
    print('Enter the BLE address of your own FUN-001. Input is hidden.')
    try:
        save_binding(getpass('Device address: '))
    except (OSError, ValueError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else 'Cannot save private binding')
        return 1
    print('Private device binding saved. Continue with the owner control console.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
