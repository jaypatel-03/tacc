#!/usr/bin/env python3

import subprocess, shutil, time, sys, signal, math, os, datetime, threading
import numpy as np

from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import QTimer

from icicle.instrument import Instrument
from icicle.hmp4040 import HMP4040
from icicle.keithley2410 import Keithley2410
from icicle.itkdcsinterlock import ITkDCSInterlock
from icicle.pidcontroller import PIDController
from icicle import hubercc508

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from contextlib import ExitStack

import logging, click

processes = []
instruments = {}
please_kill = False
SHORT_DELAY = 0.3
LONG_DELAY = 0.5 + SHORT_DELAY

class Instruments:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

ENDPOINT = 'http://pplxatlasitk02.nat.physics.ox.ac.uk:8086'

def pelts_read(pelts) -> list:
    peltier_on = []
    for i, pelt in enumerate(pelts):
        time.sleep(LONG_DELAY)
        logging.debug(f"Reading state of pelt{i}")
        peltier_on.append(pelt.state)
    return peltier_on

def pelts_on_off(pelts : list, switch : bool):
    """ Turns the peltiers in the pelt list on or off, according to the value of switch.
    Args:
        - pelt: list of peltier objects
        - switch: True for turning on, False for turning off 
    """
    for i, pelt in enumerate(pelts):
        time.sleep(LONG_DELAY)
        for i in range(3):
            logging.debug(f"Setting pelt{i} state to {switch}")
            try:
                pelt.state = bool(switch)
                break  # If successful, break out of the retry loop
            except Exception as e:
                logging.error(f"Error setting pelt{i} state: {e}")
                continue
        time.sleep(SHORT_DELAY)
        # s = pelt.state
        # print(f"Pelt {i} : {s}")

def lvs_on_off(lvs : list, v : float, i : float, switch : bool):
    """ Turns the low voltage power supplies in the lvs list on or off, according to the value of switch.
    Args:
        - lvs: list of low voltage power supply objects
        - v: voltage to set
        - i: current to set
        - switch: True for turning on, False for turning off 
    """
    for i, lv in enumerate(lvs):
        time.sleep(LONG_DELAY)
        logging.debug(f"Setting lv{i} voltage to {v}V and current to {i}A")
        lv.voltage = v
        lv.current = i
        lv.state = bool(switch)
        time.sleep(SHORT_DELAY)
        s = lv.state
        print(f"LV {i} : {s}")

