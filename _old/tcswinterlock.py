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
from icicle.hmp4040 import HMP4040

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO) # JAY

processes = []
instruments = {}
please_kill = False

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

def log_information(fl, interlock_condition, ntcs, lvs, peltiers, hvs, humi, temp, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, lid, temp_85, mini_ramp_up):
    outstring=[]
    outstring_time=datetime.datetime.utcfromtimestamp(time.time())
    outstring.append(outstring_time)

    # Read monitoring values into file or something
    print('NTC ', ntcs[1].value)
    outstring.append(ntcs[1].value)
    print('HUMI ', humidity := humi.value)
    outstring.append(humidity)
    print('TEMP', temps[1].value)
    outstring.append(temps[1].value)
    if humidity <= 0.00001:
        dewpoint = -100.0
    else:
        try:
            dewpoint = 243.04*(math.log(humidity/100)+17.625*temp_85.value/(243.04+temp_85.value))/(17.625-math.log(humidity/100)-17.625*temp_85.value/(243.04+temp_85.value))
        except Exception as e:
            print(f'Exception: {e} with humi: {humidity} temp: {temp_85.value}')
            dewpoint = -100
    print('DEWPOINT ', dewpoint)
    outstring.append(dewpoint)

    print('LV setpoint', lvs[1].voltage, lvs[1].current)
    print('LV actual', lvs[1].measure_voltage.value, lvs[1].measure_current.value)
    outstring.append(lvs[1].measure_voltage.value)
    outstring.append(lvs[1].measure_current.value)
    print('LV Status', lvs[1].status)

    #print('PELT setpoint', peltiers[1].voltage, peltiers[1].current)
    #print('PELT actual', peltiers[1].measure_voltage.value, peltiers[1].measure_current.value)
    #outstring.append(peltiers[1].measure_voltage.value)
    #outstring.append(peltiers[1].measure_current.value)
    outstring.append(0.0) # ONLY WHILE THE REST IS COMMENTED OUT
    outstring.append(0.0)
    #print('PELT Status', peltiers[1].status)

    print('HV setpoint', hvs[0].voltage, hvs[0].current)
    print('HV State', hvs[0].state)
    if int(hvs[0].state) == 1:
        print('HV actual', hvs[0].measure_voltage.value, hvs[0].measure_current.value)
        outstring.append(hvs[0].measure_voltage.value)
        outstring.append(hvs[0].measure_current.value)
    else:
        print('HV off, will not read voltage and current')
        outstring.append(0.0)
        outstring.append(0.0)
    print('HV Status', hvs[0].state)

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
    #write_to_db(write_api, dictionary) #JAY

    # Check interlock conditions
    cause = ''
    with pelt1: peltier1_on = pelt1.state    
    with pelt2: peltier2_on = pelt2.state   
    with pelt3: peltier3_on = pelt3.state   
    with pelt4: peltier4_on = pelt4.state   
    if ntcs[0].value > 70 or ntcs[1].value > 70 or ntcs[2].value > 70 or ntcs[3].value > 70:
        interlock_condition = True 
        cause = 'Temperature'
        print('Interlock triggered due to NTC_temp > 70')
        if peltier1_on:
            with pelt1: pelt1.state = False
        if peltier2_on:
            with pelt2: pelt2.state = False
        if peltier3_on:
            with pelt3: pelt3.state = False
        if peltier4_on:
            with pelt4: pelt4.state = False
    if (ntcs[0].value > 65 and peltier1_on): #or (ntcs[1].value > 65 and peltier2_on):# or (ntcs[2].value > 65 and peltier3_on) or (ntcs[3].value > 65 and peltier4_on):
        with pelt1: pelt1.state = False
        with pelt2: pelt2.state = False
        with pelt3: pelt3.state = False
        with pelt4: pelt4.state = False
        print('Peltier turned off due to NTC_temp > 65')
    if dewpoint>temps[0].value-2 or dewpoint>temps[1].value-2 or dewpoint>temps[2].value-2 or dewpoint>temps[3].value-2:
        interlock_condition = True     
        cause = 'Dewpoint'   
        print('Interlock triggered due to chuck_temp > dewpoint + 2')
    elif dewpoint>temps[0].value-5 or dewpoint>temps[1].value-5 or dewpoint>temps[2].value-5 or dewpoint>temps[3].value-5:
        if mini_ramp_up == False:
           temp+=5
        mini_ramp_up = True
        print('Target temperature increased due to chuck_temp > dewpoint + 5')
    print('THIS IS THE LID VALUE',lid.value)
    '''if lid.value < 4:
        interlock_condition = True
        cause = 'Open Lid'
        with base: base.state = False
        if peltier1_on:
            with pelt1: pelt1.state = False
        if peltier2_on:
            with pelt2: pelt2.state = False
        if peltier3_on:
            with pelt3: pelt3.state = False
        if peltier4_on:
            with pelt4: pelt4.state = False'''
    return temp, interlock_condition, mini_ramp_up, cause

