import socket
import time

RIG_HOST = "192.168.1.18"
RIG_PORT = 4532


def send_command(sock, cmd: str) -> str:
    sock.sendall((cmd + "\n").encode())
    time.sleep(0.1)
    data = sock.recv(4096)
    return data.decode(errors="ignore").strip()


def main():
    print(f"Connecting to {RIG_HOST}:{RIG_PORT}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((RIG_HOST, RIG_PORT))

    print("Connected.\n")

    while True:
        try:
            print("----")
            resp_f = send_command(sock, "f")
            print(f"Raw frequency response: '{resp_f}'")

            resp_m = send_command(sock, "m")
            print(f"Raw mode response: '{resp_m}'")

            resp_big_m = send_command(sock, "M")
            print(f"Raw M response: '{resp_big_m}'")

            resp_get = send_command(sock, "\\get_mode")
            print(f"Raw \\get_mode response: '{resp_get}'")

            time.sleep(2)

        except KeyboardInterrupt:
            print("\nExiting.")
            break
        except Exception as e:
            print(f"Error: {e}")
            break

    sock.close()


if __name__ == "__main__":
    main()