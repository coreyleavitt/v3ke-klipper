# Create MCU code

The host MCU ELF is built by the Python pipeline (in the cross-toolchain
container), not a standalone script:

    $ python3 tools/build.py artifacts

The host-MCU build steps live in `tools/build/host.py`
(`klipper_host_mcu_steps`), which mirrors the menuconfig below.

## Settings

    [*] Enable extra low-level configuration options
        Micro-controller Architecture (Linux process)  --->
    ()  GPIO pins to set at micro-controller startup
