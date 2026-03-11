import asyncio
import logging
import os
import signal

from .bridge import CATBridge

def main() -> None:
    """Start the CAT bridge."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    bridge = CATBridge()
    try:
        asyncio.run(bridge.start())
    except KeyboardInterrupt:
        logging.info("Shutting down forcefully.")
        os._exit(0)  # Force immediate exit

if __name__ == "__main__":
    main()