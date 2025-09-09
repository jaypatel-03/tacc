TaCC (ThermAl Cycle Control)
==========================================================================

Thermal cycle control script with software interlock, designed to work with the Oxford OPMD ATLAS Module Testing setup and hardware interlock

Authors: Umberto Mollinatti, Jay Patel \
Date updated: 02/09/2025

The deprecated files in /old are retained for posterity and reference. Also, as a warning to all those who come after. Poor version control but it was not expected that debugging would take as long as it did. 

## Usage

Run python tacc.py and the program will run a software interlock over the electrical testing setup while running a thermal cycle.

There are two standard cycles that need to be run: one is 10 cycles from -45 to 40, and the other is 1 cycle from -55 to 60. We often struggle to get the modules down to -55 due to lack of vacuum and poor thermal contact, so it is usually worth aiming for -50 and monitoring. 

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

## Notes

Each PID controller needs to operate on a different port so as to avoid any network protocol errors.  

The current dryer in the lab operates with a minimum of 4 bar pressure input from the main line. A measured output pressure of around 1 bar has been found to consistently work, with up to 2 bar working frequently. A common issue is that the humidity increases sometimes at low temperatures, which triggers a mini ramp up before trying to cool again. Monitor the humidity anyway for large thermal cycles (-50 60).

With the current chucks, we struggle to get the modules down to -55 as we cannot pull the vacuum and they therefore don't have good thermal contact with the chuck.  

## Potential future work
/things I didn't get a chance to do. 
- re-enable logging to Influx database once it has been set up (use new API token)
- instead of the try block to enter the contexts for some subset of instruments, use the ```getattr()``` and ```callable()``` methods to assess whether an object has the ```__enter__``` method and call it if so.
- automatically set the interlock (will need to adjust the interlock SCPI to allow SET commands) so no need to check each time
