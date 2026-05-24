import asyncio
import logging

from agent.scheduler.main import main_loop


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main_loop())


if __name__ == "__main__":
    main()
