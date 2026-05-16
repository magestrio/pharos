from apscheduler.schedulers.blocking import BlockingScheduler

from agent.loop import run_cycle


def main() -> None:
    scheduler = BlockingScheduler()
    scheduler.add_job(run_cycle, "cron", hour="*/4")
    print("Vault8004 agent starting — rebalancing every 4 hours")
    scheduler.start()


if __name__ == "__main__":
    main()
