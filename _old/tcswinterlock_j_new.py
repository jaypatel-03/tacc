#!/usr/bin/env python3

import subprocess, shutil, time, sys, signal, math, datetime
import numpy as np

from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import QTimer

from icicle.instrument import Instrument
from icicle.hmp4040 import HMP4040
from icicle.keithley2410 import Keithley2410
from icicle.itkdcsinterlock import ITkDCSInterlock
from icicle.pidcontroller import PIDController
from icicle import hubercc508

from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

from contextlib import ExitStack

import logging
import argparse
logger = logging.getLogger("tcswinterlock")

global DB_TOKEN

### TO BE MODIFIED BY USER ###
# Set the logging level to DEBUG for detailed output, or INFO for less verbose output
logging.basicConfig(level=logging.INFO)
DB_TOKEN = 'REDACTED' # Token for the InfluxDB database, redacted for security

processes = []
instruments = {}
please_kill = False

SHORT_DELAY = 0.3
LONG_DELAY = 0.5 + SHORT_DELAY

BASE_COMMAND_XTERM = ['xterm',
            '-T', 'Module QC tools',
            '-sl', '8192',
            '-geometry', '120x60+0+0',
            '-fs', '10',
            '-fa', 'Mono',
            '-bg', 'black',
            '-fg', 'gray',            
            #'-hold',
            '-e']

ENDPOINT = 'http://pplxatlasitk01.nat.physics.ox.ac.uk:8086'

def pelts_read(pelts) -> list:
    """Read the current state (on/off) of the peltiers"""
    peltier_on = []
    for i, pelt in enumerate(pelts):
        time.sleep(LONG_DELAY)
        with pelt: peltier_on.append(pelt.state)
    return peltier_on

def pelts_on_off(pelts : list, switch : bool):
    """ Turns the peltiers in the pelt list on or off, according to the value of switch.
    Args:
        - pelt: list of peltier objects
        - switch: True for turning on, False for turning off 
    """
    for i, pelt in enumerate(pelts):
        time.sleep(LONG_DELAY)
        with pelt: pelt.state = bool(switch)
        time.sleep(SHORT_DELAY)
        with pelt: s = pelt.state
        time.sleep(SHORT_DELAY)   
        logging.info(f"Pelt {i} : {s}")

def avg(instr : list) -> float:
    """Calculates the average of a list of instrument readings.
    If the instrument has a value attribute, it will read the value from each channel.
    Args:
        instr: list of instrument objects or values
    Returns:
        The average value of the readings.
    """
    if hasattr(instr, "value"):
        return np.mean([instr[i].value for i in MODULES])
    return np.mean(instr)

def calc_dewpoint(humidity : float, temp_85 : float):
    """Calculates dewpoint from humidity and temperature of the peltier back.
    
    Args:
        humidity: relative humidity in %
        temp_85: temperature of the peltier back in C
    Returns:
        Dewpoint in C, or -100.0 if humidity is too low or an error occurs.
    """
    if type(humidity) is not float: # if the object has been passed, read the value
        humidity = humidity.value if hasattr(humidity, 'value') else float(humidity)
    if type(temp_85) is not float: # if the object has been passed, read the value
        temp_85 = temp_85.value if hasattr(temp_85, 'value') else float(temp_85)
        
    if humidity <= 0.00001:
        return -100.0
    try:
        return 243.04 * (math.log(humidity / 100) + 17.625 * temp_85 / (243.04 + temp_85)) / (17.625 - math.log(humidity / 100) - 17.625 * temp_85 / (243.04 + temp_85))
    except Exception as e:
        logging.error(f'Exception: {e} with humi: {humidity} temp: {temp_85}')
        return -100.0