def log_interlock_condition_information(fl, ntcs, lvs, peltiers, hvs, humi, temp, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, temp_85, cause):
    outstring = []
    kill_processes()
    ramp_down_output = safe_shutdown(cause, humi, temp, pelt1, pelt2, pelt3, pelt4, base)
    outstring.extend(ramp_down_output)
    for i in outstring:
        if i == '\n':
            fl.write('\n')
        else:
            fl.write(str(i)+', ')
    for j in range(1,5):
        outstring = []
        outstring_time=datetime.datetime.utcfromtimestamp(time.time())
        outstring.append(outstring_time)
        print('NTC ', ntcs[1].value)
        outstring.append(ntcs[1].value)
        print('HUMI ', humidity := humi.value)
        outstring.append(humidity)
        print('TEMP', temps[1].value)
        outstring.append(temps[1].value)
        if humidity <= 0.00001:
            dewpoint = -100.0
        else:
            try:
                dewpoint = 243.04*(math.log(humidity/100)+17.625*temp_85.value/(243.04+temp_85.value))/(17.625-math.log(humidity/100)-17.625*temp_85.value/(243.04+temp_85.value))
            except Exception as e:
                print(f'Exception: {e} with humi: {humidity} temp: {temps[1].value}')
        print('DEWPOINT ', dewpoint)
        outstring.append(dewpoint)

        print('LV setpoint', lvs[1].voltage, lvs[1].current)
        print('LV actual', lvs[1].measure_voltage.value, lvs[1].measure_current.value)
        outstring.append(lvs[1].measure_voltage.value)
        outstring.append(lvs[1].measure_current.value)
        print('LV Status', lvs[1].status)

        #print('PELT setpoint', peltiers[1].voltage, peltiers[1].current)
        #print('PELT actual', peltiers[1].measure_voltage.value, peltiers[1].measure_current.value)
        #outstring.append(peltiers[1].measure_voltage.value)
        #outstring.append(peltiers[1].measure_current.value)
        outstring.append(0.0) # ONLY WHILE THE REST IS COMMENTED OUT
        outstring.append(0.0)
        #print('PELT Status', peltiers[1].status)
        
        print('HV setpoint', hvs[0].voltage, hvs[0].current)
        print('HV State', hvs[0].state)
        if int(hvs[0].state) == 1:
            print('HV actual', hvs[0].measure_voltage.value, hvs[0].measure_current.value)
            outstring.append(hvs[0].measure_voltage.value)
            outstring.append(hvs[0].measure_current.value)
        else:
            print('HV off, will not read voltage and current')
            outstring.append(0.0)
            outstring.append(0.0)
        print('HV Status', hvs[0].state)
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
        #write_to_db(write_api, dictionary) #JAY

def save_ramp_down(ramp_down_data, humi, temp, hv, temp_85):
    print('HV ramp down')
    ramp_down_data.append(datetime.datetime.utcfromtimestamp(time.time()))
    ramp_down_data.append(instruments['ntcs'][1].value)
    ramp_down_data.append(humi.value)
    ramp_down_data.append(instruments['temps'][1].value)
    if humi.value <= 0.00001:
        dewpoint = -100
    else:
        try:
            dewpoint = 243.04*(math.log(humi.value/100)+17.625*temp_85.value/(243.04+temp_85.value))/(17.625-math.log(humi.value/100)-17.625*temp_85.value/(243.04+temp_85.value))
        except Exception as e:
            print(f'Exception: {e} with humi: {humi.value}')
            dewpoint = -100
    ramp_down_data.append(dewpoint)
    ramp_down_data.append(instruments['lvs'][1].measure_voltage.value)
    ramp_down_data.append(instruments['lvs'][1].measure_current.value)
    ramp_down_data.append(instruments['peltiers'][1].measure_voltage.value)
    ramp_down_data.append(instruments['peltiers'][1].measure_current.value)
    ramp_down_data.append(hv.voltage)
    ramp_down_data.append(hv.current)
    ramp_down_data.append('\n')

