#!/usr/bin/env python3

"""
Program with web and physical interfaces to allow the control of a DAC that controls the power to the J2010 filament. Smoothly ramps up and ramps down the power to prevent damage to the crystal. Writes out a CSV file to ~/filament_controller_log.csv with the following columns:
Raw Timestamp - Floating-point number of seconds since the Unix epoch.
Formatted Timestamp - Timestamp of the format "%m/%d/%Y %I:%M:%S %p" (see the strftime (3) manpage for details).
Control Type - PANEL, WEB, SHUTOFF, or AUTO, depending on whether the control action was initiated from the panel switches and buttons, the web interface, done automatically by the shutoff timer, or done automatically when the program started.
Control Action - SWITCH_TO_MANUAL_CONTROL, SWITCH_TO_COMPUTER_CONTROL, FILAMENT_ON, or FILAMENT_OFF, depending on what action was taken. The first two actions can only be initiated by PANEL control (by the user moving the physical switch), but the last two can be PANEL, WEB, SHUTOFF, or AUTO control.
IP Address - A string like "192.168.1.168" with the IP address of the web request that initiated the action. Empty for PANEL actions.
MAC Address - A string like "00:1B:44:11:3A:B7" with the MAC address of the computer that originated the action (may not be accurate if the computer is not on the same LAN as the Raspberry Pi). Empty for PANEL actions.
"""

import RPi.GPIO as GPIO

RAMP_TIME_SECONDS = 30                        # Time over which the filament is to be ramped up or down, in seconds.
SHUTOFF_TIMER_DURATION_SECONDS = 8 * 60 * 60  # Time, in seconds, after which the filament will be automatically shut off.
ACTIVE_USER_MAX_IDLE_TIME_SECONDS = 5         # Time, in seconds, after which an active user who is not sending /status requests will be marked as inactive, in seconds.
DAC_BITS = 12                                 # Number of bits offered by the DAC. Raw values sent to the dac will be in the range [0, 2^DAC_BITS).
RED_LED_PIN = 19                              # Pin number (BCM numbering scheme) used to control the red status LED.
GREEN_LED_PIN = 26                            # Pin number (BCM numbering scheme) used to control the green status LED.
CONTROL_MODE_SWITCH_PIN = 13                  # Pin number (BCM numbering scheme) used to read the switch that sets whether the filament is under manual or computer control.
CONTROL_MODE_PIN_STATE_MANUAL = GPIO.HIGH     # GPIO state of the CONTROL_MODE_SWITCH_PIN that corresponds to manual control.
CONTROL_MODE_PIN_STATE_COMPUTER = GPIO.LOW    # GPIO state of the CONTROL_MODE_SWITCH_PIN that corresponds to computer control.
ON_BUTTON_PIN = 6                             # GPIO state of the pin that corresponds to the on button. This pin should go LOW when pressed.
OFF_BUTTON_PIN = 5                            # GPIO state of the pin that corresponds to the off button. This pin should go LOW when pressed.
LED_FLASH_FREQUENCY_HZ = 1                    # Frequency (in Hertz) at which the status LED flashes.

import json
import time
import os.path
import threading
from datetime import datetime
from getmac import get_mac_address
from flask import Flask, render_template, jsonify, make_response, request

import board
import busio
import adafruit_mcp4725

# Functions to control the status LED.
def status_led_off():
    global red_led_pwm
    global green_led_pwm
    red_led_pwm.ChangeDutyCycle(0)
    green_led_pwm.ChangeDutyCycle(0)

def status_led_solid_red():
    global red_led_pwm
    global green_led_pwm
    red_led_pwm.ChangeDutyCycle(100)
    green_led_pwm.ChangeDutyCycle(0)

def status_led_solid_green():
    global red_led_pwm
    global green_led_pwm
    red_led_pwm.ChangeDutyCycle(0)
    green_led_pwm.ChangeDutyCycle(100)

def status_led_flash_red():
    global red_led_pwm
    global green_led_pwm
    red_led_pwm.ChangeDutyCycle(50)
    green_led_pwm.ChangeDutyCycle(0)