def log_information(fl, ntcs, lvs, pelt_psu, hvs, humi, chuck_temp, HEADER, write_api, temp_85):
    """Logs the current state of the instruments to a file and optionally to a database.
    Args:
        fl: file object to write the log to
        ntcs: list of NTC temperature sensors
        lvs: list of low voltage power supplies
        pelt_psu: list of Peltier power supplies
        hvs: list of high voltage power supplies
        humi: humidity sensor
        chuck_temp: list of chuck temperature sensors
        HEADER: list of header names for the log file
        write_api: InfluxDB write API object for logging to a database
        temp_85: temperature of the peltier back
    """
    outstring=[]
    outstring_time=datetime.datetime.utcfromtimestamp(time.time())
    outstring.append(outstring_time)
    
    avgs = [ntcs, chuck_temp, lvs, pelt_psu]
    
    # Read monitoring values into file or something
    logging.debug(f"NTCs: {avg(ntcs):.2f}°C")
    outstring.append(avg(ntcs))
    
    humidity = humi.value
    logging.debug(f"HUMI: {humidity}\%")
    outstring.append(humidity)
    
    logging.debug(f"TEMP: {avg(chuck_temp):.2f}°C")
    outstring.append(avg(chuck_temp))
    
    dewpoint = calc_dewpoint(humidity, temp_85)
    logging.debug(f"DEWP: {dewpoint:.2f}°C")
    outstring.append(dewpoint)

    logging.debug(f"LV setpoint: {avg(lvs.voltage):.2f}V, {avg(lvs.current):.2f}A")
    logging.debug(f"LV actual: {avg(lvs.measure_voltage):.2f}V, {avg(lvs.measure_current):.2f}A")
    outstring.append(avg(lvs.measure_voltage))
    outstring.append(avg(lvs.measure_current)) 
    logging.debug(f'LV status: {lvs.status!r}')

    logging.debug(f"PELT setpoint: {avg(pelt_psu.voltage):.2f}V, {avg(pelt_psu.current):.2f}A")
    logging.debug(f"PELT actual: {avg(pelt_psu.measure_voltage):.2f}V, {avg(pelt_psu.measure_current):.2f}A")
    outstring.append(avg(pelt_psu.measure_voltage))
    outstring.append(avg(pelt_psu.measure_current))
    #outstring.append(0.0) # ONLY WHILE THE REST IS COMMENTED OUT
    logging.debug(f"PELT status: {pelt_psu.status!r}")

    #logging.info("HV setpoint: {hvs[0].voltage}, {hvs[0].current}")
    #print('HV State', hvs[0].state)
    if int(hvs[0].state) == 1:
        print('HV actual', hvs[0].measure_voltage.value, hvs[0].measure_current.value)
        outstring.append(hvs[0].measure_voltage.value)
        outstring.append(hvs[0].measure_current.value)
    else:
        #print('HV off, will not read voltage and current')
        outstring.append(0.0)
        outstring.append(0.0)
    #print('HV Status', hvs[0].state)

    for i in outstring[:-1]:
        fl.write(str(i)+', ')
    fl.write(str(outstring[-1])+'\n')
    dictionary={
        "measurement":'4-module testbox software',
        "tags":{'location':'OPMD-cleanroom-main'},
        "fields": {k: v for k, v in zip(HEADER[1:], outstring[1:])}, #time, NTC, HUMI, TEMP, DEWPOINT, LV VOLT, LV CURR, PELT VOLT, PELT CURR, HV VOLT, HV CURR
        "time": outstring[0]
    }
    write_to_db(write_api, dictionary)

