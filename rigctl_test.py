#!/usr/bin/env python3
"""
Continuous poller and setter for rigctld (Hamlib network daemon).
Connects to 127.0.0.1:4532, prints frequency, mode, and TX status every second,
and periodically changes frequency and mode in a loop:
- frequency toggles between 7 MHz and 8 MHz
- mode cycles LSB, USB, FM, CW (with appropriate passbands)
"""

import socket
import time
import datetime

HOST = '127.0.0.1'
PORT = 4532
POLL_INTERVAL = 1.0       # seconds between polls
CHANGE_INTERVAL = 5.0     # seconds between setting changes

# Frequency toggling
FREQ_1 = 7_000_000
FREQ_2 = 8_000_000
current_freq_target = FREQ_1

# Mode cycling (mode, passband in Hz)
MODES = [
    ('LSB', 2400),
    ('USB', 2400),
    ('FM',  5000),
    ('CW',   500)
]
mode_index = 0

def send_command(sock, cmd):
    """Send a command and return the stripped response."""
    sock.sendall((cmd + '\n').encode())
    data = sock.recv(1024).decode().strip()
    return data

def set_frequency(sock, freq):
    """Set VFO frequency (in Hz)."""
    resp = send_command(sock, f'F {freq}')
    print(f"Set frequency to {freq} Hz -> response: {resp}")

def set_mode(sock, mode, passband):
    """Set demodulator mode with passband."""
    resp = send_command(sock, f'M {mode} {passband}')
    print(f"Set mode to {mode} (passband {passband}) -> response: {resp}")

def main():
    global current_freq_target, mode_index
    print(f"Connecting to rigctld at {HOST}:{PORT} ...")
    next_change_time = time.time() + CHANGE_INTERVAL

    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect((HOST, PORT))
                print("Connected.\n")

                while True:
                    try:
                        # Poll current status
                        freq = send_command(sock, 'f')
                        mode_resp = send_command(sock, 'm')
                        tx = send_command(sock, 't')

                        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        print(f"[{timestamp}] Freq: {freq} Hz | Mode: {mode_resp} | TX: {'ON' if tx == '1' else 'OFF'}")

                        # Check if it's time to change settings
                        now = time.time()
                        if now >= next_change_time:
                            # Toggle frequency
                            current_freq_target = FREQ_2 if current_freq_target == FREQ_1 else FREQ_1
                            set_frequency(sock, current_freq_target)

                            # Cycle mode
                            mode, passband = MODES[mode_index]
                            set_mode(sock, mode, passband)
                            mode_index = (mode_index + 1) % len(MODES)

                            next_change_time = now + CHANGE_INTERVAL

                        time.sleep(POLL_INTERVAL)

                    except (socket.error, BrokenPipeError) as e:
                        print(f"Connection lost: {e}. Reconnecting...")
                        break
                    except KeyboardInterrupt:
                        print("\nExiting.")
                        return

        except ConnectionRefusedError:
            print(f"rigctld not available at {HOST}:{PORT}. Retrying in 5 seconds...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nExiting.")
            break

if __name__ == '__main__':
    main()