def status_led_flash_green():
    global red_led_pwm
    global green_led_pwm
    red_led_pwm.ChangeDutyCycle(0)
    green_led_pwm.ChangeDutyCycle(50)

# States for the controller state machine.
STARTING = 0
OFF = 1
ON = 2
RAMP_UP = 3
RAMP_DOWN = 4
state = STARTING # Current state of the controller state machine.

dac_value = 0 # Current value of the DAC (will be initialized by the controller thread).
max_dac_value = int(open("/home/pi/FilamentController/max_dac_value.txt").read().replace('\n', ''))  # Maximum allowed value of the DAC.
on_button_pressed = False # Whether the ON button was just pressed, either on the webpage or the physical buttons.
off_button_pressed = False # Whether the OFF button was just pressed, either on the webpage or the physical buttons.
computer_control = True # Whether the filament is currently being controlled by the Pi or by the manual knob.
active_users = {} # Dictionary associating connected web clients' IP addresses and the time.time() timestamp at which they last called the /status endpoint.
shutoff_timer_start = time.time() # When (in seconds since the Unix epoch) the shutoff timer was started.

# Set up GPIO.
GPIO.setmode(GPIO.BCM)
GPIO.setup(RED_LED_PIN, GPIO.OUT)
red_led_pwm = GPIO.PWM(RED_LED_PIN, LED_FLASH_FREQUENCY_HZ)
red_led_pwm.start(0.0)
GPIO.setup(GREEN_LED_PIN, GPIO.OUT)
green_led_pwm = GPIO.PWM(GREEN_LED_PIN, LED_FLASH_FREQUENCY_HZ)
green_led_pwm.start(0.0)
GPIO.setup(CONTROL_MODE_SWITCH_PIN, GPIO.IN, pull_up_down = GPIO.PUD_UP)
if GPIO.input(CONTROL_MODE_SWITCH_PIN) == CONTROL_MODE_PIN_STATE_MANUAL:
    computer_control = False
    status_led_off()
GPIO.setup(ON_BUTTON_PIN, GPIO.IN, pull_up_down = GPIO.PUD_UP)
GPIO.setup(OFF_BUTTON_PIN, GPIO.IN, pull_up_down = GPIO.PUD_UP)

# Interrupt request handler for the manual/computer control switch.
def control_switch_interrupt(_):
    global computer_control
    time.sleep(0.01) # If you sleep for too long, the RPi.GPIO library will dispatch two ISRs. If it's too short, then the GPIO.input() may read a bouncing switch value. This value seems to work well with the 50 ms debounce.
    if GPIO.input(CONTROL_MODE_SWITCH_PIN) == CONTROL_MODE_PIN_STATE_MANUAL:
        computer_control = False
        status_led_off()
    else:
        computer_control = True
        if state == OFF or state == STARTING:
            status_led_solid_red()
        elif state == ON:
            status_led_solid_green()
        elif state == RAMP_UP:
            status_led_flash_green()
        elif state == RAMP_DOWN:
            status_led_flash_red()
GPIO.add_event_detect(CONTROL_MODE_SWITCH_PIN, GPIO.BOTH, callback = control_switch_interrupt, bouncetime = 50)

# Interrupt request handler for the on button.
def on_button_pressed_interrupt(_):
    global on_button_pressed
    if computer_control:
        on_button_pressed = True
GPIO.add_event_detect(ON_BUTTON_PIN, GPIO.FALLING, callback = on_button_pressed_interrupt, bouncetime = 50)

# Interrupt request handler for the off button.
def off_button_pressed_interrupt(_):
    global off_button_pressed
    if computer_control:
        off_button_pressed = True
GPIO.add_event_detect(OFF_BUTTON_PIN, GPIO.FALLING, callback = off_button_pressed_interrupt, bouncetime = 50)

# Set up logging.
logfile_name = "/home/pi/filament_controller_log.csv"
if os.path.exists(logfile_name):
    logfile = open(logfile_name, 'a')