def interlock_test(base, pelts, ntcs, chuck_temp, lid, humi, temp_85, mini_ramp_up, temp):
    """Checks the interlock conditions and returns whether an interlock condition is met.
    Args:
        base: base temperature controller
        pelts: list of Peltier controllers
        ntcs: list of NTC temperature sensors
        chuck_temp: list of chuck temperature sensors
        lid: lid sensor
        humi: humidity sensor
        temp_85: temperature of the peltier back
        mini_ramp_up: boolean indicating if mini ramp up is active
    Returns:
        A tuple (interlock_condition, cause, mini_ramp_up) where:
        - interlock_condition: True if an interlock condition is met, False otherwise
        - cause: string indicating the cause of the interlock condition, or an empty string if no condition is met
        - mini_ramp_up: boolean indicating if mini ramp up was triggered
    """
    dewpoint = calc_dewpoint(humi.value, temp_85.value)
    
    if any([t > 70 for t in ntcs.value]):
        logging.critical('Interlock triggered due to NTC_temp > 70')
        pelts_on_off(pelts, False)
        return True, 'Temperature', mini_ramp_up, temp
    if any([t > 65 for t in ntcs.value]) and any(pelts_read(pelts)):
        pelts_on_off(pelts, switch=False)
        logging.critical('Peltier turned off due to NTC_temp > 65')
    if any([dewpoint > ch_t - 2 for ch_t in chuck_temp.value]):
        logging.critical('Interlock triggered due to chuck_temp > dewpoint + 2')
        return True, 'Dewpoint', mini_ramp_up, temp
    elif any([dewpoint > ch_t - 5 for ch_t in chuck_temp.value]):
        if mini_ramp_up == False:
        temp += 5
        mini_ramp_up = True
        logging.critical('Target temperature increased due to chuck_temp > dewpoint + 5')
        
    logging.info(f'THIS IS THE LID VALUE {lid.value}')
    if lid.value < 4:
        with base: base.state = False
        pelts_on_off(pelts, False)
        return True, 'Open Lid', mini_ramp_up, temp
    return False, '', mini_ramp_up, temp

def safe_shutdown(cause, pelt, base):
    logging.critical('[SAFE_SHUTDOWN] > please wait patiently...')
    with base: base.state = True
    with base: base.temperature = 20
    pelts_on_off(pelt, False)
    for hv in instruments['hvs']:
        if hv.state and hv.voltage > 0.001:
            hv.sweep(0, step_size = -5)      # Sweep to zero at rate 10V/s 
        hv.state = False
    for pelt in instruments['pelt_psu']:
        pelt.current = 0
        pelt.state = False
    logging.critical('... this takes a while')
    for lv in instruments['lvs']:
        lv.state = False
    show_warning(cause)

def kill_processes():
    logging.critical('[KILL_PROCESSES]')
    for proc in processes:
        if poll_process(proc):
            stop_process(proc)
        time.sleep(0.1)
        if poll_process(proc):
            kill_process(proc)

def signal_handler(sig, frame):
    # If we press ctr+c
    logging.critical('Ctrl+C received - exiting...')
    global please_kill
    if please_kill:
        kill_processes()
        safe_shutdown()
        sys.exit(1)
    else:
        please_kill = True

def ramp_up(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp : list, pelts : list, base, HEADER, write_api, lid, temp_85, mini_ramp_up, max_temp):
    """Ramps up the temperature of the Peltier elements and checks for interlock conditions.
    Args:"""
    logging.info('INSIDE RAMP UP')
    cause = ''
    pelts_on_off(pelts, False)
    while temp < max_temp: #Go up
                with base: base.temperature = temp + 10
                pelt_temperature_now = avg(chuck_temp)
                
                while pelt_temperature_now < temp - 0.1:
                    logging.info('Reaching desired temperature', temp)            
                    with base: print(base.temperature)
                    log_information(fl, ntcs, lvs, pelt_psu, hvs, humi, chuck_temp, HEADER, write_api, temp_85)
                    
                    interlock_condition, cause, mini_ramp_up = interlock_test(base, pelts, ntcs, chuck_temp, lid, humi, temp_85, mini_ramp_up)
                    
                    time.sleep(5)
                    pelt_temperature_now = avg(chuck_temp)

                    if interlock_condition:
                        break
                temp += 1          
                if interlock_condition:
                    break
    return interlock_condition, cause