def safe_shutdown(cause, humi, temp, pelt1, pelt2, pelt3, pelt4, base):
    print('[SAFE_SHUTDOWN] > please wait patiently...')
    with base: base.temperature = 20
    ramp_down_data = []
    for hv in instruments['hvs']:
        if hv.state and hv.voltage > 0.001:
            hv.sweep(0, step_size=-5, execute_each_step = lambda: save_ramp_down(ramp_down_data, humi, temp, hv, temp_85))      # Sweep to zero at rate 10V/s 
        hv.state = False
    for pelt in instruments['peltiers']:
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

def ramp_up(fl, interlock_condition, ntcs, lvs, peltiers, hvs, humi, temp, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, lid, temp_85, mini_ramp_up, max_temp):
    logging.info('INSIDE RAMP UP')
    
    with pelt1: peltier1_on = pelt1.state
    logging.info(f"Ramp up: pelt1 is {peltier1_on}")    
    with pelt2: peltier2_on = pelt2.state
    logging.info(f"Ramp up: pelt2 is {peltier2_on}")    
    with pelt3: peltier3_on = pelt3.state
    logging.info(f"Ramp up: pelt3 is {peltier3_on}")
    with pelt4: peltier4_on = pelt4.state
    logging.info(f"Ramp up: pelt4 is {peltier4_on}")
    #time.sleep(5)    
    cause = ''
    if peltier1_on:
        logging.info("Ramp up: Turning pelt1 off") # JAY
        with pelt1: pelt1.state = False
    if peltier2_on:
        time.sleep(10) # JAY
        
        logging.info("Ramp up: Turning pelt2 off") #JAY
        try:
            with pelt2: pelt2.state = False
        except:
            logging.info("Ramp up: Failed to turn pelt2 off, retrying")
            for i in range(4):
                time.sleep(10 + i)
                with pelt2: pelt2.state = False
        #with pelt2: pelt2.state = False
    if peltier3_on:
        time.sleep(10) # JAY
        logging.info("Ramp up: Turning pelt3 off") # JAY
        try:
            with pelt3: pelt3.state = False
        except:
            logging.info("Ramp up: Failed to turn pelt3 off, retrying")
            for i in range(4):
                time.sleep(10 + i)
                with pelt3: pelt3.state = False
    if peltier4_on:
        logging.debug("Turning pelt4 off") # JAY
        time.sleep(2) # JAY
        try:
            with pelt4:pelt4.state = False
        except:
            logging.info("Failed to turn pelt4 off, retrying")
            for i in range(4):
                time.sleep(10 + i)
                with pelt4: pelt4.state = False
    
    while temp < max_temp: #Go up
                with base: base.temperature = temp + 10
                #with pelt2: pelt2.temperature = temp
                pelt_temperature_now = temps[1].value
                while pelt_temperature_now < temp - 0.1:
                    print('Reaching desired temperature', temp)            
                    with base: print(base.temperature)
                    #with pelt2: print(pelt2.temperature)
                    temp, interlock_condition, mini_ramp_up, cause = log_information(fl, interlock_condition, ntcs, lvs, peltiers, hvs, humi, temp, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, lid, temp_85, mini_ramp_up)
                    time.sleep(5)
                    pelt_temperature_now = temps[1].value

                    if interlock_condition:
                        break
                temp += 1          
                if interlock_condition:
                    break
    return interlock_condition, cause

