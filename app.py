#!/usr/bin/env python3

"""
Program with web and physical interfaces to allow the control of a DAC that controls the power to the J2010 filament. Smoothly ramps up and ramps down the power to prevent damage to the crystal. Writes out a CSV file to ~/filament_controller_log.csv with the following columns:
Raw Timestamp - Floating-point number of seconds since the Unix epoch.
Formatted Timestamp - Timestamp of the format "%m/%d/%Y %I:%M:%S %p" (see the strftime (3) manpage for details).
Control Type - Either PANEL or WEB, depending on whether the control action was initiated from the panel switches and buttons or the web interface.
Control Action - SWITCH_TO_MANUAL_CONTROL, SWITCH_TO_COMPUTER_CONTROL, FILAMENT_ON, or FILAMENT_OFF, depending on what action was taken. The first two actions can only be initiated by PANEL control (by the user moving the physical switch), but the last two can be either PANEL or WEB control.
IP Address - A string like "192.168.1.168" with the IP address of the web request that initiated the action. Empty for PANEL actions.
MAC Address - A string like "00:1B:44:11:3A:B7" with the MAC address of the computer that originated the action (may not be accurate if the computer is not on the same LAN as the Raspberry Pi). Empty for PANEL actions.
"""

RAMP_TIME_SECONDS = 30    # Time over which the filament is to be ramped up or down, in seconds.
ACTIVE_USER_MAX_IDLE_TIME = 5 # Time after which an active user who is not sending /status requests will be marked as inactive, in seconds.
DAC_BITS = 12 # Number of bits offered by the DAC. Raw values sent to the dac will be in the range [0, 2^DAC_BITS).

import json
import time
import os.path
import threading
from datetime import datetime
import netifaces as nif
from flask import Flask, render_template, jsonify, make_response, request

import board
import busio
import adafruit_mcp4725

# States for the controller state machine.
OFF = 0
ON = 1
RAMP_UP = 2
RAMP_DOWN = 3
state = OFF # Current state of the controller state machine.

dac_value = 0 # Current value of the DAC (will be initialized by the controller thread).
max_dac_value = int(open("./max_dac_value.txt").read().replace('\n', ''))  # Maximum allowed value of the DAC.
on_button_pressed = False # Whether the ON button was just pressed, either on the webpage or the physical buttons.
off_button_pressed = False # Whether the OFF button was just pressed, either on the webpage or the physical buttons.
computer_control = True # Whether the filament is currently being controlled by the Pi or by the manual knob.
active_users = {} # Dictionary associating connected web clients' IP addresses and the time.time() timestamp at which they last called the /status endpoint.

logfile_name = os.path.expanduser("~/filament_controller_log.csv")
if os.path.exists(logfile_name):
    logfile = open(logfile_name, 'a')
else:
    logfile = open(logfile_name, 'w')
    logfile.write("Raw Timestamp,Formatted Timestamp,Control Type,Control Action,IP Address,MAC Address\n")

# Function to write a row to the CSV logfile.
def log_action(control_type, control_action, ip_address):
    global logfile
    mac_address = str(mac_for_ip(ip_address))
    raw_timestamp = time.time()
    formatted_timestamp = datetime.fromtimestamp(raw_timestamp).strftime("%m/%d/%Y %I:%M:%S %p")
    logfile.write("{},{},{},{},{},{}\n".format(raw_timestamp, formatted_timestamp, control_type, control_action, ip_address, mac_address))
    logfile.flush()

# Function to get the MAC address of an IP (see https://stackoverflow.com/a/22241056).
def mac_for_ip(ip):
    'Returns a list of MACs for interfaces that have given IP, returns None if not found'
    for i in nif.interfaces():
        addrs = nif.ifaddresses(i)
        try:
            if_mac = addrs[nif.AF_LINK][0]['addr']
            if_ip = addrs[nif.AF_INET][0]['addr']
        except (IndexError, KeyError): # Ignore interfaces that don't have MAC or IP addresses.
            if_mac = if_ip = None
        if if_ip == ip:
            return if_mac
    return None

# This function runs in a separate thread and handles actually controlling the filament.
def controller_thread():
    global state
    global dac_value
    global on_button_pressed
    global off_button_pressed
    # Open the DAC device.
    i2c = busio.I2C(board.SCL, board.SDA)
    dac = adafruit_mcp4725.MCP4725(i2c)
    # Get the current value of the DAC.
    dac_value = dac.raw_value
    # If the DAC isn't off right now, ramp down.
    if dac_value > 0:
        state = RAMP_DOWN
    # Main state machine loop.
    while True:
        if state == OFF:
            if on_button_pressed:
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
        elif state == ON:
            if off_button_pressed:
                state = RAMP_DOWN
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

# Function to update the active users' dictionary by adding the specified IP and dropping any IPs that have not made a /status request in ACTIVE_USER_MAX_IDLE_TIME seconds.
def update_active_users(ip_address):
    global active_users
    now = time.time()
    active_users[ip_address] = now
    new_active_users = {}
    for ip, timestamp in active_users.items():
        if now - timestamp <= ACTIVE_USER_MAX_IDLE_TIME:
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
        filament_status_message = "Filament is ON."
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
        app.run(host = "0.0.0.0", port = 80)
    except KeyboardInterrupt:
        logfile.close()