def ramp_down(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp : list, chiller, pelts : list, base, HEADER, write_api, lid, temp_85, mini_ramp_up, min_temp):
    logging.info('INSIDE RAMP DOWN')

    cause = ''
    pelts_on_off(pelts, True)
    print(pelts_read(pelts))

    while temp > min_temp: #Go down
                with base: base.temperature = temp + 5
                time.sleep(LONG_DELAY)
                
                for i, pelt in enumerate(pelts):
                    logging.info(f"Ramp down: Setting pelt{i} temperature to {temp}")
                    with pelt: pelt.temperature = temp
                    time.sleep(LONG_DELAY + 0.5)
                
                pelt_temperature_now = avg(chuck_temp)
                
                while pelt_temperature_now > temp + 0.1:
                    logging.info('Reaching desired temperature', temp)            
                    with base: logging.info(f"Chiller: {base.temperature}")
                    logging.info(f"Current chuck temp: {avg(chuck_temp)}")
                    
                    log_information(fl, ntcs, lvs, pelt_psu, hvs, humi, chuck_temp, HEADER, write_api, temp_85)
                    
                    interlock_condition, cause, mini_ramp_up = interlock_test(base, pelts, ntcs, chuck_temp, lid, humi, temp_85, mini_ramp_up)
                    
                    if mini_ramp_up:
                        logging.info('INSIDE MINI RAMP UP TEMP', temp)
                        interlock_condition, cause = ramp_up(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp - 5, chuck_temp, chiller, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up, temp) #Last temp is the new target temperature (increased by 5 through the mini ramp up condition and set to level in the temp - 5)
                        mini_ramp_up = False

                        pelts_on_off(pelts, True)

                    time.sleep(LONG_DELAY)
                    pelt_temperature_now = avg(chuck_temp)
                    
                    if interlock_condition:
                        logging.critical("INTERLOCK CONDITION IN LOOP")
                        break 
                temp -= 1               
                if interlock_condition:
                    logging.critical("INTERLOCK CONDITION OUT OF LOOP")
                    break
    return interlock_condition, cause

def main():
    
    kwargs = parse_args()
    print(kwargs)

    
    signal.signal(signal.SIGINT, signal_handler)
    
    # interlock variables
    interlock = ITkDCSInterlock(resource='TCPIP::localhost::9898::SOCKET')
    ntcs = [interlock.channel("MeasureChannel", channel, measure_type='NTC:TEMP') for channel in (1, 2, 3, 4)]
    lid = interlock.channel("MeasureChannel", 1, measure_type='LID:VOLT')
    
    # Temperature of the module chuck
    chuck_temp = [interlock.channel("MeasureChannel", channel, measure_type='PT100:TEMP') for channel in (1, 2, 3, 4)] 
    
    #Peltier back
    temp_85 = interlock.channel("MeasureChannel", 1, measure_type='SHT85:TEMP') 
    humi = interlock.channel("MeasureChannel", 1, measure_type='SHT85:HUMI')
    
    lv_psu = HMP4040(resource='ASRL/dev/ttyHMP4040a::INSTR')
    lvs = [lv_psu.channel("PowerChannel", channel) for channel in (1, 2, 3, 4)]
    
    peltier_psu = HMP4040(resource='ASRL/dev/ttyHMP4040b::INSTR')
    pelt_psu = [peltier_psu.channel("PowerChannel", channel) for channel in (1, 2, 3, 4)]
    
    hv_psu = Keithley2410(resource='ASRL/dev/ttyUSB0::INSTR')
    #hv_psu = Keithley2410(resource='ASRL/dev/ttyHMP4040b::INSTR') #PLACEHOLDER FOR WHEN THE HV ISN'T ATTACHED, REMOVE!!!!
    hvs = [hv_psu.channel("PowerChannel", 1)]
    
    h = hubercc508.HuberCC508(resource = '/dev/ttyACM0')
    base = h.channel("TemperatureChannel", 1)
    chiller = h.channel("TemperatureChannel", 1)
    
    global MODULES
    MODULES = kwargs.get('modules', [1,2,3,4])
    global peltier_configs
    peltier_configs = [f"./pidcontroller_j{m}.toml" for m in MODULES]
    pelts = connect_pelts(port0=19896, peltier_configs=peltier_configs)
    
    if not pelts:
        logging.critical('[ERROR]: No Peltier controllers connected. Exiting...')
        sys.exit(1)
    
    for ch in [*ntcs, *lvs, *pelt_psu, *hvs, humi, *chuck_temp]:
        ch.__enter__()
    try:
        main_with_instruments(ntcs, lvs, pelt_psu, hvs, humi, chuck_temp, chiller, pelts, base, lid, temp_85, **kwargs)
    finally:
        instruments = {}
        for ch in [*ntcs, *lvs, *pelt_psu, *hvs, humi, *chuck_temp]:
            ch.__exit__(None, None, None)


