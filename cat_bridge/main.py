"""Entry point for CAT bridge."""

import asyncio
import logging

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
        logging.info("Shutting down.")


if __name__ == "__main__":
    main()