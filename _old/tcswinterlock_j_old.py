#!/usr/bin/env python3

from re import L
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

import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO) # JAY

processes = []
instruments = {}
please_kill = False

MODULES = [0,1,2,3]
SHORT_DELAY = 0.2
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
    peltier_on = []
    for i, pelt in enumerate(pelts):
        time.sleep(LONG_DELAY)
        logging.debug(f"Reading state of pelt{i}")
        with pelt: peltier_on.append(pelt.state)
    return peltier_on

def pelts_on_off(pelts : list, peltier_on : list, switch : bool):
    """ Turns the peltiers in the pelt list on or off, according to the value of switch.
    Args:
        - pelt: list of peltier objects
        - peltier_on: output from pelts_read function with current state NOTE: DISABLED.
        - switch: True for turning on, False for turning off 
    """
    for i, pelt in enumerate(pelts):
        time.sleep(LONG_DELAY)
        with pelt: pelt.state = bool(switch)
        time.sleep(SHORT_DELAY)
        with pelt: s = pelt.state
        time.sleep(SHORT_DELAY)   
        print(f"Pelt {i} : {s}")


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

def log_information(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp, chiller, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up):
    outstring=[]
    outstring_time=datetime.datetime.utcfromtimestamp(time.time())
    outstring.append(outstring_time)
    
    # Read monitoring values into file or something
    #print('NTC ', np.mean([ntcs[i].value for i in MODULES))
    outstring.append(np.mean([ntcs[i].value for i in MODULES]))
    print('HUMI ', humidity := humi.value)
    outstring.append(humidity)
    print('TEMP', np.mean([chuck_temp[i].value for i in MODULES]))
    outstring.append(np.mean([chuck_temp[i].value for i in MODULES]))
    
    dewpoint = calc_dewpoint(humidity, temp_85)
    #print('DEWPOINT ', )
    outstring.append(dewpoint)

    #print('LV setpoint', np.mean([lvs[i].voltage for i in MODULES), np.mean([lvs[i].current for i in MODULES)))
    #print('LV actual', np.mean([lvs[i].measure_voltage.value for i in MODULES), np.mean([lvs[i].measure_current.value for i in MODULES))
    outstring.append(np.mean([lvs[i].measure_voltage.value for i in MODULES]))
    outstring.append(np.mean([lvs[i].measure_current.value for i in MODULES]))
    #print('LV Status', lvs[1].status)

    #print('PELT setpoint', np.mean([pelt_psu[i].voltage for i in MODULES), np.mean([pelt_psu[i].current for i in MODULES) )
    #print('PELT actual', pelt_psu[1].measure_voltage.value, pelt_psu[1].measure_current.value)
    outstring.append(np.mean([pelt_psu[i].measure_voltage.value for i in MODULES]))
    outstring.append(np.mean([pelt_psu[i].measure_current.value for i in MODULES]))
    #outstring.append(0.0) # ONLY WHILE THE REST IS COMMENTED OUT
    #print('PELT Status', pelt_psu[1].status)

    #print('HV setpoint', hvs[0].voltage, hvs[0].current)
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
#time, NTC, HUMI, TEMP, DEWPOINT, LV VOLT, LV CURR, PELT VOLT, PELT CURR, HV VOLT, HV CURR
        "fields": {k: v for k, v in zip(HEADER[1:], outstring[1:])},
        "time": outstring[0]
    }
    write_to_db(write_api, dictionary)

    # Check interlock conditions
    cause = ''
    peltier_on = pelts_read(pelts)
    
    if ntcs[0].value > 70 or ntcs[1].value > 70 or ntcs[2].value > 70 or ntcs[3].value > 70:
        interlock_condition = True 
        cause = 'Temperature'
        print('Interlock triggered due to NTC_temp > 70')

        pelts_on_off(pelts, False, peltier_on)

    if (ntcs[0].value > 65 and any(peltier_on)):
        pelts_on_off(pelts, switch=False)
        print('Peltier turned off due to NTC_temp > 65')
    if dewpoint>chuck_temp[0].value-2 or dewpoint>chuck_temp[1].value-2 or dewpoint>chuck_temp[2].value-2 or dewpoint>chuck_temp[3].value-2:
        interlock_condition = True     
        cause = 'Dewpoint'   
        print('Interlock triggered due to chuck_temp > dewpoint + 2')
    elif dewpoint>chuck_temp[0].value-5 or dewpoint>chuck_temp[1].value-5 or dewpoint>chuck_temp[2].value-5 or dewpoint>chuck_temp[3].value-5:
        if mini_ramp_up == False:
           temp+=5
        mini_ramp_up = True
        print('Target temperature increased due to chuck_temp > dewpoint + 5')
    print('THIS IS THE LID VALUE',lid.value)
    if lid.value < 4:
        interlock_condition = True
        cause = 'Open Lid'
        with base: base.state = False

        pelts_on_off(pelts, peltier_on, False)
    return temp, interlock_condition, mini_ramp_up, cause