else:
    logfile = open(logfile_name, 'w')
    logfile.write("Raw Timestamp,Formatted Timestamp,Control Type,Control Action,IP Address,MAC Address\n")

# Function to write a row to the CSV logfile.
def log_action(control_type, control_action, ip_address):
    global logfile
    if ip_address != "":
        if ':' in ip_address:
            mac_address = get_mac_address(ip6 = ip_address)
        else:
            mac_address = get_mac_address(ip = ip_address)
    else:
            mac_address = ""
    raw_timestamp = time.time()
    formatted_timestamp = datetime.fromtimestamp(raw_timestamp).strftime("%m/%d/%Y %I:%M:%S %p")
    logfile.write("{},{},{},{},{},{}\n".format(raw_timestamp, formatted_timestamp, control_type, control_action, ip_address, mac_address))
    logfile.flush()

# This function runs in a separate thread and handles actually controlling the filament.
def controller_thread():
    global state
    global dac_value
    global on_button_pressed
    global off_button_pressed
    global shutoff_timer_start
    # Open the DAC device.
    i2c = busio.I2C(board.SCL, board.SDA)
    dac = adafruit_mcp4725.MCP4725(i2c)
    # Get the current value of the DAC. We read the value 10 times from I2C to avoid reading an erroneously high value that would cause the RAMP_DOWN state to write that erroneously high value back to the DAC and break the crystal.
    dac_readings = []
    print("Starting controller...", end = '', flush = True)
    for _ in range(10):
        dac_readings.append(dac.raw_value)
        print('.', end = '', flush = True)
        time.sleep(0.05)
    print("\nController started.")
    dac_value = int(sum(dac_readings) / float(len(dac_readings)))
    # If the DAC isn't off right now, ramp down.
    if dac_value > 0:
        log_action("AUTO", "FILAMENT_OFF", "")
        state = RAMP_DOWN
        if computer_control:
            status_led_flash_red()
    else:
        state = OFF
        if computer_control:
            status_led_solid_red()
    # Main state machine loop.
    while True:
        if state == OFF:
            if on_button_pressed:
                shutoff_timer_start = time.time()
                status_led_flash_green()
                state = RAMP_UP
            off_button_pressed = False
            on_button_pressed = False
            time.sleep(0.1)
        elif state == RAMP_UP:
            off_button_pressed = False
            on_button_pressed = False
            time.sleep(float(RAMP_TIME_SECONDS) / (max_dac_value + 1))
            dac_value += 1
            dac.raw_value = dac_value
            if dac_value >= max_dac_value:
                state = ON
                status_led_solid_green()
        elif state == ON:
            if off_button_pressed:
                state = RAMP_DOWN
                status_led_flash_red()
            if time.time() - shutoff_timer_start >= SHUTOFF_TIMER_DURATION_SECONDS:
                state = RAMP_DOWN
                status_led_flash_red()
                log_action("SHUTOFF", "FILAMENT_OFF", "")
            off_button_pressed = False
            on_button_pressed = False
            time.sleep(0.1)
        elif state == RAMP_DOWN:
            off_button_pressed = False
            on_button_pressed = False
            time.sleep(float(RAMP_TIME_SECONDS) / (max_dac_value + 1))
            dac_value -= 1
            dac.raw_value = dac_value
            if dac_value <= 0:
                state = OFF
                status_led_solid_red()

# Function to update the active users' dictionary by adding the specified IP and dropping any IPs that have not made a /status request in ACTIVE_USER_MAX_IDLE_TIME_SECONDS seconds.
def update_active_users(ip_address):
    global active_users
    now = time.time()
    active_users[ip_address] = now
    new_active_users = {}
    for ip, timestamp in active_users.items():
        if now - timestamp <= ACTIVE_USER_MAX_IDLE_TIME_SECONDS:
            new_active_users[ip] = timestamp
    active_users = new_active_users

# Set up web server.
app = Flask(__name__)
app.config["SECRET_KEY"] = open("/home/pi/secret_key.txt").read().replace('\n', '')

# Homepage.
@app.route('/')
@app.route("/index")
def index():
    return render_template("index.html")

