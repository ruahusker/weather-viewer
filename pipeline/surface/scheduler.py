"""Surface observations scheduler — fetches IEM Mesonet every 5 minutes."""
import logging
import time

from pipeline.surface.fetcher import fetch_and_write

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [surface] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

POLL_INTERVAL = 300  # 5 minutes


def main():
    log.info("Surface observations scheduler starting (interval=%ds)", POLL_INTERVAL)
    while True:
        try:
            count = fetch_and_write()
            log.info("Surface obs updated: %d stations", count)
        except Exception as exc:
            log.error("Surface fetch error (will retry in %ds): %s", POLL_INTERVAL, exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