def save_ramp_down(ramp_down_data, humi, temp, hv, temp_85):
    print('HV ramp down')
    ramp_down_data.append(datetime.datetime.utcfromtimestamp(time.time()))
    ramp_down_data.append(instruments['ntcs'][1].value)
    ramp_down_data.append(humi.value)
    ramp_down_data.append(instruments['chuck_temp'][1].value)
    ramp_down_data.append(calc_dewpoint(humi.value, temp_85.value))
    ramp_down_data.append(instruments['lvs'][1].measure_voltage.value)
    ramp_down_data.append(instruments['lvs'][1].measure_current.value)
    ramp_down_data.append(instruments['pelt_psu'][1].measure_voltage.value)
    ramp_down_data.append(instruments['pelt_psu'][1].measure_current.value)
    ramp_down_data.append(hv.voltage)
    ramp_down_data.append(hv.current)
    ramp_down_data.append('\n')

def safe_shutdown(cause, humi, temp, pelt, base):
    print('[SAFE_SHUTDOWN] > please wait patiently...')
    with base: base.temperature = 20
    ramp_down_data = []
    for hv in instruments['hvs']:
        if hv.state and hv.voltage > 0.001:
            hv.sweep(0, step_size=-5, execute_each_step = lambda: save_ramp_down(ramp_down_data, humi, temp, hv))      # Sweep to zero at rate 10V/s 
        hv.state = False
    for pelt in instruments['pelt_psu']:
        pelt.current = 0
        pelt.state = False
    print('... this takes a while')
    for lv in instruments['lvs']:
        lv.state = False
    show_warning(cause)
    return ramp_down_data

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
        safe_shutdown()
        sys.exit(1)
    else:
        please_kill = True

def ramp_up(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp : list, chiller, pelts : list, base, HEADER, write_api, lid, temp_85, mini_ramp_up, max_temp):
    logging.info('INSIDE RAMP UP')
    peltier_on = pelts_read(pelts)
    cause = ''
    pelts_on_off(pelts, peltier_on, False)
    while temp < max_temp: #Go up
                with base: base.temperature = temp + 10
                pelt_temperature_now = np.mean([chuck_temp[i].value for i in MODULES])
                
                while pelt_temperature_now < temp - 0.1:
                    print('Reaching desired temperature', temp)            
                    with base: print(base.temperature)
                    temp, interlock_condition, mini_ramp_up, cause = log_information(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp, chiller, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up)
                    time.sleep(5)
                    pelt_temperature_now = np.mean([chuck_temp[i].value for i in MODULES])

                    if interlock_condition:
                        break
                temp += 1          
                if interlock_condition:
                    break
    return interlock_condition, cause

def ramp_down(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp : list, chiller, pelts : list, base, HEADER, write_api, lid, temp_85, mini_ramp_up, min_temp):
    print('INSIDE RAMP DOWN')
    peltier_on = pelts_read(pelts)
 
    cause = ''
    print("TRYING TO RAMP DOWN, TURN pelt_psu ON")
    pelts_on_off(pelts, peltier_on, True)

    while temp > min_temp: #Go down
                with base: base.temperature = temp + 5
                
                time.sleep(LONG_DELAY)
                for i, pelt in enumerate(pelts):
                    logging.info(f"Ramp down: Setting pelt{i} temperature to {temp}")
                    with pelt: pelt.temperature = temp
                    time.sleep(LONG_DELAY)
                
                pelt_temperature_now = np.mean([chuck_temp[i].value for i in MODULES])
                while pelt_temperature_now > temp + 0.1:
                    print('Reaching desired temperature', temp)            
                    with base: print(f"Chiller: { base.temperature}")
                    #for i, pelt in enumerate(pelts):
                    #    with pelt: print(pelt.temperature)
                    #    time.sleep(SHORT_DELAY)
                    print(f"Current chuck temp: {np.mean([chuck_temp[i].value for i in MODULES])}")
                    temp, interlock_condition, mini_ramp_up, cause = log_information(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp, chiller, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up)
                    if mini_ramp_up:
                        print('INSIDE MINI RAMP UP TEMP', temp)
                        interlock_condition, cause = ramp_up(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp - 5, chuck_temp, chiller, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up, temp) #Last temp is the new target temperature (increased by 5 through the mini ramp up condition and set to level in the temp - 5)
                        mini_ramp_up = False
                        peltier_on = pelts_read(pelts)

                        pelts_on_off(pelts, peltier_on, True)

                    time.sleep(LONG_DELAY)
                    pelt_temperature_now = np.mean([chuck_temp[i].value for i in MODULES])
                    #JAY 
                    if interlock_condition:
                        #print("INTERLOCK CONDITION IN LOOP")
                        break 
                temp -= 1               
                if interlock_condition:
                    #print("INTERLOCK CONDITION OUT OF LOOP")
                    break
    return interlock_condition, cause

