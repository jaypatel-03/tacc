TaCC (ThermAl Cycle Control)
==========================================================================

Thermal cycle control script with software interlock, designed to work with the Oxford OPMD ATLAS Module Testing setup and hardware interlock

Authors: Umberto Mollinatti, Jay Patel
Date updated: 02/09/2025

The deprecated files are retained for posterity and reference.
## Usage

Run python tacc.py and the program will run a software interlock over the electrical testing setup while running a thermal cycle.

There are two standard cycles that need to be run: one is 10 cycles from -45 to 40, and the other is 1 cycle from -55 to 60

TaCC (ThermAl Cycle Control)

Examples:

```python tacc 2 3 4 -n 1 -t -55 60```\
::Does 1 thermal cycle between -55 and 60 for only modules 2 3 4

```python tacc```\
::Does 10 thermal cycles of all modules between -40 and 45

```python tacc 1 2 3 4 -n 1 -t -55 60 && python tacc 1 2 3 4```\
::Does 1 big + 10 small

## Requirements:
- *nix OS
- Python 3.x
- PyQt5
- [icicle](https://gitlab.cern.ch/icicle/icicle.git) in ```pidcontroller-bugfix``` branch 
- [tricicle](https://gitlab.cern.ch/icicle/tricicle.git) with ```pip install -e  ".[qt5]"```
- Oxford hardware interlock system [itk-dcs-interlock](https://gitlab.cern.ch/sfkoch/itk-dcs-interlock.git) with relevant instrument naming rules and CRIO drivers



## Example Peltier config: 
```
[[pidcontroller]]
power_instrument = "HMP4040"
power_resource = "ASRL/dev/ttyHMP4040b"
power_channel = 1
power_type = "current"
measure_instrument = "ITkDCSInterlock"
measure_resource = "TCPIP::localhost::9898::SOCKET"
measure_channel = 1
measure_type = "PT100:TEMP"
simulate = false
Kp = -1.5
Ki = -0.085
Kd = -0.01
setpoint = 15                # degrees C
starting_output = 0.0        # amps
sample_time = 0.5            # seconds
output_limits = [ 0.0, 4.0 ]  # amps
proportional_on_measurement = false
differential_on_measurement = true
```
