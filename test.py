#!/usr/bin/env python3

import time
import RPi.GPIO as GPIO

GPIO.setmode(GPIO.BCM)
GPIO.setup(13, GPIO.IN, pull_up_down = GPIO.PUD_UP)

def callback(_):
    print("Start callback...")
    time.sleep(0.01)
    print("Callback read state {}.".format(GPIO.input(13)))
    print("End callback.")

GPIO.add_event_detect(13, GPIO.BOTH, callback = callback, bouncetime = 50)

try:
    while True:
        time.sleep(1000)
except KeyboardInterrupt:
    GPIO.cleanup()