def main_with_instruments(ntcs, lvs, pelt_psu, hvs, humi, chuck_temp, chiller, pelts, base, lid, temp_85, **kwargs):
    """Main function to run the thermal cycle with the given instruments.
    Args:
        ntcs: list of NTC temperature sensors
        lvs: list of low voltage power supplies
        pelt_psu: list of Peltier power supplies
        hvs: list of high voltage power supplies
        humi: humidity sensor
        chuck_temp: list of chuck temperature sensors
        chiller: chiller temperature controller
        pelts: list of Peltier controllers
        base: base temperature controller
        lid: lid sensor
        temp_85: temperature of the Peltier back
        kwargs: additional arguments such as n_cycles, max_temp, min_temp, modules
    """

    instruments['ntcs'] = ntcs
    instruments['lvs'] = lvs
    instruments['pelt_psu'] = pelt_psu
    instruments['hvs'] = hvs
    instruments['chuck_temp'] = chuck_temp
    instruments['chiller'] = chiller
    write_api = None
    
    write_api = connect_to_db(ENDPOINT)
    if write_api is None:
        logging.critical('[ERROR]: Cannot connect to database. Refusing to run.')
        sys.exit(1)
    
    
    # qc_tool = open_module_qc_tools()
    # processes.append(qc_tool)

    time.sleep(1)

    #Log output 
    logfile_time=time.strftime('%Y%m%d_%H%M%S')
    file_path = logfile_time + '_Interlock_log.csv'

    with open(file_path,'a') as fl: #FOLLOW EXAMPLE IN MAIN, NEED TO WRAP AROUND POLL PROCESS AND WRITE LINE BY LINE

        HEADER = ['time', 'NTC', 'HUMI', 'TEMP', 'DEWPOINT', 'LV VOLT', 'LV CURR', 'PELT VOLT', 'PELT CURR', 'HV VOLT', 'HV CURR']
        fl.write(', '.join(HEADER) + '\n')
        interlock_condition = False
        mini_ramp_up = False

        global please_kill
        #while poll_process(qc_tool) and not please_kill:
        cycles = 0
        max_cycles = kwargs.get('n_cycles', 10)
        max_temp = kwargs.get('max_temp', 30)
        min_temp = kwargs.get('min_temp', -45)

        
        if max_temp <= min_temp :
            logging.critical(f'[ERROR]: max_temp ({max_temp}) must be >= min_temp ({min_temp}). Exiting...')
            sys.exit(1)
        elif max_cycles <= 0:
            logging.critical(f'[ERROR]: max_cycles ({max_cycles}) must be > 0. Exiting...')
            sys.exit(1)
            
        logging.info(f'Starting thermal cycle with {max_cycles} cycles from {min_temp}°C to {max_temp}°C')
        logging.info(f'Connecting to Peltier controllers on ports {19896} to {19896 + len(pelts) - 1}')
        logging.info(f'Using the configuration files: {peltier_configs}')
        
        with base: base.speed = 2000
        with base: base.state = True
        print(peltier_on := pelts_read(pelts))
        while not please_kill and cycles<max_cycles:
            logging.info(f"\n*********Cycle {cycles}*********\n")
            temp = 20            
            interlock_condition, cause = ramp_down(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp, chiller, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up, min_temp)
            temp = min_temp
            interlock_condition, cause = ramp_up(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up, max_temp)
            temp = max_temp            
            interlock_condition, cause = ramp_down(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp, chiller, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up, 20)
            cycles += 1
            if interlock_condition:
                break
        
        
        if interlock_condition:
            logging.critical(f'Interlock condition met: {cause}. Shutting down safely...')
            safe_shutdown(cause, pelts, base)
            with base: base.state = True
            with base: base.temperature = 20
            for _ in range(5):
                log_information(fl, ntcs, lvs, pelt_psu, hvs, humi, chuck_temp, HEADER, write_api, temp_85)
            kill_processes()
        else:
            logging.critical('No interlock condition met, shutting down safely...')
            with base: base.state = True
            time.sleep(5)
            with base: base.temperature = 20
            time.sleep(5)
            pelts_on_off(pelts, False)
            sys.exit(0)