def ramp_down(fl, interlock_condition, ntcs, lvs, peltiers, hvs, humi, temp, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, lid, temp_85, mini_ramp_up, min_temp):
    print('INSIDE RAMP DOWN')
    with pelt1: peltier1_on = pelt1.state    
    with pelt2: peltier2_on = pelt2.state    
    with pelt3: peltier3_on = pelt3.state    
    with pelt4: peltier4_on = pelt4.state    
    cause = ''
    if not peltier1_on:
        logging.info("Ramp down: Turn pelt1 on") #JAY
        with pelt1: pelt1.state = True
    if not peltier2_on:
        logging.info("Ramp down: Turn pelt2 on") #JAY
        with pelt2: pelt2.state = True
    if not peltier3_on:
        logging.info("Ramp down: Turn pelt3 on") #JAY
        with pelt3: pelt3.state = True
    if not peltier4_on:
        logging.info("Ramp down: Turn pelt4 on") #JAY
        with pelt4: pelt4.state = True
    while temp > min_temp: #Go down
                with base: base.temperature = temp + 5
                with pelt1: pelt1.temperature = temp
                logging.debug(f"Ramp down: Set pelt1 temperature to {temp}") 
                with pelt2: pelt2.temperature = temp
                logging.debug(f"Ramp down: Set pelt2 temperature to {temp}")
                with pelt3: pelt3.temperature = temp
                logging.debug(f"Ramp down: Set pelt3 temperature to {temp}")
                with pelt4: pelt4.temperature = temp
                logging.debug(f"Ramp down: Set pelt4 temperature to {temp}")
                pelt_temperature_now = temps[1].value
                while pelt_temperature_now > temp + 0.1:
                    print('Reaching desired temperature', temp)            
                    with base: print(base.temperature)
                    with pelt1: print(pelt1.temperature)
                    with pelt2: print(pelt2.temperature)
                    with pelt3: print(pelt3.temperature)
                    with pelt4: print(pelt4.temperature)
                    print(temps[1].value)
                    temp, interlock_condition, mini_ramp_up, cause = log_information(fl, interlock_condition, ntcs, lvs, peltiers, hvs, humi, temp, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, lid, temp_85, mini_ramp_up)
                    if mini_ramp_up:
                        print('INSIDE MINI RAMP UP TEMP', temp)
                        interlock_condition, cause = ramp_up(fl, interlock_condition, ntcs, lvs, peltiers, hvs, humi, temp - 5, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, lid, temp_85, mini_ramp_up, temp) #Last temp is the new target temperature (increased by 5 through the mini ramp up condition and set to level in the temp - 5)
                        mini_ramp_up = False
                        with pelt1: peltier1_on = pelt1.state    
                        with pelt2: peltier2_on = pelt2.state    
                        with pelt3: peltier3_on = pelt3.state    
                        with pelt4: peltier4_on = pelt4.state    
                        if not peltier1_on:
                            with pelt1: pelt1.state = True
                        if not peltier2_on:
                            with pelt2: pelt2.state = True
                        if not peltier3_on:
                            with pelt3: pelt3.state = True
                        if not peltier4_on:
                            with pelt4: pelt4.state = True
                    time.sleep(5)
                    pelt_temperature_now = temps[1].value

                    if interlock_condition:
                        break 
                temp -= 1               
                if interlock_condition:
                    break
    return interlock_condition, cause

