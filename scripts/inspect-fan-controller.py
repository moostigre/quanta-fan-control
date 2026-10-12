#!/usr/bin/python3
"""Read verified S210 W83793 registers; never write register values."""
import json
import subprocess


def read_register(register):
    # Master Write-Read sends only the register pointer, followed by one read.
    result = subprocess.run(
        ['/usr/bin/ipmitool', 'raw', '0x06', '0x52', '0x01', '0x5e',
         '0x01', hex(register)], check=True, capture_output=True, text=True, timeout=8)
    values = result.stdout.split()
    if len(values) != 1:
        raise RuntimeError('Unexpected register response')
    return int(values[0], 16)


def main():
    identity = subprocess.run(['/usr/bin/ipmitool', 'mc', 'info'], check=True,
                              capture_output=True, text=True, timeout=8).stdout
    if not all(value in identity for value in ['Firmware Revision         : 7.54',
                                               'Manufacturer ID           : 7244',
                                               'Product ID                : 12882']):
        raise RuntimeError('Inspection restricted to verified S210 firmware 7.54')
    bank = read_register(0)
    if bank & 7:
        raise RuntimeError('Controller is not in bank 0; retry without changing banks')
    result = {'bank_select': bank, 'output_style_b0': read_register(0xb0),
              'output_style_b1': read_register(0xb1), 'fan_duty': {}}
    for selector, register in enumerate([0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xb5], 1):
        result['fan_duty'][str(selector)] = {'register': hex(register),
                                            'raw': read_register(register)}
    if read_register(0) != bank:
        raise RuntimeError('Bank changed during inspection; discard and retry')
    result['note'] = 'Sequential snapshot; PWM signal and fan power are not measured.'
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