def calc_dewpoint(humidity : float, temp_85 : float):
    """Calculates dewpoint from humidity and temperature of the peltier back.
    
    Args:
        humidity: relative humidity in %
        temp_85: temperature of the peltier back in C
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
        print(f'Exception: {e} with humi: {humidity} temp: {temp_85}')
        return -100.0

def avg(instr : list) -> float:
    """Calculates the average of a list of instrument readings.
    If the instrument has a value attribute, it will read the value from each channel.
    Args:
        instr: list of instrument objects or values
    Returns:
        The average value of the readings.
    """
    if not isinstance(instr[0], float):
        return np.mean([instr[i].value for i in range(len(instr))])
    return np.mean(instr)

def read_instrument_values(instr : list) -> list:
    """Reads the values from a list of instrument objects.
    If the instrument has a value attribute, it will read the value from each channel.
    Args:
        instr: list of instrument objects or values
    Returns:
        A list of values read from the instruments.
    """
    if not isinstance(instr[0], float):
        return [instr[i].value for i in range(len(instr))]
    return instr

def log_information(fl, instruments, HEADER, write_api):
    """Logs the current state of the instruments to a file and optionally to a instruments.database.
    Args:
        fl: file object to write the log to
        ntcs: list of NTC temperature sensors
        lvs: list of low voltage power supplies
        pelt_psu: list of Peltier power supplies
        hvs: list of high voltage power supplies
        humi: humidity sensor
        chuck_temp: list of chuck temperature sensors
        HEADER: list of header names for the log file
        write_api: InfluxDB write API object for logging to a instruments.database
        temp_85: temperature of the peltier back
    """
    outstring=[]
    outstring_time=datetime.datetime.utcfromtimestamp(time.time())
    outstring.append(outstring_time)
    
    # Read monitoring values into file or something
    logging.info(f"NTCs: {avg(instruments.ntcs):.2f}°C")
    outstring.append(avg(instruments.ntcs))
    
    humidity = instruments.humi.value
    logging.info(f"HUMI: {humidity}\%")
    outstring.append(humidity)
    
    logging.info(f"TEMP: {avg(instruments.chuck_temp):.2f}°C")
    outstring.append(avg(instruments.chuck_temp))
    
    dewpoint = calc_dewpoint(humidity, instruments.temp_85)
    logging.info(f"DEWP: {dewpoint:.2f}°C")
    outstring.append(dewpoint)
    
    avg_voltage = lambda x: np.mean([x[i].voltage for i in range(len(x))])
    avg_current = lambda x: np.mean([x[i].current for i in range(len(x))])
    avg_measure_voltage = lambda x: np.mean([x[i].measure_voltage.value for i in range(len(x))])
    avg_measure_current = lambda x: np.mean([x[i].measure_current.value for i in range(len(x))])
    
    logging.info(f"LV setpoint: {avg_voltage(instruments.lvs):.2f}V, {avg_current(instruments.lvs):.2f}A")
    logging.info(f"LV actual: {avg_measure_voltage(instruments.lvs):.2f}V, {avg_measure_current(instruments.lvs):.2f}A")
    
    outstring.append(avg_voltage(instruments.lvs))
    outstring.append(avg_current(instruments.lvs))
    
    logging.info(f"PELT setpoint: {avg_voltage(instruments.pelt_psu):.2f}V, {avg_current(instruments.pelt_psu):.2f}A")
    logging.info(f"PELT actual: {avg_measure_voltage(instruments.pelt_psu):.2f}V, {avg_measure_current(instruments.pelt_psu):.2f}A")
    
    outstring.append(avg_measure_voltage(instruments.pelt_psu))
    outstring.append(avg_measure_current(instruments.pelt_psu))

    #outstring.append(0.0) # ONLY WHILE THE REST IS COMMENTED OUT
    logging.info(f"PELT status: {[instruments.pelt_psu[i].status for i in range(len(instruments.pelt_psu))]!r}")

    #logging.info("HV setpoint: {hvs[0].voltage}, {hvs[0].current}")
    #print('HV State', hvs[0].state)
    ''' 
    if int(hvs[0].state) == 1:
        print('HV actual', hvs[0].measure_voltage.value, hvs[0].measure_current.value)
        outstring.append(hvs[0].measure_voltage.value)
        outstring.append(hvs[0].measure_current.value)
    else:
        #print('HV off, will not read voltage and current')
        outstring.append(0.0)
        outstring.append(0.0)
    #print('HV Status', hvs[0].state)
    '''
    outstring.append(0.0) # JAY: to delete
    outstring.append(0.0) # JAY: to delete
    
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

def interlock_test(instruments : Instruments, mini_ramp_up, temp):
    """Checks the interlock conditions and returns whether an interlock condition is met.
    Args:
        instruments: class object containing list of instrument channels
        mini_ramp_up: boolean indicating if mini ramp up is active
        temp: current temperature
    Returns:
        A tuple (interlock_condition, cause, mini_ramp_up) where:
        - interlock_condition: True if an interlock condition is met, False otherwise
        - cause: string indicating the cause of the interlock condition, or an empty string if no condition is met
        - mini_ramp_up: boolean indicating if mini ramp up was triggered
    """
    dewpoint = calc_dewpoint(instruments.humi.value, instruments.temp_85.value)
    ntc_vals = read_instrument_values(instruments.ntcs)
    chuck_temp_vals = read_instrument_values(instruments.chuck_temp)
    relay_vals = read_instrument_values(instruments.ilock_relay)
    #print(f"{relay_vals=}")
    if any([t > 70 for t in ntc_vals]):
        logging.critical('Interlock triggered due to NTC temp > 70')
        pelts_on_off(instruments.pelts, False)
        return True, 'Temperature', mini_ramp_up, temp
    if any([t > 65 for t in ntc_vals]) and any(pelts_read(instruments.pelts)):
        pelts_on_off(instruments.pelts, switch=False)
        logging.critical('Peltier turned off due to NTC temp > 65')
    if any([dewpoint > ch_t - 2 for ch_t in chuck_temp_vals]):
        logging.critical('Interlock triggered due to chuck temp > dewpoint + 2')
        return True, 'Dewpoint', mini_ramp_up, temp
    elif any([dewpoint > ch_t - 5 for ch_t in chuck_temp_vals]):
        print(f"{dewpoint=}")
        print(f"{chuck_temp_vals=}")
        if mini_ramp_up == False:
            temp += 5
            mini_ramp_up = True
            logging.critical('Target temperature increased due to chuck temp > dewpoint + 5')
        
    if instruments.lid.value < 4:
        pelts_on_off(instruments.pelts, False)
        time.sleep(2)
        logging.critical('Interlock triggered due to lid voltage < 4V')
        return True, 'Open Lid', mini_ramp_up, temp
    
    if "TRIP" in relay_vals[0]:
        time.sleep(2)
        pelts_on_off(instruments.pelts, False)
        time.sleep(2)
        logging.critical('Hardware interlock triggered')
        return True, 'HW Interlock', mini_ramp_up, temp
    
    return False, '', mini_ramp_up, temp

def safe_shutdown(cause, instruments = None):
    print('[SAFE_SHUTDOWN] > please wait patiently...')
    if instruments:
        with instruments.base: instruments.base.temperature = 20
        for hv in instruments.hvs:
            if hv.state and hv.voltage > 0.001:
                hv.sweep(0, step_size=-5)      # Sweep to zero at rate 10V/s 
            hv.state = False
        for pelt in instruments.pelt_psu:
            pelt.current = 0
            pelt.state = False
        print('... this takes a while')
        for lv in instruments.lvs:
            lv.state = False
    show_warning(cause)

def kill_processes():
    print('[KILL_PROCESSES]')
    for proc in processes:
        if poll_process(proc):
            stop_process(proc)
        time.sleep(0.1)
        if poll_process(proc):
            kill_process(proc)

def signal_handler(sig, frame):
    # If we press ctr+c
    print('Ctrl+C received - exiting...')
    global please_kill
    if please_kill:
        kill_processes()
        safe_shutdown('Keyboard interrupt',  )
        sys.exit(1)
    else:
        please_kill = True

def ramp_up(instruments, fl, interlock_condition, HEADER, write_api, mini_ramp_up, temp, max_temp):
    logging.warning('INSIDE RAMP UP')
    cause = ''
    # pelts_on_off(pelts, False)

    if temp > max_temp:
        return interlock_condition, cause
    
    if not mini_ramp_up:
        with instruments.base: instruments.base.temperature = max_temp + 15 if max_temp < 55 else 70
        with instruments.base: logging.info(f"Chiller: {instruments.base.temperature}")
        
    while temp < max_temp: #Go up

        pelt_temperature_now = avg(instruments.ntcs)
        
        # if (max_temp - 12 < temp) or (temp < max_temp - 8):
            # lvs_on_off(lvs, 1.0, 0.5, True) #Set the low voltage power supplies to 1.0V and 0.5A
            
        while pelt_temperature_now < temp - 0.1:
            logging.info('Reaching desired temperature', temp)            
            
            # log_information(fl, ntcs, lvs, pelt_psu, hvs, humi, chuck_temp, HEADER, write_api, temp_85)
            
            interlock_condition, cause, mini_ramp_up, temp = interlock_test(instruments, mini_ramp_up, temp)
            
            pelt_temperature_now = avg(instruments.ntcs)
            logging.info(f"Current NTC temp: {pelt_temperature_now}C")
            if interlock_condition:
                break
        temp += 1          
        if interlock_condition:
            break
            
        # lvs_on_off(lvs, 0.0, 0.0, False) #Turn off the low voltage power supplies    
    logging.warning("RAMP UP FINISHED")            
    return interlock_condition, cause

def ramp_down(instruments : Instruments, fl, interlock_condition, HEADER, write_api, temp, mini_ramp_up, min_temp):
    logging.warning('INSIDE RAMP DOWN')
    
    cause = ''
  
    with instruments.base: instruments.base.temperature = min_temp
    with instruments.base: logging.info(f"Chiller: {instruments.base.temperature}")
    pelt_temperature_now = avg(instruments.ntcs)
    
    if min_temp < -40:
        logging.warning("45 minute pause to allow chiller to begin cooling")
        time.sleep(45*60)
    elif pelt_temperature_now - min_temp > 10:
        logging.warning("Seven minute pause to allow chiller to begin cooling")
        time.sleep(7*60)
    
    pelts_on_off(instruments.pelts, True)
        
    while temp > min_temp: #Go down
            
            for i, pelt in enumerate(instruments.pelts):
                logging.info(f"Ramp down: Setting pelt{i} temperature to {temp}")
                time.sleep(LONG_DELAY)
                pelt.temperature = temp
            
            pelt_temperature_now = avg(instruments.ntcs)
            
            while pelt_temperature_now > temp + 0.5:
                logging.info('Reaching desired temperature', temp)            
                logging.info(f"Current NTC temp: {pelt_temperature_now}C")
                
                # log_information(fl, ntcs, lvs, pelt_psu, hvs, humi, chuck_temp, HEADER, write_api, temp_85)
                
                interlock_condition, cause, mini_ramp_up, temp = interlock_test(instruments, mini_ramp_up, temp)
                
                if mini_ramp_up:
                    logging.warning('INSIDE MINI RAMP UP TEMP', temp)
                    pelts_on_off(instruments.pelts,False)
                    
                    interlock_condition, cause = ramp_up(instruments, fl, interlock_condition, HEADER,write_api, mini_ramp_up, temp - 5,  temp) #Last temp is the new target temperature (increased by 5 through the mini ramp up condition and set to level in the temp - 5)
                    mini_ramp_up = False

                    pelts_on_off(instruments.pelts, True)

                pelt_temperature_now = avg(instruments.ntcs)
                if interlock_condition:
                    logging.critical("INTERLOCK CONDITION IN LOOP")
                    break 
                
            if (temp - min_temp) > 5:
                temp -= 5
            else:
                temp -= 1
                        
            if interlock_condition:
                logging.critical("INTERLOCK CONDITION OUT OF LOOP")
                break
    logging.warning("RAMP DOWN FINISHED")
    
    pelts_on_off(instruments.pelts, False)
    
    return interlock_condition, cause

def setup_logging(verbosity):
    logger = logging.getLogger(__name__)
    levels = [logging.WARNING, logging.INFO, logging.DEBUG]
    level = levels[min(verbosity, len(levels) - 1)]
    logging.basicConfig(level=level,
                        format='[%(asctime)s][%(name)s][%(levelname)s] - %(message)s')
    logging.getLogger().setLevel(level)
    logging.info(f"Verbosity level set to {logging.getLogger().level}")
    
@click.command()
@click.argument(
    'modules', 
    type=int, 
    nargs=-1,       # Accepts 0 or more integers
)
@click.option(
    '-n', 
    '--n-cycles',
    metavar="<n cycles>",
    type=int, 
    default=10, 
    show_default=True,
    help='Number of temperature cycles to run'
)
@click.option(
    '-t',
    '--temp-range',
    metavar = '<min max>', 
    type=float, 
    nargs=2, 
    default=(-40, 45), 
    show_default=True,
    help='Temperature range'
)
@click.option(
    '-v', '--verbosity',
    count=True, 
    default=0, 
    show_default=True,
    help='Increase output verbosity: -v, -vv, -vvv'
)
def cli(n_cycles, temp_range, modules, verbosity):
    """
    TaCC (ThermAl Cycle Control)
    
    Examples: \n
    python tacc 2 3 4 -n 1 -t -55 60 \n # Does 1 thermal cycle between -55 and 60 for only modules 2 3 4 \n 
    python tacc \n # Does 10 thermal cycles of all modules between -40 and 45 \n
    python tacc 1 2 3 4 -n 1 -t -55 60 && python tacc 1 2 3 4 \n # Does 1 big + 10 small
    """
    min_temp, max_temp = temp_range
    click.echo(f"n_cycles: {n_cycles}")
    click.echo(f"min_temp: {min_temp}")
    click.echo(f"max_temp: {max_temp}")
    click.echo(f"modules: {modules}")
    click.echo(f"verbosity: {verbosity}")
    
    
    if not modules:
        modules = (1,2,3,4)
    elif not any([a in [1,2,3,4] for a in modules]):
        raise click.BadParameter("Invalid module numbers, should be subset of {1,2,3,4}")
    inst_modules = [m for m in modules]
        
    setup_logging(verbosity)
    
    signal.signal(signal.SIGINT, signal_handler)
    
    # interlock variables
    interlock = ITkDCSInterlock(resource='TCPIP::localhost::9898::SOCKET')
    ntcs = [interlock.channel("MeasureChannel", channel, measure_type='NTC:TEMP') for channel in inst_modules]
    ilock_relay = [interlock.channel("MeasureChannel", channel, measure_type='RELAY:STATUS') for channel in inst_modules]
    chuck_temp = [interlock.channel("MeasureChannel", channel, measure_type='PT100:TEMP') for channel in inst_modules] #Temperature of the module chuck
    humi = interlock.channel("MeasureChannel", 1, measure_type='SHT85:HUMI')
    temp_85 = interlock.channel("MeasureChannel", 1, measure_type='SHT85:TEMP') #Temperature of the peltier back
    lid = interlock.channel("MeasureChannel", 1, measure_type='LID:VOLT')
    lv_psu = HMP4040(resource='ASRL/dev/ttyHMP4040a::INSTR')
    lvs = [lv_psu.channel("PowerChannel", channel) for channel in inst_modules]
    peltier_psu = HMP4040(resource='ASRL/dev/ttyHMP4040b::INSTR')
    pelt_psu = [peltier_psu.channel("PowerChannel", channel) for channel in inst_modules]
    hv_psu = Keithley2410(resource='ASRL/dev/ttyUSB0::INSTR')
    #hv_psu = Keithley2410(resource='ASRL/dev/ttyHMP4040b::INSTR') #PLACEHOLDER FOR WHEN THE HV ISN'T ATTACHED, REMOVE!!!!
    hvs = [hv_psu.channel("PowerChannel", 1)]
    h = hubercc508.HuberCC508(resource = '/dev/ttyACM0')
    base = h.channel("TemperatureChannel", 1)
    chiller = h.channel("TemperatureChannel", 1)

    pelts = []
    port0 = 19895
    
    for i in inst_modules:
        # These config files should only contain 1 channel each.
        
        tricicle = open_tricicle(f"./pidcontroller_j{i}.toml", port=port0+i) 
        time.sleep(2)
        processes.append(tricicle)
        p = PIDController(resource = f"TCPIP::localhost::{port0+i}::SOCKET")
        pelts.append(p.channel("TemperatureChannel", 1)) # must assign channel 1 (maybe?)

    global MODULES
    MODULES = [x-1 for x in inst_modules]
    
    instruments = Instruments(
        ntcs=ntcs,
        chuck_temp=chuck_temp,
        humi=humi,
        temp_85=temp_85,
        lid=lid,
        lvs=lvs,
        pelt_psu=pelt_psu,
        hvs=hvs,
        base=base,
        chiller=chiller,
        pelts=pelts,
        ilock_relay=ilock_relay
    )
    
    for ch in [*ntcs, *lvs, *pelt_psu ,*hvs, humi, *chuck_temp, *ilock_relay]:
        ch.__enter__()
    try:
        
        main_with_instruments(instruments, n_cycles, min_temp, max_temp)
    finally:
        instruments = {}
        for ch in [*ntcs, *lvs, *pelt_psu, *hvs, humi, *chuck_temp, *ilock_relay]:
            ch.__exit__(None, None, None)
        kill_processes()

def main_with_instruments(instruments : Instruments, n_cycles, min_temp, max_temp):

    write_api = None
    
    write_api = connect_to_db(ENDPOINT)
    if write_api is None:
        print('[ERROR]: Cannot connect to database. Refusing to run.')
        sys.exit(1)


    #Log output 
    logfile_time=time.strftime('%Y%m%d_%H%M%S')
    file_path = logfile_time + '_Interlock_log.csv'
    
    with open(file_path,'a') as fl: #FOLLOW EXAMPLE IN MAIN, NEED TO WRAP AROUND POLL PROCESS AND WRITE LINE BY LINE
        HEADER = ['time', 'NTC', 'HUMI', 'TEMP', 'DEWPOINT', 'LV VOLT', 'LV CURR', 'PELT VOLT', 'PELT CURR', 'HV VOLT', 'HV CURR']
        fl.write(', '.join(HEADER) + '\n')
        interlock_condition = False
        mini_ramp_up = False

        global please_kill
        cycles = 0

        with instruments.base: instruments.base.speed = 2000
        with instruments.base: instruments.base.state = True
        
        # print(f"Peltiers initial states: {pelts_read(pelts)!r}")
        temp = 20
        logging.warning(f"Doing {n_cycles} cycles from {min_temp}°C to {max_temp}°C with modules {MODULES}")  
        while not please_kill and cycles < n_cycles:
            cycles += 1
            logging.warning(f"\n*********Cycle {cycles}*********\n")
            with ExitStack() as stack:
                stack = [stack.enter_context(pelt) for pelt in instruments.pelts]
                
                interlock_condition, cause = ramp_down(instruments, fl, interlock_condition, HEADER, write_api, temp, mini_ramp_up, min_temp)
                
                temp = min_temp
                interlock_condition, cause = ramp_up(instruments, fl, interlock_condition, HEADER, write_api, mini_ramp_up, temp, max_temp)
                
                temp = max_temp            
                if interlock_condition:
                    break
                
                if cycles == n_cycles:
                    interlock_condition, cause = ramp_down(instruments, fl, interlock_condition, HEADER, write_api, temp, mini_ramp_up, 20)
                    with instruments.base: instruments.base.state = False
                    # lvs_on_off(lv, 0,0, False)
        
        
        if interlock_condition:
            with instruments.base: instruments.base.state = True
            with instruments.base: instruments.base.temperature = 20
            # for i in range(3):
                # instruments.lvs[i].state = False
                # hvs[i].state = False

def show_warning(cause):
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Warning) 
    msg.setText(f"{cause} above expected level, ramping down voltages and terminating any scans")
    msg.setWindowTitle("Warning")
    msg.show()
    msg.exec_()

def open_tricicle(config_file, port = 19898):
    print(shutil.which("pidcontroller-ui"))
    return subprocess.Popen([shutil.which("pidcontroller-ui"), "-c", config_file, "-a", '-p', str(port)], stdin=None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

def kill_process(popen):
    popen.kill()

def stop_process(popen):
    popen.terminate()

def poll_process(popen):
    return popen.poll() is None

def connect_db(url,
               token='REDACTED',
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
        print('\nConnected to instruments.database, logging locally, and to influxDB if switched on!\n')
        return write_api
    else:
        print('\nCould not connect to instruments.database, only logging locally!\n')
        return None

def write_to_db(write_api, dictionary):
    try:
        org=''
        write_api.write('mydb', org, dictionary)
        return True
    except Exception as e:
        print('[ERROR] Error writing to db: ' + str(e))
        return False
     
if __name__ == '__main__':
    cli()