def main():
    signal.signal(signal.SIGINT, signal_handler)
    
    # interlock variables
    p = PIDController(resource = "TCPIP::localhost::19898::SOCKET")
    interlock = ITkDCSInterlock(resource='TCPIP::localhost::9898::SOCKET')
    ntcs = [interlock.channel("MeasureChannel", channel, measure_type='NTC:TEMP') for channel in (1, 2, 3, 4)]
    temps = [interlock.channel("MeasureChannel", channel, measure_type='PT100:TEMP') for channel in (1, 2, 3, 4)] #Temperature of the module chuck
    humi = interlock.channel("MeasureChannel", 1, measure_type='SHT85:HUMI')
    temp_85 = interlock.channel("MeasureChannel", 1, measure_type='SHT85:TEMP') #Temperature of the peltier back
    lid = interlock.channel("MeasureChannel", 1, measure_type='LID:VOLT')
    lv_psu = HMP4040(resource='ASRL/dev/ttyHMP4040a::INSTR')
    lvs = [lv_psu.channel("PowerChannel", channel) for channel in (1, 2, 3, 4)]
    peltier_psu = HMP4040(resource='ASRL/dev/ttyHMP4040b::INSTR')
    peltiers = [peltier_psu.channel("PowerChannel", channel) for channel in (1, 2, 3, 4)]
    hv_psu = Keithley2410(resource='ASRL/dev/ttyUSB0::INSTR')
    #hv_psu = Keithley2410(resource='ASRL/dev/ttyHMP4040b::INSTR') #PLACEHOLDER FOR WHEN THE HV ISN'T ATTACHED, REMOVE!!!!
    hvs = [hv_psu.channel("PowerChannel", 1)]
    h = hubercc508.HuberCC508(resource = '/dev/ttyACM0')
    base = h.channel("TemperatureChannel", 1)
    chiller = h.channel("TemperatureChannel", 1)
    pelt1 = p.channel("TemperatureChannel", 1)
    pelt2 = p.channel("TemperatureChannel", 2)
    pelt3 = p.channel("TemperatureChannel", 3)
    pelt4 = p.channel("TemperatureChannel", 4)


    for ch in [*ntcs, *lvs, *peltiers, *hvs, humi, *temps]:
        ch.__enter__()
    try:
        main_with_instruments(ntcs, lvs, peltiers, hvs, humi, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, lid, temp_85)
    finally:
        instruments = {}
        for ch in [*ntcs, *lvs, *peltiers, *hvs, humi, *temps]:
            ch.__exit__(None, None, None)


def main_with_instruments(ntcs, lvs, peltiers, hvs, humi, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, lid, temp_85):

    instruments['ntcs'] = ntcs
    instruments['lvs'] = lvs
    instruments['peltiers'] = peltiers
    instruments['hvs'] = hvs
    instruments['temps'] = temps
    instruments['chiller'] = chiller

    write_api = connect_to_db(ENDPOINT)
    if write_api is None:
        print('[ERROR]: Cannot connect to database. Refusing to run.')
        sys.exit(1)
    
    tricicle = open_tricicle("../configs/pidcontroller.toml")
    processes.append(tricicle)
    
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
        #max_temp = 40 #10 cycles
        #min_temp = -45
        max_temp = 25 #Mini test
        min_temp = 15
        with base: base.speed = 2000
        with base: base.state = True
        while not please_kill and cycles<max_cycles:
            temp = 20            
            interlock_condition, cause = ramp_down(fl, interlock_condition, ntcs, lvs, peltiers, hvs, humi, temp, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, lid, temp_85, mini_ramp_up, min_temp)
            temp = min_temp            
            interlock_condition, cause = ramp_up(fl, interlock_condition, ntcs, lvs, peltiers, hvs, humi, temp, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, lid, temp_85, mini_ramp_up, max_temp)
            temp = max_temp            
            interlock_condition, cause = ramp_down(fl, interlock_condition, ntcs, lvs, peltiers, hvs, humi, temp, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, lid, temp_85, mini_ramp_up, 20)
            cycles += 1
            if interlock_condition:
                break

        with pelt1: peltier1_on = pelt1.state
        with pelt2: peltier2_on = pelt2.state
        with pelt3: peltier3_on = pelt3.state
        with pelt4: peltier4_on = pelt4.state
        if peltier1_on:
            with pelt1: pelt1.state = False  
        if peltier2_on:
            with pelt2: pelt2.state = False 
        if peltier3_on:
            with pelt3: pelt3.state = False 
        if peltier4_on:
            with pelt4: pelt4.state = False       
        if interlock_condition:
            with base: base.state = True
            with base: base.temperature = 20
            log_interlock_condition_information(fl, ntcs, lvs, peltiers, hvs, humi, temp, temps, chiller, pelt1, pelt2, pelt3, pelt4, base, HEADER, write_api, temp_85, cause)
				    

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

def open_tricicle(config_file):
    print(shutil.which("pidcontroller-ui"))
    #return subprocess.Popen([shutil.which("pidcontroller-ui"), "-c", config_file, "-a"], stdin=None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return None

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
               token='eyJrIjoiVWU2NWpWSW90a0tJS0dmOUxvU3NJZE9TdDE3cm9ERHEiLCJuIjoiQ2FuYXJ5X3Rlc3QiLCJpZCI6MX0=',
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