def main():
    signal.signal(signal.SIGINT, signal_handler)
    
    # interlock variables
    interlock = ITkDCSInterlock(resource='TCPIP::localhost::9898::SOCKET')
    ntcs = [interlock.channel("MeasureChannel", channel, measure_type='NTC:TEMP') for channel in (1, 2, 3, 4)]
    chuck_temp = [interlock.channel("MeasureChannel", channel, measure_type='PT100:TEMP') for channel in (1, 2, 3, 4)] #Temperature of the module chuck
    humi = interlock.channel("MeasureChannel", 1, measure_type='SHT85:HUMI')
    temp_85 = interlock.channel("MeasureChannel", 1, measure_type='SHT85:TEMP') #Temperature of the peltier back
    lid = interlock.channel("MeasureChannel", 1, measure_type='LID:VOLT')
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

    pelts = []
    port0 = 19895
    for i in MODULES:
        # These config files should only contain 1 channel each.
        tricicle = open_tricicle(f"../configs/pidcontroller_j{i+1}.toml", port=port0+i) 
        time.sleep(5)
        processes.append(tricicle)
        p = PIDController(resource = f"TCPIP::localhost::{port0+i}::SOCKET")
        pelts.append(p.channel("TemperatureChannel", 1)) # must assign channel 1 (maybe?)

    
    for ch in [*ntcs, *lvs, *pelt_psu, *hvs, humi, *chuck_temp]:
        ch.__enter__()
    try:
        main_with_instruments(ntcs, lvs, pelt_psu, hvs, humi, chuck_temp, chiller, pelts, base, lid, temp_85)
    finally:
        instruments = {}
        for ch in [*ntcs, *lvs, *pelt_psu, *hvs, humi, *chuck_temp]:
            ch.__exit__(None, None, None)


def main_with_instruments(ntcs, lvs, pelt_psu, hvs, humi, chuck_temp, chiller, pelts, base, lid, temp_85):

    instruments['ntcs'] = ntcs
    instruments['lvs'] = lvs
    instruments['pelt_psu'] = pelt_psu
    instruments['hvs'] = hvs
    instruments['chuck_temp'] = chuck_temp
    instruments['chiller'] = chiller
    write_api = None
    
    write_api = connect_to_db(ENDPOINT)
    if write_api is None:
        print('[ERROR]: Cannot connect to database. Refusing to run.')
        sys.exit(1)
    
    
    qc_tool = open_module_qc_tools()
    processes.append(qc_tool)

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
        max_cycles = 10
        #max_temp = 60 #1 cycle
        #min_temp = -55
        max_temp = 40 #10 cycles
        min_temp = -45
        #max_temp = 25 #Mini test
        #min_temp = 15
        with base: base.speed = 2000
        with base: base.state = True
        
        print(peltier_on := pelts_read(pelts))
        #pelts_on_off(pelts, 0, True)
        
        while not please_kill and cycles<max_cycles:
            print(f"\n*********Cycle {cycles}*********\n")
            temp = 20            
            interlock_condition, cause = ramp_down(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp, chiller, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up, min_temp)
            temp = min_temp
            interlock_condition, cause = ramp_up(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp, chiller, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up, max_temp)
            temp = max_temp            
            interlock_condition, cause = ramp_down(fl, interlock_condition, ntcs, lvs, pelt_psu, hvs, humi, temp, chuck_temp, chiller, pelts, base, HEADER, write_api, lid, temp_85, mini_ramp_up, 20)
            cycles += 1
            if interlock_condition:
                break
        
        
        if interlock_condition:
            with base: base.state = True
            with base: base.temperature = 20

    fl.close()

def show_warning(cause):
    app = QApplication(sys.argv)
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Warning) 
    msg.setText(f"{cause} above expected level, ramping down voltages and terminating any scans")
    msg.setWindowTitle("Warning")
    msg.show()


    #QTimer.singleShot(0, lambda: msg.exec_())
    msg.exec_()
    #app.exec_()

def open_tricicle(config_file, port = 19898):
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

def open_module_qc_tools(tool=sys.argv[1]):
    return subprocess.Popen([*BASE_COMMAND_XTERM, shutil.which(tool), *sys.argv[2:]], stdin=None, stdout=None, stderr=None)
    
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
        print('\nConnected to database, logging locally, and to influxDB if switched on!\n')
        return write_api
    else:
        print('\nCould not connect to database, only logging locally!\n')
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
    main()