def connect_pelts(peltier_configs, port0=19896):
    """Connects to the Peltier controllers and returns a list of Peltier objects. 
    Args:
        port0: base port number for the Peltier controllers, each Peltier will be connected to a port incremented by 1.
    Returns:
        A list of Peltier objects connected to the specified ports.
    """
    pelts = []
    for i in range(len(MODULES)):
        # These config files should only contain 1 channel each.
        tricicle = open_tricicle(peltier_configs[i], port=port0+i) 
        time.sleep(2)
        processes.append(tricicle)
        p = PIDController(resource = f"TCPIP::localhost::{port0}::SOCKET")
        pelts.append(p.channel("TemperatureChannel", 1)) # must assign channel 1 (maybe?)
    return pelts
    
def show_warning(cause):
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Warning) 
    msg.setText(f"{cause} above expected level, ramping down voltages and terminating any scans")
    msg.setWindowTitle("Warning")
    msg.show()
    msg.exec_()

def open_tricicle(config_file, port = 19898):
    """Opens the tricicle PID controller GUI on a specific port to communicate with the peltiers using a TCP/IP protocol over a SCPI interface.
    Args:
        config_file: path to the configuration file for the PID controller. NB: there must be a config file for each peltier controller, with the channel set to 1. Each config file must only contain one Peltier channel.
        port: port number to connect to the PID controller
    Returns:
        A subprocess.Popen object which opens the PID GUI controller."""
    print(shutil.which("pidcontroller-ui"))
    return subprocess.Popen([shutil.which("pidcontroller-ui"), "-c", config_file, "-a", '-p', str(port)], stdin=None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

def kill_process(popen):
    popen.kill()

def stop_process(popen):
    popen.terminate()

def poll_process(popen):
    return popen.poll() is None

def get_process_output(popen):
    return popen.communicate()[0]

'''
def open_module_qc_tools(tool=sys.argv[1]):
    return subprocess.Popen([*BASE_COMMAND_XTERM, shutil.which(tool), *sys.argv[2:]], stdin=None, stdout=None, stderr=None)
'''
    
def connect_db(url,
            token=DB_TOKEN,
            org=''):
    client = InfluxDBClient(url=url, token='')
    write_api = client.write_api(write_options=SYNCHRONOUS)
    if client.ping():
        return write_api
    else:
        return False

def connect_to_db(endpoint):
    write_api = connect_db(endpoint)
    if write_api:
        logging.info('\nConnected to database, logging locally, and to influxDB if switched on!\n')
        return write_api
    else:
        logging.info('\nCould not connect to database, only logging locally!\n')
        return None

def write_to_db(write_api, dictionary):
    try:
        org=''
        write_api.write('mydb', org, dictionary)
        return True
    except Exception as e:
        logging.critical('[ERROR] Error writing to db: ' + str(e))
        return False



def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Thermal cycle control script")
    parser.add_argument('-n', '--n_cycles', type=int, default=10, help='Number of thermal cycles')
    parser.add_argument('-b', '--min_temp', type=float, default=-45, help='Minimum temperature (°C)')
    parser.add_argument('-t', '--max_temp', type=float, default=30, help='Maximum temperature (°C)')
    parser.add_argument('-m', '--modules', type=int, nargs='+', default=[1,2,3,4], help='List of module numbers to use')
    args = vars(parser.parse_args(argv))
    return args

if __name__ == '__main__':
    main()