# Setup page.
@app.route("/setup", methods = ["GET", "POST"])
def setup():
    global max_dac_value
    if request.method == "GET":
        return render_template("setup.html")
    elif request.method == "POST":
        if state != OFF:
            return make_response("Error: You cannot change the settings while the filament is on, ramping up, or ramping down. Switch the filament off before attempting to modify settings.", 400)
        try:
            max_virtual_knob_value = float(request.form["max_virtual_knob_value"])
        except:
            return make_response("Error: Invalid or empty value for maximum virtual knob value.", 400)
        if max_virtual_knob_value <= 0.0 or max_virtual_knob_value > 10.0:
            return make_response("Error: Maximum virtual knob value must be in the range (0.0, 10.0].", 400)
        max_dac_value = int((max_virtual_knob_value / 10.0) * (2 ** DAC_BITS - 1))
        try:
            open("./max_dac_value.txt", 'w').write("{}".format(max_dac_value))
        except:
            return make_response("Error: Failed to save new maximum virtual knob setting to disk. The Raspberry Pi's SD card may be failing.", 400)
        return make_response("Successfully changed maximum virtual knob setting.", 200)

# API endpoint to switch the filament on.
@app.route("/filament-on")
def filamentOn():
    global on_button_pressed
    if not computer_control:
        return "Filament cannot be controlled from the API when in manual (knob) control mode."
    on_button_pressed = True
    log_action("WEB", "FILAMENT_ON", str(request.remote_addr))
    if state == ON:
        return "Filament is already on; you cannot turn it on while it is already on."
    elif state == OFF:
        return "Turning filament on..."
    elif state == RAMP_UP:
        return "Filament is ramping up; you cannot turn it on while it is already turning on."
    elif state == RAMP_DOWN:
        return "Filament is ramping down; you cannot turn it on while it is already turning off."

# API endpoint to switch the filament off.
@app.route("/filament-off")
def filamentOff():
    global off_button_pressed
    if not computer_control:
        return "Filament cannot be controlled from the API when in manual (knob) control mode."
    off_button_pressed = True
    log_action("WEB", "FILAMENT_OFF", str(request.remote_addr))
    if state == ON:
        return "Turning filament off..."
    elif state == OFF:
        return "Filament is already off; you cannot turn it off while it is already off."
    elif state == RAMP_UP:
        return "Filament is ramping up; you cannot turn it off while it is already turning on."
    elif state == RAMP_DOWN:
        return "Filament is ramping down; you cannot turn it off while it is already turning off."

# API endpoint to get the current filament control status.
@app.route("/status")
def status():
    global filament_status_message
    update_active_users(request.remote_addr)
    if state == ON:
        remaining_time = SHUTOFF_TIMER_DURATION_SECONDS - (time.time() - shutoff_timer_start)
        hours_left = int(remaining_time // 3600)
        minutes_left = int((remaining_time - (3600 * hours_left)) // 60)
        seconds_left = int(remaining_time - (3600 * hours_left) - (60 * minutes_left))
        filament_status_message = "Filament is ON, {} H:{} M:{} S left until automatic shutoff.".format(hours_left, minutes_left, seconds_left)
    elif state == OFF:
        filament_status_message = "Filament is OFF."
    elif state == RAMP_UP:
        filament_status_message = "Filament is ramping up ({}% complete)...".format(int(float(dac_value) / max_dac_value * 100))
    elif state == RAMP_DOWN:
        filament_status_message = "Filament is ramping down ({}% complete)...".format(int(100 - float(dac_value) / max_dac_value * 100))
    return make_response(jsonify({"computer_control": computer_control, "filament_status_message": filament_status_message, "active_users": len(active_users), "max_dac_value": max_dac_value, "dac_bits": DAC_BITS}), 200)

if __name__ == "__main__":
    try:
        t = threading.Thread(target = controller_thread)
        t.start()
        while state == STARTING:
            time.sleep(0.1)
        app.run(host = "0.0.0.0", port = 80)
    except KeyboardInterrupt:
        GPIO.cleanup()
        logfile.close()
